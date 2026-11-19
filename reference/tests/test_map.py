"""Read and write mapper contract."""
import os
import tempfile
import unittest

from solo.demo import SQL, Service
from solo.mapper import MapperError
from solo.sqlreg import params, registry


class MapperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.audit = []
        self.service = Service(os.path.join(self.dir.name, "t.db"), audit=self.audit.append)

    def tearDown(self):
        self.dir.cleanup()

    def test_registry_refuses_unscoped_statement(self):
        with self.assertRaises(ValueError):
            registry({"read.all": "SELECT id FROM task"})
        self.assertEqual(params("SELECT 1 WHERE a = :account_id AND b = :task_id"), {"account_id", "task_id"})

    def test_only_registered_read_statements_with_exact_parameters(self):
        read = self.service.read
        with self.assertRaises(ValueError):
            read.query("SELECT * FROM task", account_id="a")
        with self.assertRaises(ValueError):  # a write statement through the read mapper
            read.query("write.task.status", account_id="a", task_id="x", status="done", now="n")
        with self.assertRaises(ValueError):  # a parameter the statement does not name
            read.query("read.task", account_id="a", task_id="x", status="open")
        with self.assertRaises(ValueError):
            read.query("read.task", account_id="", task_id="x")
        self.assertTrue(set(SQL) >= {"read.task", "lock.task", "write.task.create"})

    def test_other_account_looks_like_missing(self):
        made = self.service.create_task("acct-a", "견적 확인")
        self.assertEqual(self.service.read.one("read.task", account_id="acct-a", task_id=made["id"])["title"], "견적 확인")
        with self.assertRaises(MapperError) as caught:
            self.service.read.one("read.task", account_id="acct-b", task_id=made["id"])
        self.assertEqual(caught.exception.code, "NOT_FOUND")
        with self.assertRaises(MapperError) as caught:
            self.service.set_status("acct-b", made["id"], "done")
        self.assertEqual(caught.exception.code, "NOT_FOUND")

    def test_list_is_capped_and_marked(self):
        self.service.read.cap = 2
        for title in ("a", "b", "c"):
            self.service.create_task("acct-a", title)
        page = self.service.read.page("read.tasks", account_id="acct-a", status="open")
        self.assertEqual((len(page["items"]), page["truncated"]), (2, True))

    def test_version_conflict_before_image_and_undo(self):
        made = self.service.create_task("acct-a", "견적 확인")
        changed = self.service.set_status("acct-a", made["id"], "done", expected_updated_at=made["updatedAt"])
        self.assertEqual(changed["before"]["status"], "open")
        with self.assertRaises(MapperError) as caught:  # the version read before the change is now stale
            self.service.set_status("acct-a", made["id"], "dismissed", expected_updated_at=made["updatedAt"])
        self.assertEqual(caught.exception.code, "CONFLICT")
        self.service.undo_status("acct-a", changed)
        self.assertEqual(self.service.read.one("read.task", account_id="acct-a", task_id=made["id"])["status"], "open")
        with self.assertRaises(MapperError):  # undo applies only to the version our write made
            self.service.undo_status("acct-a", changed)

    def test_dry_run_rolls_back(self):
        made = self.service.create_task("acct-a", "견적 확인")
        preview = self.service.set_status("acct-a", made["id"], "done", dry_run=True)
        self.assertEqual(preview["before"]["status"], "open")
        self.assertEqual(self.service.read.one("read.task", account_id="acct-a", task_id=made["id"])["status"], "open")
        self.service.create_task("acct-a", "저장되지 않는 할 일", dry_run=True)
        self.assertEqual(len(self.service.read.page("read.tasks", account_id="acct-a", status="open")["items"]), 1)

    def test_audit_has_ids_and_counts_but_no_values(self):
        made = self.service.create_task("acct-a", "비밀 견적")
        self.service.read.one("read.task", account_id="acct-a", task_id=made["id"])
        text = repr(self.audit)
        self.assertIn("read.task", text)
        self.assertIn("task.create", text)
        self.assertNotIn("비밀 견적", text)
        self.assertNotIn(made["id"], text)


if __name__ == "__main__":
    unittest.main()
