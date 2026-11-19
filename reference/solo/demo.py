"""A small task service built the Solo-API way, to read and to run.

    python -m solo.demo catalog [mcp|openai|anthropic]
    python -m solo.demo call task_list '{}' --account acct-a
    python -m solo.demo call task_set_status '{...}' --account acct-a --request-id <uuid>
    python -m solo.demo tick --account acct-a          # what a cron line runs

No HTTP endpoint is added for any of this. The assistant gets tools, the scheduler gets jobs,
and both reach the database through the same fixed statements.
"""
import argparse
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import sys
import uuid

from .jobs import Jobs, step
from .mapper import MapperError, ReadMapper
from .sqlreg import registry
from .tools import Tool, Tools
from .writer import WriteMapper

KST = timezone(timedelta(hours=9))
SCHEMA = """
CREATE TABLE IF NOT EXISTS task (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, title TEXT NOT NULL,
    status TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS day_report (
    account_id TEXT NOT NULL, day TEXT NOT NULL, open_count INTEGER NOT NULL, done_count INTEGER NOT NULL,
    PRIMARY KEY (account_id, day)
);
"""
_TASK = 'id, title, status, updated_at AS "updatedAt"'
SQL = registry({
    "read.task": f"SELECT {_TASK} FROM task WHERE account_id = :account_id AND id = :task_id",
    "read.tasks": f"SELECT {_TASK} FROM task WHERE account_id = :account_id AND status = :status "
                  "ORDER BY updated_at DESC, id LIMIT :row_limit",
    "read.task_counts": "SELECT status, count(*) AS n FROM task WHERE account_id = :account_id GROUP BY status",
    "read.day_report": 'SELECT day, open_count AS "openCount", done_count AS "doneCount" FROM day_report '
                       "WHERE account_id = :account_id AND day = :day",
    "lock.task": "SELECT id, title, status, updated_at FROM task WHERE account_id = :account_id AND id = :task_id",
    "write.task.create": "INSERT INTO task (id, account_id, title, status, updated_at) "
                         "VALUES (:task_id, :account_id, :title, 'open', :now)",
    "write.task.status": "UPDATE task SET status = :status, updated_at = :now "
                         "WHERE account_id = :account_id AND id = :task_id",
    "write.day_report": "INSERT INTO day_report (account_id, day, open_count, done_count) "
                        "VALUES (:account_id, :day, :open_count, :done_count)",
})
STATUS = ["open", "done", "dismissed"]


class Service:
    def __init__(self, path, *, audit=None, content_window=None):
        with closing(sqlite3.connect(path)) as conn:
            conn.executescript(SCHEMA)
        self.read = ReadMapper(path, SQL, audit=audit)
        self.write = WriteMapper(path, SQL, audit=audit)
        self.jobs = Jobs(path)
        self.tools = Tools(audit=audit, content_window=content_window)
        for tool in self._tools():
            self.tools.register(tool)

    # ------------------------------------------------------------------ writes
    def create_task(self, account_id, title, dry_run=False):
        task_id, now = uuid.uuid4().hex, self.write.clock()
        with self.write.transaction("task.create", account_id, dry_run) as conn:
            self.write.run(conn, "write.task.create", account_id=account_id, task_id=task_id, title=title, now=now)
        return {"id": task_id, "updatedAt": now}

    def set_status(self, account_id, task_id, status, *, expected_title=None, expected_updated_at=None, dry_run=False):
        now = self.write.clock()
        with self.write.transaction("task.status", account_id, dry_run) as conn:
            before = self.write.locked(conn, "lock.task", expected_updated_at, account_id=account_id, task_id=task_id)
            if expected_title is not None and before["title"] != expected_title:
                raise MapperError("CONFLICT", "the confirmed title does not match this task")
            self.write.run(conn, "write.task.status", account_id=account_id, task_id=task_id, status=status, now=now)
        return {"id": task_id, "updatedAt": now, "before": before}

    def undo_status(self, account_id, changed):
        """Back to the before-image, only if nobody changed the row after our write."""
        before = changed["before"]
        return self.set_status(account_id, before["id"], before["status"], expected_updated_at=changed["updatedAt"])

    # ------------------------------------------------------------------- tools
    def _tools(self):
        closed = lambda properties, required=(): {"type": "object", "properties": properties,  # noqa: E731
                                                  "required": list(required), "additionalProperties": False}
        task_id = {"type": "string", "pattern": "^[0-9a-f]{32}$", "description": "할 일 ID"}
        yield Tool(
            name="task_list", title="할 일 목록", effect="read",
            description="내 할 일을 상태별로 조회합니다. 결과가 잘렸으면 truncated가 true입니다.",
            input_schema=closed({"status": {"type": "string", "enum": STATUS, "description": "기본 open"}}),
            run=lambda a, account: self.read.page("read.tasks", account_id=account, status=a.get("status", "open")))
        yield Tool(
            name="task_get", title="할 일 한 건", effect="read",
            description="할 일 한 건의 제목·상태·버전(updatedAt)을 조회합니다.",
            input_schema=closed({"task_id": task_id}, ["task_id"]),
            run=lambda a, account: self.read.one("read.task", account_id=account, task_id=a["task_id"]))
        yield Tool(
            name="task_set_status", title="할 일 상태 변경", effect="write",
            description="확인한 할 일의 상태를 바꿉니다. 먼저 task_get으로 읽은 제목과 updatedAt을 그대로 넘깁니다.",
            input_schema=closed({
                "task_id": task_id,
                "expected_title": {"type": "string", "minLength": 1, "maxLength": 300, "description": "읽을 때 본 제목"},
                "expected_updated_at": {"type": "string", "minLength": 1, "maxLength": 40, "description": "읽을 때 본 updatedAt"},
                "status": {"type": "string", "enum": STATUS, "description": "바꿀 상태"},
            }, ["task_id", "expected_title", "expected_updated_at", "status"]),
            questions={"확인": "대상 제목과 바꿀 상태를 보여 주고 사용자가 확인한 경우에만 호출합니다."},
            side_effects="task 한 행의 상태와 updated_at을 바꿉니다.",
            run=lambda a, account: self.set_status(account, a["task_id"], a["status"], expected_title=a["expected_title"],
                                                   expected_updated_at=a["expected_updated_at"]))
        yield Tool(
            name="day_report_get", title="하루 집계 조회", effect="read",
            description="스케줄러가 만들어 둔 그날의 할 일 집계를 조회합니다. 생성은 요청하지 않습니다.",
            input_schema=closed({"day": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$", "description": "KST 날짜"}}, ["day"]),
            run=lambda a, account: self.read.one("read.day_report", account_id=account, day=a["day"]))

    # --------------------------------------------------------------- scheduler
    def tick(self, account_id, day=None):
        """What cron runs. Firing twice on the same day submits one job and writes one report."""
        day = day or datetime.now(KST).date().isoformat()
        self.jobs.submit(account_id, "day_report", day, {"day": day})
        return step(self.jobs, {"day_report": _DayReport(self)})


class _DayReport:
    """Job handler. Here the 'original system' is our own table; for RPA it is the screen's system of record."""

    def __init__(self, service):
        self.service = service

    def find(self, job):
        rows = self.service.read.query("read.day_report", account_id=job["account_id"], day=job["business_key"])
        return rows[0] if rows else None

    def act(self, job):
        account = job["account_id"]
        counts = {row["status"]: row["n"] for row in self.service.read.query("read.task_counts", account_id=account)}
        with self.service.write.transaction("day_report", account) as conn:
            self.service.write.run(conn, "write.day_report", account_id=account, day=job["business_key"],
                                   open_count=counts.get("open", 0), done_count=counts.get("done", 0))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="solo.demo")
    parser.add_argument("--db", default="solo.db")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog").add_argument("format", nargs="?", default="mcp")
    call = commands.add_parser("call")
    call.add_argument("tool")
    call.add_argument("arguments", nargs="?", default="{}")
    call.add_argument("--account", required=True)
    call.add_argument("--request-id")
    tick = commands.add_parser("tick")
    tick.add_argument("--account", required=True)
    tick.add_argument("--day")
    args = parser.parse_args(argv)
    service = Service(args.db)
    if args.command == "catalog":
        out = service.tools.catalog(args.format)
    elif args.command == "call":
        out = service.tools.call(args.tool, json.loads(args.arguments), account_id=args.account, request_id=args.request_id)
    else:
        out = service.tick(args.account, args.day)
    json.dump(out, sys.stdout, ensure_ascii=False, indent=2)
    print()


if __name__ == "__main__":
    main()
