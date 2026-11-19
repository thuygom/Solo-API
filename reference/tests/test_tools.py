"""Tool registry contract: closed inputs, host-decided account, replay-safe writes, envelopes."""
import os
import tempfile
import threading
import time
import unittest

from solo.demo import Service
from solo.tools import Tool, Tools

A, B = "acct-a", "acct-b"
CLOSED = {"type": "object", "properties": {}, "additionalProperties": False}


class ToolTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.audit = []
        self.service = Service(os.path.join(self.dir.name, "t.db"), audit=self.audit.append)
        self.call = self.service.tools.call
        self.task = self.service.create_task(A, "견적 확인")

    def tearDown(self):
        self.dir.cleanup()

    def change(self, **over):
        args = {"task_id": self.task["id"], "expected_title": "견적 확인",
                "expected_updated_at": self.task["updatedAt"], "status": "done"}
        return {**args, **over}

    def test_registration_rules(self):
        tools = Tools()
        base = dict(name="t", title="t", description="d", effect="read", run=lambda a, account: None)
        with self.assertRaises(ValueError):  # open object
            tools.register(Tool(**base, input_schema={"type": "object", "properties": {}}))
        with self.assertRaises(ValueError):  # the model must not choose the account
            tools.register(Tool(**base, input_schema={"type": "object", "additionalProperties": False,
                                                      "properties": {"account_id": {"type": "string"}}}))
        with self.assertRaises(ValueError):  # a question about something that is not an input
            tools.register(Tool(**base, questions={"guidance": "방향"},
                                input_schema={"type": "object", "additionalProperties": False, "properties": {}}))

    def test_catalog_formats_share_one_definition(self):
        mcp = self.service.tools.catalog("mcp")["tools"]
        self.assertEqual([t["name"] for t in mcp], ["task_list", "task_get", "task_set_status", "day_report_get"])
        write = next(t for t in mcp if t["name"] == "task_set_status")
        self.assertFalse(write["annotations"]["readOnlyHint"])
        self.assertIn("Question", write["description"])
        self.assertNotIn("account_id", write["inputSchema"]["properties"])
        self.assertEqual(self.service.tools.catalog("openai")[0]["function"]["parameters"], mcp[0]["inputSchema"])
        self.assertEqual(self.service.tools.catalog("anthropic")[0]["input_schema"], mcp[0]["inputSchema"])

    def test_read_is_scoped_to_the_host_account(self):
        self.assertEqual(self.call("task_get", {"task_id": self.task["id"]}, account_id=A)["data"]["title"], "견적 확인")
        other = self.call("task_get", {"task_id": self.task["id"]}, account_id=B)
        self.assertEqual((other["ok"], other["error"]["code"]), (False, "NOT_FOUND"))
        self.assertTrue(other["error"]["next_action"])
        with self.assertRaises(ValueError):
            self.call("task_get", {"task_id": self.task["id"]}, account_id="")

    def test_invalid_input_is_refused_before_anything_runs(self):
        for arguments in ({}, {"task_id": self.task["id"], "account_id": B}, {"task_id": "not-an-id"}, {"task_id": 7},
                          {"task_id": self.task["id"] + "\n"}):  # '$' must not accept a trailing newline
            self.assertEqual(self.call("task_get", arguments, account_id=A)["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.call("task_list", {"status": "deleted"}, account_id=A)["error"]["code"], "INVALID_INPUT")
        self.assertEqual(self.call("nope", {}, account_id=A)["error"]["code"], "UNKNOWN_TOOL")

    def test_write_needs_request_id_and_replays_the_same_intent(self):
        self.assertEqual(self.call("task_set_status", self.change(), account_id=A)["error"]["code"], "INVALID_INPUT")
        first = self.call("task_set_status", self.change(), account_id=A, request_id="r-1")
        self.assertTrue(first["ok"])
        again = self.call("task_set_status", self.change(), account_id=A, request_id="r-1")
        self.assertEqual(again["data"], first["data"])  # not a version conflict: the stored result comes first
        self.assertTrue(again["meta"]["replayed"])
        other = self.call("task_set_status", self.change(status="dismissed"), account_id=A, request_id="r-1")
        self.assertEqual(other["error"]["code"], "IDEMPOTENCY_CONFLICT")
        stale = self.call("task_set_status", self.change(status="dismissed"), account_id=A, request_id="r-2")
        self.assertEqual(stale["error"]["code"], "CONFLICT")
        wrong = self.call("task_set_status", self.change(expected_title="다른 제목", expected_updated_at=first["data"]["updatedAt"]),
                          account_id=A, request_id="r-3")
        self.assertEqual(wrong["error"]["code"], "CONFLICT")

    def test_failure_is_never_an_empty_result(self):
        calls = []

        def broken(arguments, account):
            calls.append(account)
            raise RuntimeError("connection reset")

        tools = Tools()
        closed = {"type": "object", "properties": {}, "additionalProperties": False}
        tools.register(Tool(name="r", title="r", description="d", effect="read", input_schema=closed, run=broken))
        tools.register(Tool(name="w", title="w", description="d", effect="write", input_schema=closed, run=broken))
        tools.register(Tool(name="h", title="h", description="d", effect="read", input_schema=closed, run=broken,
                            hold_reason="API가 아직 없습니다."))
        self.assertEqual(tools.call("r", {}, account_id=A)["error"]["code"], "BACKEND_UNAVAILABLE")
        self.assertEqual(tools.call("w", {}, account_id=A, request_id="r-1")["error"]["code"], "OUTCOME_UNKNOWN")
        # The same intent is not sent a second time while its outcome is unknown.
        self.assertEqual(tools.call("w", {}, account_id=A, request_id="r-1")["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(calls), 2)
        self.assertEqual(tools.call("h", {}, account_id=A)["error"]["code"], "TOOL_DISABLED")
        self.assertIn("[실행 불가]", tools.catalog("anthropic")[2]["description"])

    def test_concurrent_calls_with_one_request_id_write_once(self):
        runs, outs = [], []

        def slow(arguments, account):
            time.sleep(0.2)
            runs.append(1)
            return {"n": len(runs)}

        tools = Tools()
        tools.register(Tool(name="w", title="w", description="d", effect="write", input_schema=CLOSED, run=slow))
        threads = [threading.Thread(target=lambda: outs.append(tools.call("w", {}, account_id=A, request_id="r-1")))
                   for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(runs), 1)
        self.assertEqual(sum(o["ok"] for o in outs), 1)
        self.assertEqual({o["error"]["code"] for o in outs if not o["ok"]}, {"OPERATION_IN_PROGRESS"})
        self.assertTrue(tools.call("w", {}, account_id=A, request_id="r-1")["meta"]["replayed"])

    def test_content_window_for_clients_without_request_id(self):
        now, made, broken = [0.0], [], []
        one = {"type": "object", "properties": {"title": {"type": "string", "maxLength": 50}},
               "required": ["title"], "additionalProperties": False}

        def fail(arguments, account):
            broken.append(1)
            raise RuntimeError("connection reset")

        tools = Tools(content_window=600, clock=lambda: now[0])
        tools.register(Tool(name="create", title="c", description="d", effect="write", input_schema=one,
                            run=lambda a, account: made.append(a["title"]) or {"n": len(made)}))
        tools.register(Tool(name="flaky", title="f", description="d", effect="write", input_schema=CLOSED, run=fail))
        first = tools.call("create", {"title": "견적 확인"}, account_id=A)
        now[0] = 300
        self.assertTrue(tools.call("create", {"title": "견적 확인"}, account_id=A)["meta"]["replayed"])  # a retry
        self.assertEqual(tools.call("create", {"title": "다른 일"}, account_id=A)["data"], {"n": 2})
        now[0] = 601
        self.assertEqual(tools.call("create", {"title": "견적 확인"}, account_id=A)["data"], {"n": 3})  # a new intent
        self.assertEqual((first["data"], len(made)), ({"n": 1}, 3))
        self.assertEqual(tools.call("flaky", {}, account_id=A)["error"]["code"], "OUTCOME_UNKNOWN")
        now[0] = 10_000  # an unknown outcome is never retried by the window running out
        self.assertEqual(tools.call("flaky", {}, account_id=A)["error"]["code"], "OUTCOME_UNKNOWN")
        self.assertEqual(len(broken), 1)

    def test_full_contract_and_generate_effect(self):
        full = {t["name"]: t for t in self.service.tools.catalog("full")}
        self.assertEqual(full["task_set_status"]["side_effects"], "task 한 행의 상태와 updated_at을 바꿉니다.")
        self.assertEqual(full["task_get"]["effect"], "read")
        tools = Tools()
        tools.register(Tool(name="g", title="g", description="d", effect="generate", input_schema=CLOSED,
                            run=lambda a, account: {"draft": "초안"}))
        out = tools.call("g", {}, account_id=A)  # a draft changes nothing, so it needs no request_id
        self.assertEqual(out["data"], {"draft": "초안"})
        self.assertFalse(tools.catalog("mcp")["tools"][0]["annotations"]["readOnlyHint"])

    def test_audit_has_no_arguments(self):
        self.call("task_get", {"task_id": self.task["id"]}, account_id=A)
        entry = next(e for e in self.audit if e.get("tool") == "task_get")
        self.assertEqual((entry["account_id"], entry["code"]), (A, "OK"))
        self.assertNotIn(self.task["id"], repr(entry))


if __name__ == "__main__":
    unittest.main()
