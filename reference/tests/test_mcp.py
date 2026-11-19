"""The MCP adapter serves the same tools over stdio. Skipped when the SDK is not installed."""
import asyncio
import importlib.util
import os
from pathlib import Path
import sys
import tempfile
import unittest

from solo.demo import Service

ROOT = Path(__file__).resolve().parent.parent
REVISION = "2026-07-28"


@unittest.skipUnless(importlib.util.find_spec("mcp"), 'mcp SDK is not installed (pip install -e ".[mcp]")')
class McpTest(unittest.TestCase):
    def test_stdio_lists_and_calls_the_registry(self):
        from mcp.client import Client
        from mcp.client.stdio import StdioServerParameters

        with tempfile.TemporaryDirectory() as folder:
            db = os.path.join(folder, "t.db")
            service = Service(db)
            mine = service.create_task("acct-a", "견적 확인")
            theirs = service.create_task("acct-b", "남의 할 일")

            async def run():
                params = StdioServerParameters(command=sys.executable, args=["-m", "solo.mcpio", "--db", db], cwd=str(ROOT),
                                               env={**os.environ, "SOLO_ACCOUNT_ID": "acct-a", "PYTHONUNBUFFERED": "1"})
                async with Client(params, mode=REVISION) as client:
                    listed = {t.name for t in (await client.list_tools()).tools}
                    got = await client.call_tool("task_get", {"task_id": mine["id"]})
                    other = await client.call_tool("task_get", {"task_id": theirs["id"]})
                    forged = await client.call_tool("task_get", {"task_id": theirs["id"], "account_id": "acct-b"})
                    change = {"task_id": mine["id"], "expected_title": "견적 확인",
                              "expected_updated_at": mine["updatedAt"], "status": "done"}
                    first = await client.call_tool("task_set_status", change)
                    again = await client.call_tool("task_set_status", change)
                    return client.protocol_version, listed, got, other, forged, first, again

            version, listed, got, other, forged, first, again = asyncio.run(run())
            self.assertEqual(version, REVISION)
            self.assertEqual(listed, {"task_list", "task_get", "task_set_status", "day_report_get"})
            self.assertFalse(got.is_error)
            self.assertEqual(got.structured_content["data"]["title"], "견적 확인")
            self.assertTrue(other.is_error)
            self.assertEqual(other.structured_content["error"]["code"], "NOT_FOUND")
            self.assertTrue(forged.is_error)  # the account is not an input; the forged field is refused
            self.assertFalse(first.is_error)
            self.assertTrue(again.structured_content["meta"]["replayed"])
            self.assertEqual(service.read.one("read.task", account_id="acct-a", task_id=mine["id"])["status"], "done")


if __name__ == "__main__":
    unittest.main()
