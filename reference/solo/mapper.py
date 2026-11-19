"""Read mapper: registered statements only, one account per call, read-only connection.

A production mapper runs the same contract on PostgreSQL (read-only role,
REPEATABLE READ snapshot, statement timeout, decryption). This one runs on SQLite so the contract
can be read and tested without any service.
"""
from contextlib import closing
import sqlite3

from .sqlreg import params


class MapperError(Exception):
    """A refusal the caller can act on. `code` is one of the tool error codes."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code, self.message = code, message


def check(sql, prefixes, statement_id, values):
    """The statement is registered, its prefix is allowed here, and exactly its parameters were passed."""
    if statement_id not in sql or not statement_id.startswith(prefixes):
        raise ValueError("statement is not allowed")
    statement = sql[statement_id]
    if set(values) != params(statement):
        raise ValueError("parameters do not match the statement")
    if not isinstance(values["account_id"], str) or not values["account_id"]:
        raise ValueError("account_id is required")
    return statement


class ReadMapper:
    def __init__(self, path, sql, *, audit=None, cap=1000):
        self.path, self.sql, self.cap = path, sql, cap
        self.audit = audit or (lambda entry: None)

    def query(self, statement_id, **values):
        statement = check(self.sql, ("read.",), statement_id, values)
        # The audit carries the statement ID, the account and the row count. Never a value.
        entry = {"mapper": "read", "statement_id": statement_id, "account_id": values["account_id"], "ok": False}
        try:
            with closing(sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)) as conn:
                conn.row_factory = sqlite3.Row
                rows = [dict(row) for row in conn.execute(statement, values)]
            entry.update(ok=True, rows=len(rows))
            return rows
        except sqlite3.Error:
            # A failed lookup is a failure. It is never turned into an empty result.
            raise MapperError("BACKEND_UNAVAILABLE", "lookup failed") from None
        finally:
            self.audit(entry)

    def one(self, statement_id, **values):
        rows = self.query(statement_id, **values)
        if not rows:
            # Another account's row and a missing row look the same to the caller.
            raise MapperError("NOT_FOUND", "not found in this account")
        return rows[0]

    def page(self, statement_id, **values):
        """A list read asks for cap + 1 rows. The extra row only says the list was cut."""
        rows = self.query(statement_id, row_limit=self.cap + 1, **values)
        return {"items": rows[:self.cap], "truncated": len(rows) > self.cap}
