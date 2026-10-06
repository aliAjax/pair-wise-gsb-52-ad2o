import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from app import build_recon_service, build_service
from src.domain import Actor, Conflict, PermissionDenied, ValidationError


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
PLAN_REF = 'IEP-28001'
CONSENT = {'plan_reference': PLAN_REF, 'guardian': 'G-1', 'scope': '个别化服务', 'valid_from': '2026-01-01', 'valid_to': '2026-06-30'}
MANAGER = Actor('clerk-1', 'case_manager')
PARENT = Actor('guardian-1', 'parent_rep')
PROVIDER = Actor('provider-1', 'provider_ops')
ADMIN = Actor('admin-1', 'administrator')


def receipt(external_id, service_date, minutes, plan_reference=PLAN_REF):
    return {'external_id': external_id, 'plan_reference': plan_reference, 'service_date': service_date, 'minutes': minutes}


class ReconTestBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        db_path = str(Path(self.temp.name) / 'test.db')
        self.service = build_service(db_path)
        self.recon = build_recon_service(db_path)
        self.service.create(Actor('creator', 'case_manager'), PLAN_REF, CREATE_DATA)

    def tearDown(self):
        self.temp.cleanup()

    def register_consent(self, **overrides):
        payload = dict(CONSENT, **overrides)
        return self.recon.register_consent(PARENT, payload)

    def submit(self, batch_no, receipts, **extra):
        payload = {'batch_no': batch_no, 'provider': 'SP-3', 'receipts': receipts}
        payload.update(extra)
        return self.recon.submit_batch(PROVIDER, payload)

    def ledger_minutes(self):
        return self.recon.repository.get_ledger(PLAN_REF)['receipt_minutes']

    def posted_events(self):
        return self.recon.repository.events(plan_reference=PLAN_REF, entity_type='receipt')


class ReceiptIdempotencyTest(ReconTestBase):
    def test_receipt_posted_once_by_external_id(self):
        self.register_consent()
        first = self.submit('B-1', [receipt('EXT-1', '2026-03-01', 60), receipt('EXT-2', '2026-03-02', 30)])
        self.assertEqual(first['status'], 'completed')
        self.assertEqual(first['result']['posted'], 2)
        self.assertEqual(self.ledger_minutes(), 90)

        second = self.submit('B-2', [receipt('EXT-1', '2026-03-01', 60), receipt('EXT-3', '2026-03-03', 45)])
        self.assertEqual(second['result']['duplicates'], 1)
        self.assertEqual(second['result']['posted'], 1)
        # 重传的回执不再累计分钟、不再追加审计
        self.assertEqual(self.ledger_minutes(), 135)
        self.assertEqual(len(self.recon.repository.list_receipts(plan_reference=PLAN_REF)), 3)
        self.assertEqual(len([e for e in self.posted_events() if e['action'] == 'receipt_posted']), 3)

        with self.assertRaises(Conflict):
            self.submit('B-1', [receipt('EXT-9', '2026-03-09', 10)])


class ConsentWindowTest(ReconTestBase):
    def test_service_date_scored_only_within_consent_window(self):
        self.register_consent()
        batch = self.submit('B-1', [
            receipt('EXT-IN', '2026-03-01', 60),
            receipt('EXT-OUT', '2026-08-01', 45),
            receipt('EXT-UNKNOWN', '2026-03-01', 15, plan_reference='IEP-99999'),
        ])
        self.assertEqual(batch['result']['posted'], 1)
        self.assertEqual(batch['result']['held'], 2)
        self.assertEqual(self.ledger_minutes(), 60)
        held = {r['external_id']: r['hold_reason'] for r in self.recon.repository.list_receipts(plan_reference=PLAN_REF, statuses=['held'])}
        self.assertEqual(held, {'EXT-OUT': 'no_valid_consent'})

    def test_invalid_receipt_rejected(self):
        self.register_consent()
        with self.assertRaises(ValidationError):
            self.submit('B-1', [receipt('EXT-1', '2026-13-40', 60)])
        with self.assertRaises(ValidationError):
            self.submit('B-1', [receipt('EXT-1', '2026-03-01', 0)])
        with self.assertRaises(PermissionDenied):
            self.recon.submit_batch(Actor('x', 'parent_rep'), {'batch_no': 'B-1', 'provider': 'SP-3', 'receipts': [receipt('EXT-1', '2026-03-01', 60)]})


class WithdrawalTest(ReconTestBase):
    def test_withdrawal_holds_later_receipts_but_keeps_posted_minutes(self):
        consent = self.register_consent()
        self.submit('B-1', [receipt('EXT-1', '2026-03-01', 60)])
        self.assertEqual(self.ledger_minutes(), 60)

        withdrawn = self.recon.withdraw_consent(PARENT, consent['id'], {'effective_date': '2026-03-10'})
        self.assertEqual(withdrawn['status'], 'withdrawn')
        self.assertEqual(withdrawn['version'], 2)

        # 撤回生效日之后的服务待核；生效日之前已发生的服务仍有效
        self.submit('B-2', [receipt('EXT-2', '2026-03-15', 30), receipt('EXT-3', '2026-03-05', 20)])
        view = self.recon.reconciliation(ADMIN, PLAN_REF)
        self.assertEqual(view['receipts'], {'posted': 2, 'held': 1, 'pending': 0})
        # 已入账的累计分钟不被撤回冲掉
        self.assertEqual(view['delivered_minutes'], 120 + 60 + 20)
        self.assertEqual(view['held_minutes'], 30)

        with self.assertRaises(Conflict):
            self.recon.withdraw_consent(PARENT, consent['id'], {'effective_date': '2026-03-10'})


class SnapshotBasisTest(ReconTestBase):
    def test_confirmed_snapshot_keeps_basis_and_unconfirmed_receipts_recalculated(self):
        consent = self.register_consent()
        self.submit('B-1', [receipt('EXT-1', '2026-02-01', 60), receipt('EXT-2', '2026-08-01', 45)])
        snapshot = self.recon.create_snapshot(ADMIN, PLAN_REF)
        self.assertEqual(snapshot['delivered_minutes'], 180)
        self.assertEqual(snapshot['consent_version'], 1)
        self.assertEqual(snapshot['compliance_rate'], 30.0)

        self.recon.withdraw_consent(PARENT, consent['id'], {'effective_date': '2026-03-01'})
        # 已确认快照保留当时依据
        frozen = self.recon.reconciliation(ADMIN, PLAN_REF)['snapshots'][0]
        self.assertEqual(frozen['id'], snapshot['id'])
        self.assertEqual(frozen['delivered_minutes'], 180)
        self.assertEqual(frozen['basis']['consent_version'], 1)
        self.assertEqual(frozen['basis']['consent_status'], 'active')

        # 依据变化：新同意覆盖后，未确认回执失效重算并入账
        self.register_consent(valid_from='2026-07-01', valid_to='2026-12-31')
        view = self.recon.reconciliation(ADMIN, PLAN_REF)
        self.assertEqual(view['receipts']['posted'], 2)
        self.assertEqual(view['delivered_minutes'], 225)
        rescored = [e for e in view['events'] if e['action'] == 'receipt_posted' and e['details'].get('recalculated')]
        self.assertEqual(len(rescored), 1)
        self.assertEqual(rescored[0]['details']['consent_version'], 1)

        updated = self.recon.create_snapshot(ADMIN, PLAN_REF)
        self.assertEqual(updated['delivered_minutes'], 225)
        self.assertEqual(updated['basis']['consent_status'], 'active')
        still_frozen = self.recon.reconciliation(ADMIN, PLAN_REF)['snapshots'][0]
        self.assertEqual(still_frozen['delivered_minutes'], 180)


class BatchConcurrencyTest(ReconTestBase):
    def test_stale_version_claim_is_rejected(self):
        self.register_consent()
        self.recon.repository.create_batch('B-1', 'SP-3', [receipt('EXT-1', '2026-03-01', 60)], 'clerk-1')
        done = self.recon.resume_batch(PROVIDER, 'B-1', {'expected_version': 1})
        self.assertEqual(done['status'], 'completed')
        with self.assertRaises(Conflict):
            self.recon.resume_batch(Actor('clerk-2', 'case_manager'), 'B-1', {'expected_version': 1})

    def test_concurrent_claim_only_first_version_proceeds(self):
        self.register_consent()
        receipts = [receipt('EXT-%d' % i, '2026-03-01', 10) for i in range(20)]
        self.recon.repository.create_batch('B-1', 'SP-3', receipts, 'clerk-1')
        barrier = threading.Barrier(2)
        outcomes = []

        def claim(actor_id):
            barrier.wait()
            try:
                outcomes.append(self.recon.repository.claim_batch('B-1', 1, actor_id))
            except Conflict:
                outcomes.append('conflict')

        threads = [threading.Thread(target=claim, args=('clerk-%d' % i,)) for i in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(outcomes.count('conflict'), 1)
        self.assertEqual(sum(1 for o in outcomes if isinstance(o, dict)), 1)

    def test_resume_with_mismatched_payload_is_rejected(self):
        self.register_consent()
        self.recon.repository.create_batch('B-1', 'SP-3', [receipt('EXT-1', '2026-03-01', 60)], 'clerk-1')
        with self.assertRaises(Conflict):
            self.recon.resume_batch(PROVIDER, 'B-1', {'expected_version': 1, 'receipts': [receipt('EXT-2', '2026-03-01', 60)]})


class FailureRecoveryTest(ReconTestBase):
    def test_resume_from_checkpoint_without_double_counting(self):
        self.register_consent()
        receipts = [receipt('EXT-%d' % i, '2026-03-0%d' % (i + 1), 10 * (i + 1)) for i in range(4)]
        original = self.recon.repository.process_receipt
        calls = {'n': 0}

        def flaky(*args, **kwargs):
            calls['n'] += 1
            if calls['n'] == 3:
                raise sqlite3.OperationalError('模拟写库失败')
            return original(*args, **kwargs)

        self.recon.repository.process_receipt = flaky
        with self.assertRaises(sqlite3.OperationalError):
            self.submit('B-1', receipts)
        self.recon.repository.process_receipt = original

        interrupted = self.recon.get_batch(PROVIDER, 'B-1')
        self.assertEqual(interrupted['status'], 'failed')
        self.assertEqual(interrupted['processed'], 2)
        self.assertEqual(interrupted['checkpoint_seq'], 1)
        self.assertEqual(self.ledger_minutes(), 30)

        resumed = self.recon.resume_batch(PROVIDER, 'B-1', {'expected_version': interrupted['version']})
        self.assertEqual(resumed['status'], 'completed')
        self.assertEqual(resumed['result']['posted'], 2)
        # 重试未完成回执：分钟只累计一次，审计不重复追加
        self.assertEqual(self.ledger_minutes(), 100)
        self.assertEqual(len(self.recon.repository.list_receipts(plan_reference=PLAN_REF)), 4)
        self.assertEqual(len([e for e in self.posted_events() if e['action'] == 'receipt_posted']), 4)
        view = self.recon.reconciliation(ADMIN, PLAN_REF)
        self.assertEqual(view['delivered_minutes'], 220)
        self.assertEqual(view['compliance_rate'], round(220 / 600 * 100, 2))


if __name__ == '__main__':
    unittest.main()
