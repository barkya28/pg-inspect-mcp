"""pg-inspect-mcp: a read-only Postgres schema-inspection MCP server.

Design principle: the read-only constraint is enforced structurally, not by
prompt instructions. Every connection is opened with
`default_transaction_read_only=on`, so even if a caller (or a confused model)
smuggles a write into a query, Postgres itself rejects it. A SQL guard on top
of that restricts free-form input to SELECT/WITH before it ever reaches the
database. Two independent layers; neither trusts the model.
"""

from __future__ import annotations

import json
import os
import re

import psycopg
from psycopg import sql
from psycopg.rows import dict_row

from mcp.server import MCPServer

server = MCPServer(
    name="pg-inspect",
    instructions=(
        "Read-only Postgres schema inspector. Explore schemas, tables, "
        "indexes, sizes and query plans. This server cannot write: every "
        "connection is opened read-only at the Postgres level."
    ),
    version="0.1.0",
)

# --- structural safety -------------------------------------------------------

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")

# Only statements that begin with these keywords are accepted for EXPLAIN.
_ALLOWED_QUERY_PREFIX = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

# Defence in depth: reject obvious write/DDL keywords anywhere in the text,
# even though the read-only connection would refuse them anyway.
_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|MERGE|TRUNCATE|DROP|ALTER|CREATE|GRANT|REVOKE|"
    r"COPY|VACUUM|CALL|DO|SET\s+ROLE)\b",
    re.IGNORECASE,
)


def validate_identifier(name: str, kind: str = "identifier") -> str:
    """Reject anything that is not a plain SQL identifier."""
    if not _IDENT_RE.match(name):
        raise ValueError(f"invalid {kind}: {name!r}")
    return name


def validate_readonly_query(query: str) -> str:
    """Allow only SELECT/WITH statements, single statement, no write keywords."""
    if ";" in query.rstrip().rstrip(";"):
        raise ValueError("multiple statements are not allowed")
    if not _ALLOWED_QUERY_PREFIX.match(query):
        raise ValueError("only SELECT/WITH queries are allowed")
    if _FORBIDDEN.search(query):
        raise ValueError("query contains a forbidden keyword")
    return query.rstrip().rstrip(";")


def statement_timeout_ms() -> int:
    """Read a bounded PostgreSQL statement timeout from the environment."""
    raw = os.environ.get("PG_INSPECT_STATEMENT_TIMEOUT_MS", "5000")
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise ValueError("PG_INSPECT_STATEMENT_TIMEOUT_MS must be an integer") from exc
    if not 1 <= timeout <= 600000:
        raise ValueError("PG_INSPECT_STATEMENT_TIMEOUT_MS must be between 1 and 600000")
    return timeout


def get_conn() -> psycopg.Connection:
    """Open a connection that Postgres itself treats as read-only."""
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        raise RuntimeError("DATABASE_URL is not set")
    conn = psycopg.connect(
        dsn,
        options=(
            "-c default_transaction_read_only=on "
            f"-c statement_timeout={statement_timeout_ms()}"
        ),
        row_factory=dict_row,
        autocommit=True,
    )
    return conn


def _rows(query: sql.Composable | str, params: tuple = ()) -> str:
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute(query, params)
        return json.dumps(cur.fetchall(), default=str, indent=2)


# --- tools -------------------------------------------------------------------


@server.tool()
def list_schemas() -> str:
    """List non-system schemas in the connected database."""
    return _rows(
        """
        SELECT schema_name
        FROM information_schema.schemata
        WHERE schema_name NOT IN ('pg_catalog', 'information_schema')
        ORDER BY schema_name
        """
    )


@server.tool()
def list_tables(schema: str = "public") -> str:
    """List tables and views in a schema, with estimated row counts."""
    validate_identifier(schema, "schema")
    return _rows(
        """
        SELECT c.relname AS name,
               CASE c.relkind WHEN 'r' THEN 'table' WHEN 'v' THEN 'view'
                    WHEN 'm' THEN 'materialized view' WHEN 'p' THEN 'partitioned table'
               END AS kind,
               c.reltuples::bigint AS estimated_rows
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname = %s AND c.relkind IN ('r', 'v', 'm', 'p')
        ORDER BY c.relname
        """,
        (schema,),
    )


@server.tool()
def describe_table(table: str, schema: str = "public") -> str:
    """Show columns, types, nullability and defaults for a table."""
    validate_identifier(schema, "schema")
    validate_identifier(table, "table")
    return _rows(
        """
        SELECT column_name, data_type, is_nullable, column_default,
               character_maximum_length
        FROM information_schema.columns
        WHERE table_schema = %s AND table_name = %s
        ORDER BY ordinal_position
        """,
        (schema, table),
    )


@server.tool()
def list_indexes(table: str, schema: str = "public") -> str:
    """List indexes on a table with their definitions."""
    validate_identifier(schema, "schema")
    validate_identifier(table, "table")
    return _rows(
        """
        SELECT indexname, indexdef
        FROM pg_indexes
        WHERE schemaname = %s AND tablename = %s
        ORDER BY indexname
        """,
        (schema, table),
    )


@server.tool()
def table_stats(table: str, schema: str = "public") -> str:
    """Table size on disk, index size, and live/dead tuple estimates."""
    validate_identifier(schema, "schema")
    validate_identifier(table, "table")
    return _rows(
        """
        SELECT pg_size_pretty(pg_table_size(c.oid)) AS table_size,
               pg_size_pretty(pg_indexes_size(c.oid)) AS index_size,
               s.n_live_tup, s.n_dead_tup, s.last_vacuum, s.last_analyze
        FROM pg_class c
        JOIN pg_namespace n ON n.oid = c.relnamespace
        LEFT JOIN pg_stat_user_tables s
               ON s.schemaname = n.nspname AND s.relname = c.relname
        WHERE n.nspname = %s AND c.relname = %s
        """,
        (schema, table),
    )


@server.tool()
def sample_rows(table: str, schema: str = "public", limit: int = 10) -> str:
    """Fetch up to 50 sample rows from a table."""
    validate_identifier(schema, "schema")
    validate_identifier(table, "table")
    limit = max(1, min(int(limit), 50))
    query = sql.SQL("SELECT * FROM {}.{} LIMIT {}").format(
        sql.Identifier(schema), sql.Identifier(table), sql.Literal(limit)
    )
    return _rows(query)


@server.tool()
def explain_query(query: str) -> str:
    """Return the JSON query plan (EXPLAIN, no execution) for a SELECT/WITH query."""
    safe = validate_readonly_query(query)
    with get_conn() as conn, conn.cursor() as cur:
        cur.execute("EXPLAIN (FORMAT JSON) " + safe)
        plan = cur.fetchone()
        return json.dumps(plan, default=str, indent=2)


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
