import tempfile
import threading
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, BatchInterrupted, Conflict, PermissionDenied, ValidationError


def create_data(**overrides):
    data = {
        'student_id': 'S-200', 'disability': 'autism', 'service_minutes': 600,
        'delivered_minutes': 0, 'review_due_days': 15, 'goals_count': 3, 'consent': False,
    }
    data.update(overrides)
    return data


class ReconciliationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))
        self.cm = Actor("cm", "case_manager")
        self.parent = Actor("mom", "parent_rep")
        self.specialist = Actor("sp", "specialist")
        self.admin = Actor("adm", "administrator")
        record = self.service.create(self.cm, "IEP-30001", create_data())
        record = self.service.act(
            self.parent, record["id"], record["version"], "consent",
            {"guardian_confirmed": True, "consent_scope": "全部", "effective_from": "2026-06-01"},
        )
        record = self.service.act(self.cm, record["id"], record["version"], "activate", {})
        self.record_id = record["id"]
        self.version = record["version"]

    def tearDown(self):
        self.temp.cleanup()

    def active(self):
        return self.service.get_record(self.cm, self.record_id)

    def test_receipt_in_consent_window_posts_minutes_once(self):
        summary = self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
            {"external_id": "RX-2", "minutes": 50, "service_date": "2026-06-10"},
        ])
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["posted"], 2)
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 150)
        self.assertEqual(record["payload"]["delivered_minutes"], 150)
        self.assertEqual(record["payload"]["compliance_rate"], 25.0)

    def test_service_outside_consent_window_is_pending_and_not_scored(self):
        summary = self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-05-20"},
            {"external_id": "RX-2", "minutes": 50, "service_date": "2026-05-25"},
        ])
        self.assertEqual(summary["pending"], 2)
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 0)
        self.assertEqual(record["payload"]["pending_minutes"], 150)
        self.assertEqual(record["payload"]["delivered_minutes"], 0)
        receipts = {r["external_id"]: r for r in self.service.list_receipts(self.cm, self.record_id)}
        self.assertEqual(receipts["RX-1"]["reason"], "before_consent")
        self.assertEqual(receipts["RX-2"]["reason"], "before_consent")

    def test_service_on_withdrawal_day_is_outside_window(self):
        # 撤回日当天起不再计入，半开区间 [生效日, 撤回日)。
        summary = self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-30"},
        ])
        self.assertEqual(summary["posted"], 1)
        record = self.active()
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-06-30"},
        )
        record = self.service.act(
            self.parent, record["id"], record["version"], "consent",
            {"guardian_confirmed": True, "consent_scope": "全部", "effective_from": "2026-07-01"},
        )
        self.service.submit_batch(self.specialist, record["id"], record["version"], "B-2", [
            {"external_id": "RX-2", "minutes": 50, "service_date": "2026-06-30"},
        ])
        receipt = next(r for r in self.service.list_receipts(self.cm, self.record_id) if r["external_id"] == "RX-2")
        self.assertEqual(receipt["status"], "pending")

    def test_duplicate_retransmission_never_double_counts_or_audits(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
        ])
        record = self.active()
        timeline_before = len(self.service.timeline(self.cm, self.record_id))
        # 服务商整批重传：新批次里携带相同外部编号。
        summary = self.service.submit_batch(self.specialist, record["id"], record["version"], "B-2", [
            {"external_id": "RX-1", "minutes": 999, "service_date": "2026-06-05"},
        ])
        self.assertEqual(summary["duplicate"], 1)
        self.assertEqual(summary["posted"], 0)
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 100)
        self.assertEqual(record["payload"]["delivered_minutes"], 100)
        timeline_after = self.service.timeline(self.cm, self.record_id)
        self.assertEqual(len(timeline_after), timeline_before)
        receipt = next(r for r in timeline_after if r["action"] == "receipt_posted")
        self.assertEqual(receipt["details"]["minutes"], 100)

    def test_withdraw_invalidates_pending_but_keeps_posted_service(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
            {"external_id": "RX-2", "minutes": 30, "service_date": "2026-05-20"},
        ])
        record = self.active()
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-07-01"},
        )
        payload = record["payload"]
        self.assertEqual(payload["posted_minutes"], 100)
        self.assertEqual(payload["invalid_minutes"], 30)
        self.assertEqual(payload["pending_minutes"], 0)
        self.assertEqual(payload["delivered_minutes"], 100)  # 原入账服务不冲掉
        statuses = {r["external_id"]: r["status"] for r in self.service.list_receipts(self.cm, self.record_id)}
        self.assertEqual(statuses, {"RX-1": "posted", "RX-2": "invalid"})

    def test_reconsent_backdate_reevaluates_invalid_receipt(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 30, "service_date": "2026-05-20"},
        ])
        record = self.active()
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-07-01"},
        )
        self.assertEqual(self.active()["payload"]["invalid_minutes"], 30)
        # 监护人补签同意，生效日回溯到服务之前：失效回执重新计分。
        record = self.service.act(
            self.parent, record["id"], record["version"], "consent",
            {"guardian_confirmed": True, "consent_scope": "全部", "effective_from": "2026-05-01"},
        )
        self.assertEqual(record["payload"]["posted_minutes"], 30)
        self.assertEqual(record["payload"]["invalid_minutes"], 0)
        self.assertEqual(record["payload"]["delivered_minutes"], 30)

    def test_review_snapshot_unconfirmed_goes_stale_when_basis_changes(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
            {"external_id": "RX-2", "minutes": 30, "service_date": "2026-05-20"},
        ])
        record = self.active()
        record = self.service.act(self.admin, record["id"], record["version"], "review", {"progress_note": "复查"})
        snapshots = self.service.snapshots(self.cm, self.record_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["status"], "unconfirmed")
        self.assertEqual(snapshots[0]["basis"]["posted_minutes"], 100)
        # 复查中监护人迟到撤回：未确认快照失效。
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-07-01"},
        )
        self.assertEqual(self.service.snapshots(self.cm, self.record_id)[0]["status"], "stale")

    def test_confirmed_snapshot_freezes_basis_against_later_changes(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
            {"external_id": "RX-2", "minutes": 30, "service_date": "2026-05-20"},
        ])
        record = self.active()
        record = self.service.act(self.admin, record["id"], record["version"], "review", {"progress_note": "复查"})
        frozen = self.service.snapshots(self.cm, self.record_id)[0]["basis"]
        record = self.service.act(
            self.admin, record["id"], record["version"], "confirm_snapshot", {"snapshot_confirmed": True}
        )
        # 撤回后补同意：新依据让 RX-2 计分，但已确认快照保留当时依据。
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-07-01"},
        )
        record = self.service.act(
            self.parent, record["id"], record["version"], "consent",
            {"guardian_confirmed": True, "consent_scope": "全部", "effective_from": "2026-05-01"},
        )
        snapshots = self.service.snapshots(self.cm, self.record_id)
        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0]["status"], "confirmed")
        self.assertEqual(snapshots[0]["basis"], frozen)
        self.assertEqual(snapshots[0]["basis"]["posted_minutes"], 100)
        self.assertEqual(snapshots[0]["basis"]["posted_receipt_count"], 1)
        # 快照外的 RX-2 仍按新依据重算。
        self.assertEqual(record["payload"]["posted_minutes"], 130)

    def test_confirmed_snapshot_locks_its_posted_receipts(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
        ])
        record = self.active()
        record = self.service.act(self.admin, record["id"], record["version"], "review", {"progress_note": "复查"})
        record = self.service.act(
            self.admin, record["id"], record["version"], "confirm_snapshot", {"snapshot_confirmed": True}
        )
        # 已确认快照内的服务即使撤回也不被重算冲掉。
        record = self.service.act(
            self.parent, record["id"], record["version"], "withdraw_consent",
            {"withdrawn_on": "2026-06-10"},
        )
        receipt = next(r for r in self.service.list_receipts(self.cm, self.record_id) if r["external_id"] == "RX-1")
        self.assertEqual(receipt["status"], "posted")
        self.assertEqual(record["payload"]["posted_minutes"], 100)
        self.assertEqual(record["payload"]["delivered_minutes"], 100)

    def test_concurrent_same_batch_only_first_wins(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def submit():
            barrier.wait()
            try:
                self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
                    {"external_id": "RX-%s" % threading.current_thread().name, "minutes": 10,
                     "service_date": "2026-06-05"},
                ])
                outcomes.append("ok")
            except Conflict:
                outcomes.append("conflict")

        threads = [threading.Thread(target=submit, name=str(i)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(sorted(outcomes), ["conflict", "ok"])
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 10)

    def test_batch_resumes_from_last_checkpoint_without_double_counting(self):
        fired = {"n": 0}

        def hook(batch_id, seq):
            fired["n"] += 1
            if fired["n"] == 2:
                raise RuntimeError("模拟写库失败")

        self.service.repository.fault_hook = hook
        with self.assertRaises(BatchInterrupted) as ctx:
            self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
                {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
                {"external_id": "RX-2", "minutes": 50, "service_date": "2026-06-06"},
                {"external_id": "RX-3", "minutes": 25, "service_date": "2026-06-07"},
            ])
        self.assertEqual(ctx.exception.checkpoint, 1)
        # 第一条完整提交，第二条整体回滚。
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 100)
        self.service.repository.fault_hook = None
        summary = self.service.resume_batch(self.specialist, "B-1")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(summary["resumed_from"], 1)
        self.assertEqual(summary["posted"], 2)
        record = self.active()
        self.assertEqual(record["payload"]["posted_minutes"], 175)
        receipts = self.service.list_receipts(self.cm, self.record_id)
        self.assertEqual(len(receipts), 3)
        posted_audits = [e for e in self.service.timeline(self.cm, self.record_id) if e["action"] == "receipt_posted"]
        self.assertEqual(len(posted_audits), 3)

    def test_resume_completed_batch_is_idempotent(self):
        self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
            {"external_id": "RX-1", "minutes": 100, "service_date": "2026-06-05"},
        ])
        timeline_before = len(self.service.timeline(self.cm, self.record_id))
        summary = self.service.resume_batch(self.specialist, "B-1")
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(len(self.service.timeline(self.cm, self.record_id)), timeline_before)

    def test_invalid_batch_payload_rejected(self):
        with self.assertRaises(ValidationError):
            self.service.submit_batch(self.specialist, self.record_id, self.version, "B-1", [
                {"external_id": "RX-1", "minutes": 0, "service_date": "2026-06-05"},
            ])
        with self.assertRaises(ValidationError):
            self.service.submit_batch(self.specialist, self.record_id, self.version, "B-2", [
                {"external_id": "RX-1", "minutes": 10, "service_date": "2026/06/05"},
            ])
        with self.assertRaises(ValidationError):
            self.service.submit_batch(self.specialist, self.record_id, self.version, "B-3", [
                {"external_id": "RX-1", "minutes": 10, "service_date": "2026-06-05"},
                {"external_id": "RX-1", "minutes": 20, "service_date": "2026-06-06"},
            ])
        # 校验失败不留批次、不推进版本。
        self.assertEqual(self.active()["version"], self.version)

    def test_specialist_role_required_for_batches(self):
        with self.assertRaises(PermissionDenied):
            self.service.submit_batch(self.parent, self.record_id, self.version, "B-1", [
                {"external_id": "RX-1", "minutes": 10, "service_date": "2026-06-05"},
            ])

    def test_stale_version_batch_rejected(self):
        with self.assertRaises(Conflict):
            self.service.submit_batch(self.specialist, self.record_id, self.version - 1, "B-1", [
                {"external_id": "RX-1", "minutes": 10, "service_date": "2026-06-05"},
            ])


if __name__ == "__main__":
    unittest.main()
