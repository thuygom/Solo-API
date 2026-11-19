"""Write mapper: one call is one transaction.

The row is locked, the caller's version is compared, the change is made and the row as it was
before the change is returned, so the caller can undo it. `dry_run` runs the same SQL and rolls back.
A production writer uses a separate writer role and SELECT ... FOR UPDATE.
SQLite has no row lock, so BEGIN IMMEDIATE takes the write lock for the whole transaction.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3

from .mapper import MapperError, check


class WriteMapper:
    def __init__(self, path, sql, *, audit=None, clock=None):
        self.path, self.sql = path, sql
        self.audit = audit or (lambda entry: None)
        self.clock = clock or (lambda: datetime.now(timezone.utc).isoformat(timespec="microseconds"))

    @contextmanager
    def transaction(self, operation, account_id, dry_run=False):
        entry = {"mapper": "write", "operation": operation, "account_id": account_id, "dry_run": dry_run, "ok": False}
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.execute("ROLLBACK" if dry_run else "COMMIT")
            entry["ok"] = True
        except MapperError as error:
            conn.execute("ROLLBACK")
            entry["code"] = error.code
            raise
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MapperError("BACKEND_UNAVAILABLE", "write failed and was rolled back") from None
        finally:
            conn.close()
            self.audit(entry)

    def run(self, conn, statement_id, **values):
        statement = check(self.sql, ("write.", "lock."), statement_id, values)
        return conn.execute(statement, values)

    def locked(self, conn, statement_id, expected_updated_at=None, **values):
        """The current row of this account, or 404. With a version, 409 when someone changed it first."""
        row = self.run(conn, statement_id, **values).fetchone()
        if row is None:
            raise MapperError("NOT_FOUND", "not found in this account")
        before = dict(row)
        if expected_updated_at is not None and before["updated_at"] != expected_updated_at:
            raise MapperError("CONFLICT", "the row changed after it was read")
        return before
