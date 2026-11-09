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


class TestStatementTimeout:
    @pytest.mark.parametrize("value,expected", [
        (None, 5000),
        ("1", 1),
        ("600000", 600000),
        ("2500", 2500),
    ])
    def test_valid_timeout(self, monkeypatch, value, expected):
        from pg_inspect_mcp import server

        if value is None:
            monkeypatch.delenv("PG_INSPECT_STATEMENT_TIMEOUT_MS", raising=False)
        else:
            monkeypatch.setenv("PG_INSPECT_STATEMENT_TIMEOUT_MS", value)
        assert server.statement_timeout_ms() == expected

    @pytest.mark.parametrize("value", ["", "0", "-1", "600001", "abc", "1.5"])
    def test_invalid_timeout(self, monkeypatch, value):
        from pg_inspect_mcp import server

        monkeypatch.setenv("PG_INSPECT_STATEMENT_TIMEOUT_MS", value)
        with pytest.raises(ValueError, match="PG_INSPECT_STATEMENT_TIMEOUT_MS"):
            server.statement_timeout_ms()

    def test_connect_enforces_read_only_and_timeout(self, monkeypatch):
        from pg_inspect_mcp import server

        captured = {}
        monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/test")
        monkeypatch.setenv("PG_INSPECT_STATEMENT_TIMEOUT_MS", "1200")

        def fake_connect(dsn, **kwargs):
            captured.update(dsn=dsn, **kwargs)
            return object()

        monkeypatch.setattr(server.psycopg, "connect", fake_connect)
        server.get_conn()
        assert captured["dsn"] == "postgresql://localhost/test"
        assert "default_transaction_read_only=on" in captured["options"]
        assert "statement_timeout=1200" in captured["options"]
        assert captured["autocommit"] is True
