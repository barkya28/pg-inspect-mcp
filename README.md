# pg-inspect-mcp

A read-only Postgres schema-inspection [MCP](https://modelcontextprotocol.io) server. Point an MCP client (Claude Desktop, Claude Code, or anything speaking MCP) at a database and explore schemas, tables, indexes, sizes, sample data and query plans in natural language.

## Design principle: impossible, not forbidden

Most "read-only" AI database tools are read-only because a prompt says so. That is a warning label, not a constraint — prompt injection or model error walks straight through it.

This server enforces read-only access **structurally, in two independent layers**, neither of which trusts the model:

1. **The connection itself is read-only.** Every connection is opened with `default_transaction_read_only=on`, so Postgres rejects any write — even one that somehow reaches the database.
2. **A SQL guard rejects writes before the database sees them.** Free-form input (only accepted by `explain_query`) must be a single `SELECT`/`WITH` statement; stacked statements, writing CTEs, and DDL/DML keywords are refused. All identifier inputs are validated against a strict pattern and quoted with `psycopg.sql.Identifier`.

No amount of clever input produces a write, because no code path can perform one.

## Tools

| Tool | What it does |
|---|---|
| `list_schemas` | Non-system schemas |
| `list_tables` | Tables/views in a schema with estimated row counts |
| `describe_table` | Columns, types, nullability, defaults |
| `list_indexes` | Index definitions |
| `table_stats` | On-disk size, index size, live/dead tuples, last vacuum/analyze |
| `sample_rows` | Up to 50 sample rows |
| `explain_query` | `EXPLAIN (FORMAT JSON)` for a SELECT/WITH query — plan only, never executed |

## Setup

```bash
pip install -e .
export DATABASE_URL=postgresql://user:pass@host:5432/dbname
pg-inspect-mcp
```

For extra safety in shared environments, connect as a Postgres role that has only `SELECT` grants — then the least-privilege credential is a third independent layer.

### Claude Desktop / Claude Code config

```json
{
  "mcpServers": {
    "pg-inspect": {
      "command": "pg-inspect-mcp",
      "env": { "DATABASE_URL": "postgresql://user:pass@host:5432/dbname" }
    }
  }
}
```

### Local database for development

```bash
docker compose up -d
export DATABASE_URL=postgresql://postgres:postgres@localhost:5433/postgres
```

## Tests

The safety layer is the point of the server, so it is what gets tested:

```bash
pip install pytest && pytest
```

Covers identifier injection shapes, stacked statements, writing CTEs (`WITH x AS (DELETE ...)`), `EXPLAIN DELETE`, `COPY`, and the read-only connection option.

## Failure modes considered

- **Prompt injection via table contents** — sampled rows may contain hostile text; the guard doesn't care what the model *wants*, only what the connection *can do*.
- **Writing CTEs** — `WITH x AS (DELETE ... RETURNING *)` is valid SQL that begins with an allowed keyword; explicitly rejected.
- **`EXPLAIN ANALYZE`** — actually executes the query; this server only ever issues plain `EXPLAIN`.
- **Stacked statements** — `SELECT 1; DELETE ...` refused before parsing.

## License

MIT


### Query timeout

By default, PostgreSQL cancels statements taking longer than **5 seconds**.
Set `PG_INSPECT_STATEMENT_TIMEOUT_MS` to override this (integer milliseconds,
1–600000; maximum 10 minutes). The timeout is enforced by PostgreSQL on each
connection using `statement_timeout`, alongside the existing read-only
transaction setting. For example:

```bash
export PG_INSPECT_STATEMENT_TIMEOUT_MS=10000
```

Invalid timeout values cause a configuration error instead of silently
disabling the limit.
