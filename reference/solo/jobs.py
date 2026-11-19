"""Job contract for work that leaves an effect outside our database: RPA, cron batches, external APIs.

States: queued -> running -> succeeded | failed | unknown, and queued -> cancelled.
`unknown` means the effect may already exist in the original system. It is never re-run
automatically; it is reconciled against the original system by its business key.
A job is unique per (account, kind, business key), so a scheduler that fires twice submits once.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import uuid

from .mapper import MapperError

SCHEMA = """
CREATE TABLE IF NOT EXISTS job (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, kind TEXT NOT NULL, business_key TEXT NOT NULL,
    payload TEXT NOT NULL, state TEXT NOT NULL, result TEXT, lease_until TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    UNIQUE (account_id, kind, business_key)
)"""
_FIELDS = "id, account_id, kind, business_key, payload, state, result, lease_until, created_at, updated_at"


class NoEffect(Exception):
    """Raised by a handler when it is certain that nothing was applied to the original system."""


def _stamp(moment):
    return moment.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _job(row):
    job = dict(row)
    job["payload"] = json.loads(job["payload"])
    job["result"] = json.loads(job["result"]) if job["result"] else None
    return job


class Jobs:
    def __init__(self, path, *, clock=None):
        self.path = path
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(self._connect()) as conn:
            conn.execute(SCHEMA)

    def _connect(self):
        conn = sqlite3.connect(self.path, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _one(self, conn, where, *values):
        row = conn.execute(f"SELECT {_FIELDS} FROM job WHERE {where}", values).fetchone()
        return _job(row) if row else None

    def submit(self, account_id, kind, business_key, payload):
        """Accept a job. The same business key returns the job that already exists.

        A failed or cancelled job left no effect in the original system, so submitting its key again puts the
        same job back in the queue with the new payload. A queued, running, succeeded or unknown job is returned
        as it is: unknown must be reconciled first.
        """
        now = _stamp(self.clock())
        where = "account_id = ? AND kind = ? AND business_key = ?"
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = self._one(conn, where, account_id, kind, business_key)
            if job is None:
                conn.execute(f"INSERT INTO job ({_FIELDS}) VALUES (?, ?, ?, ?, ?, 'queued', NULL, NULL, ?, ?)",
                             (uuid.uuid4().hex, account_id, kind, business_key,
                              json.dumps(payload, ensure_ascii=False), now, now))
            elif job["state"] in ("failed", "cancelled"):
                conn.execute("UPDATE job SET state = 'queued', payload = ?, result = NULL, lease_until = NULL, "
                             "updated_at = ? WHERE id = ?", (json.dumps(payload, ensure_ascii=False), now, job["id"]))
            job = self._one(conn, where, account_id, kind, business_key)
            conn.execute("COMMIT")
        return job

    def get(self, job_id, account_id):
        with closing(self._connect()) as conn:
            job = self._one(conn, "id = ? AND account_id = ?", job_id, account_id)
        if job is None:
            raise MapperError("NOT_FOUND", "not found in this account")
        return job

    def _move(self, job_id, sources, target, result=None, lease_until=None):
        with closing(self._connect()) as conn:
            marks = ", ".join("?" for _ in sources)
            done = conn.execute(
                f"UPDATE job SET state = ?, result = ?, lease_until = ?, updated_at = ? WHERE id = ? AND state IN ({marks})",
                (target, json.dumps(result, ensure_ascii=False) if result is not None else None, lease_until,
                 _stamp(self.clock()), job_id, *sources)).rowcount
            if not done:
                raise MapperError("CONFLICT", f"the job is not in a state that can become {target}")
            return self._one(conn, "id = ?", job_id)

    def cancel(self, job_id, account_id):
        """Only before a worker took it. After that the effect may exist."""
        self.get(job_id, account_id)
        return self._move(job_id, ("queued",), "cancelled")

    def claim(self, lease_seconds=300):
        """The oldest queued job becomes running under an exclusive lease, or None."""
        now = self.clock()
        with closing(self._connect()) as conn:
            conn.execute("BEGIN IMMEDIATE")
            # rowid breaks a tie between jobs submitted in the same instant, in submission order.
            job = self._one(conn, "state = 'queued' ORDER BY created_at, rowid LIMIT 1")
            if job is not None:
                conn.execute("UPDATE job SET state = 'running', lease_until = ?, updated_at = ? WHERE id = ?",
                             (_stamp(now + timedelta(seconds=lease_seconds)), _stamp(now), job["id"]))
                job = self._one(conn, "id = ?", job["id"])
            conn.execute("COMMIT")
        return job

    def expire(self):
        """A running job whose lease ran out becomes unknown. It is not queued again."""
        with closing(self._connect()) as conn:
            now = _stamp(self.clock())
            return conn.execute("UPDATE job SET state = 'unknown', lease_until = NULL, updated_at = ? "
                                "WHERE state = 'running' AND lease_until < ?", (now, now)).rowcount

    def finish(self, job_id, state, result=None):
        if state not in ("succeeded", "failed", "unknown"):
            raise ValueError("a running job ends as succeeded, failed or unknown")
        return self._move(job_id, ("running",), state, result)

    def reconcile(self, job_id, result):
        """After someone checked the original system: a result means it was applied, None means it was not."""
        return self._move(job_id, ("unknown",), "succeeded" if result is not None else "failed", result)


def step(jobs, handlers):
    """One worker turn. A handler has find(job) -> result or None, and act(job) -> None.

    find looks the job up in the original system by its business key. act performs one external action.
    Every turn first turns running jobs whose lease ran out into unknown, so a dead worker's job is not left running.
    """
    jobs.expire()
    job = jobs.claim()
    if job is None:
        return None
    handler = handlers[job["kind"]]
    try:
        found = handler.find(job)
    except Exception:
        return jobs.finish(job["id"], "unknown")
    if found is not None:
        # Already there: record it instead of doing it twice.
        return jobs.finish(job["id"], "succeeded", found)
    try:
        handler.act(job)
    except NoEffect:
        return jobs.finish(job["id"], "failed")
    except Exception:
        pass  # the action may have landed before it failed; only the lookup below decides
    try:
        found = handler.find(job)
    except Exception:
        found = None
    # Pressing save is not the same as the record existing. Only the original system confirms success.
    return jobs.finish(job["id"], "succeeded", found) if found is not None else jobs.finish(job["id"], "unknown")
