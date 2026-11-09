"""The safety layer is the point of this server, so it is what gets tested.

These tests run without a database: they verify the guards that sit in front
of it. The read-only connection option is the second, independent layer and
is asserted in test_connection_options.
"""

import pytest

from pg_inspect_mcp.server import (
    validate_identifier,
    validate_readonly_query,
)


class TestIdentifierValidation:
    def test_accepts_plain_identifiers(self):
        assert validate_identifier("orders") == "orders"
        assert validate_identifier("_private_tbl") == "_private_tbl"
        assert validate_identifier("t2$audit") == "t2$audit"

    @pytest.mark.parametrize(
        "bad",
        [
            "orders; DROP TABLE users",
            'orders"',
            "orders--",
            "or ders",
            "",
            "1starts_with_digit",
            "sneaky'quote",
        ],
    )
    def test_rejects_injection_shapes(self, bad):
        with pytest.raises(ValueError):
            validate_identifier(bad)


class TestQueryGuard:
    def test_accepts_select(self):
        assert validate_readonly_query("SELECT 1") == "SELECT 1"

    def test_accepts_cte(self):
        q = "WITH x AS (SELECT 1 AS n) SELECT n FROM x"
        assert validate_readonly_query(q) == q

    def test_strips_trailing_semicolon(self):
        assert validate_readonly_query("SELECT 1;") == "SELECT 1"

    @pytest.mark.parametrize(
        "bad",
        [
            "DELETE FROM orders",
            "INSERT INTO t VALUES (1)",
            "UPDATE t SET a = 1",
            "DROP TABLE t",
            "SELECT 1; DELETE FROM orders",       # stacked statements
            "WITH x AS (DELETE FROM t RETURNING *) SELECT * FROM x",  # writing CTE
            "EXPLAIN DELETE FROM t",
            "COPY t TO '/tmp/x'",
            "CREATE TABLE t (a int)",
        ],
    )
    def test_rejects_writes(self, bad):
        with pytest.raises(ValueError):
            validate_readonly_query(bad)


def test_connection_options_are_read_only():
    """The DSN options must force read-only transactions at the server side."""
    import inspect
    from pg_inspect_mcp import server

    src = inspect.getsource(server.get_conn)
    assert "default_transaction_read_only=on" in src


class TestForeignKeyInspection:
    def test_parameterized_catalog_query(self, monkeypatch):
        from pg_inspect_mcp import server

        captured = {}

        def fake_rows(query, params=()):
            captured["query"] = query
            captured["params"] = params
            return "[]"

        monkeypatch.setattr(server, "_rows", fake_rows)
        assert server._foreign_keys("orders", "sales") == "[]"
        assert captured["params"] == ("sales", "orders")
        assert "unnest(con.conkey, con.confkey)" in captured["query"]
        assert "ORDER BY keys.ordinality" in captured["query"]
        assert "con.contype = 'f'" in captured["query"]

    @pytest.mark.parametrize("schema,table", [
        ("public; DROP SCHEMA public", "orders"),
        ("public", "orders; DROP TABLE orders"),
    ])
    def test_rejects_invalid_identifiers(self, monkeypatch, schema, table):
        from pg_inspect_mcp import server

        def unexpected_query(*args, **kwargs):
            pytest.fail("Invalid identifier reached the database layer")

        monkeypatch.setattr(server, "_rows", unexpected_query)
        with pytest.raises(ValueError):
            server._foreign_keys(table, schema)
