import tempfile
import unittest
from pathlib import Path

from app import build_service
from src.domain import Actor, Conflict, PermissionDenied


CREATE_DATA = {'student_id': 'S-100', 'disability': 'hearing', 'service_minutes': 600, 'delivered_minutes': 120, 'review_due_days': 15, 'goals_count': 4, 'consent': False}
FLOW = [('consent', 'parent_rep', {'guardian_confirmed': True, 'consent_scope': '个别化服务'}, 'consented'), ('activate', 'case_manager', {}, 'active'), ('log_service', 'specialist', {'session_minutes': 60, 'provider': 'SP-3'}, 'active'), ('review', 'administrator', {'progress_note': '阶段复盘'}, 'under_review'), ('amend', 'case_manager', {'amendment_reason': '调整目标', 'updated_goals': ['目标A', '目标B']}, 'active'), ('close', 'administrator', {'review_complete': True}, 'closed')]


class FailureTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = build_service(str(Path(self.temp.name) / "test.db"))

    def tearDown(self):
        self.temp.cleanup()

    def test_permission_and_duplicate(self):
        with self.assertRaises(PermissionDenied):
            self.service.create(Actor("outsider", "outsider"), "IEP-28001", CREATE_DATA)
        self.service.create(Actor("creator", "case_manager"), "IEP-28001", CREATE_DATA)
        with self.assertRaises(Conflict):
            self.service.create(Actor("creator", "case_manager"), "IEP-28001", CREATE_DATA)

    def test_stale_version_is_rejected(self):
        record = self.service.create(Actor("creator", "case_manager"), "IEP-28001", CREATE_DATA)
        first = FLOW[0]
        record = self.service.act(Actor("operator", first[1]), record["id"], record["version"], first[0], first[2])
        second = FLOW[1]
        with self.assertRaises(Conflict):
            self.service.act(Actor("operator", second[1]), record["id"], record["version"] - 1, second[0], second[2])
