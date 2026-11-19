"""Job contract: one job per business key, unknown is never re-run, the scheduler can fire twice."""
from datetime import datetime, timedelta, timezone
import os
import tempfile
import unittest

from solo.demo import Service
from solo.jobs import Jobs, NoEffect, step
from solo.mapper import MapperError

A = "acct-a"


class Erp:
    """A stand-in for a system that only has a screen. `saved` is its system of record."""

    def __init__(self, fail=None, lose_reply=False):
        self.saved, self.actions, self.fail, self.lose_reply = {}, 0, fail, lose_reply

    def find(self, job):
        return self.saved.get(job["business_key"])

    def act(self, job):
        self.actions += 1
        if self.fail:
            raise self.fail
        self.saved[job["business_key"]] = {"voucher_no": f"V-{len(self.saved) + 1}"}
        if self.lose_reply:
            raise TimeoutError("saved, then the session dropped")


class JobTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.now = datetime(2026, 11, 19, 8, 0, tzinfo=timezone.utc)
        self.path = os.path.join(self.dir.name, "t.db")
        self.jobs = Jobs(self.path, clock=lambda: self.now)

    def tearDown(self):
        self.dir.cleanup()

    def submit(self, key="DOC-2026-001"):
        return self.jobs.submit(A, "invoice", key, {"vendor": "V-20", "amount_minor": 100000, "currency": "KRW"})

    def test_same_business_key_is_one_job(self):
        first, second = self.submit(), self.submit()
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["state"], "queued")
        with self.assertRaises(MapperError):
            self.jobs.get(first["id"], "acct-b")

    def test_success_needs_the_original_system_to_confirm(self):
        erp, job = Erp(), self.submit()
        done = step(self.jobs, {"invoice": erp})
        self.assertEqual((done["state"], done["result"]), ("succeeded", {"voucher_no": "V-1"}))
        self.assertIsNone(step(self.jobs, {"invoice": erp}))
        self.assertEqual(self.jobs.get(job["id"], A)["state"], "succeeded")

    def test_lost_reply_after_save_is_confirmed_by_lookup(self):
        erp = Erp(lose_reply=True)
        self.submit()
        done = step(self.jobs, {"invoice": erp})
        self.assertEqual((done["state"], done["result"], erp.actions), ("succeeded", {"voucher_no": "V-1"}, 1))

    def test_unknown_is_not_rerun_and_is_reconciled_by_a_lookup(self):
        erp, job = Erp(fail=TimeoutError("no reply")), self.submit()
        self.assertEqual(step(self.jobs, {"invoice": erp})["state"], "unknown")
        self.assertIsNone(step(self.jobs, {"invoice": erp}))  # unknown is not picked up again
        self.assertEqual(erp.actions, 1)
        self.assertEqual(self.jobs.reconcile(job["id"], None)["state"], "failed")  # checked: nothing was saved
        with self.assertRaises(MapperError):
            self.jobs.reconcile(job["id"], {"voucher_no": "V-9"})

    def test_confirmed_no_effect_is_failed(self):
        self.submit()
        self.assertEqual(step(self.jobs, {"invoice": Erp(fail=NoEffect("rejected before save"))})["state"], "failed")

    def test_already_processed_is_recorded_without_acting(self):
        erp = Erp()
        erp.saved["DOC-2026-001"] = {"voucher_no": "V-7"}
        self.submit()
        done = step(self.jobs, {"invoice": erp})
        self.assertEqual((done["state"], done["result"], erp.actions), ("succeeded", {"voucher_no": "V-7"}, 0))

    def test_expired_lease_becomes_unknown_and_cancel_only_before_claim(self):
        job = self.submit()
        other = self.submit("DOC-2026-002")
        self.assertEqual(self.jobs.claim(lease_seconds=60)["id"], job["id"])
        with self.assertRaises(MapperError):
            self.jobs.cancel(job["id"], A)
        self.assertEqual(self.jobs.cancel(other["id"], A)["state"], "cancelled")
        self.now += timedelta(seconds=61)
        self.assertEqual(self.jobs.expire(), 1)
        self.assertEqual(self.jobs.get(job["id"], A)["state"], "unknown")
        self.assertIsNone(self.jobs.claim())

    def test_failed_or_cancelled_key_runs_again_but_unknown_does_not(self):
        job = self.submit()
        self.assertEqual(step(self.jobs, {"invoice": Erp(fail=NoEffect("rejected"))})["state"], "failed")
        again = self.submit()
        self.assertEqual((again["id"], again["state"]), (job["id"], "queued"))  # same job, back in the queue
        self.assertEqual(step(self.jobs, {"invoice": Erp()})["state"], "succeeded")
        other = self.submit("DOC-2026-002")
        self.jobs.cancel(other["id"], A)
        self.assertEqual(self.submit("DOC-2026-002")["state"], "queued")
        self.jobs.claim()  # a worker holds DOC-2026-002, so the next turn takes DOC-2026-003
        lost = self.submit("DOC-2026-003")
        self.assertEqual(step(self.jobs, {"invoice": Erp(fail=TimeoutError("no reply"))})["state"], "unknown")
        self.assertEqual(self.submit("DOC-2026-003")["state"], "unknown")  # reconcile first
        self.assertEqual(self.jobs.reconcile(lost["id"], None)["state"], "failed")
        self.assertEqual(self.submit("DOC-2026-003")["state"], "queued")

    def test_worker_turn_expires_a_dead_workers_lease(self):
        job = self.submit()
        self.jobs.claim(lease_seconds=60)  # the worker dies here
        self.now += timedelta(hours=3)
        self.assertIsNone(step(self.jobs, {"invoice": Erp()}))
        self.assertEqual(self.jobs.get(job["id"], A)["state"], "unknown")

    def test_scheduler_reruns_a_day_whose_job_failed(self):
        service = Service(self.path)
        service.jobs.submit(A, "day_report", "2026-11-19", {"day": "2026-11-19"})
        failed = step(service.jobs, {"day_report": Erp(fail=NoEffect("db was read-only"))})
        self.assertEqual(failed["state"], "failed")
        self.assertEqual(service.tick(A, "2026-11-19")["state"], "succeeded")

    def test_scheduler_firing_twice_writes_one_report(self):
        service = Service(self.path)
        service.create_task(A, "견적 확인")
        done = service.set_status(A, service.create_task(A, "계약서 검토")["id"], "done")
        self.assertTrue(done["updatedAt"])
        first = service.tick(A, "2026-11-19")
        self.assertEqual((first["state"], first["result"]), ("succeeded", {"day": "2026-11-19", "openCount": 1, "doneCount": 1}))
        self.assertIsNone(service.tick(A, "2026-11-19"))
        report = service.tools.call("day_report_get", {"day": "2026-11-19"}, account_id=A)
        self.assertEqual(report["data"]["openCount"], 1)
        self.assertEqual(service.tools.call("day_report_get", {"day": "2026-11-20"}, account_id=A)["error"]["code"], "NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
