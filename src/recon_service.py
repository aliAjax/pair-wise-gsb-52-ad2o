"""对账链用例编排：批次提交/续传、同意登记与撤回、依据重算、复查快照。

恢复语义：
- 批次处理以单回执事务推进，失败时检查点即最后完整入账位置；
- 续传从检查点之后继续，external_id 唯一约束兜底，分钟与审计都不会重复。
"""
import json
from typing import Any, Dict, List, Optional

from .domain import Actor, Conflict, NotFound, PermissionDenied, ValidationError
from .recon_repository import ReconRepository
from .recon_rules import ReconRules
from .repository import Repository


class ReconService:
    def __init__(self, repository: ReconRepository, rules: ReconRules, records: Repository) -> None:
        self.repository = repository
        self.rules = rules
        self.records = records

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def _ensure_can(self, actor: Actor, operation: str) -> None:
        if not self.rules.role_can(actor.role, operation):
            raise PermissionDenied("角色无权执行该操作")

    def _plan_or_none(self, plan_reference: str) -> Optional[Dict[str, Any]]:
        return self.records.find_by_reference(plan_reference)

    def _plan_or_raise(self, plan_reference: str) -> Dict[str, Any]:
        plan = self._plan_or_none(plan_reference)
        if plan is None:
            raise NotFound("支持计划不存在")
        return plan

    # ---- 监护人同意 ----

    def register_consent(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_can(actor, "consent_create")
        consent = self.rules.validate_consent(payload)
        self._plan_or_raise(consent["plan_reference"])
        created = self.repository.create_consent(consent, actor.user_id)
        self._recalculate_plan(consent["plan_reference"], actor.user_id)
        return created

    def withdraw_consent(self, actor: Actor, consent_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_can(actor, "consent_withdraw")
        effective_date = self.rules.validate_withdraw(payload)
        updated = self.repository.withdraw_consent(int(consent_id), effective_date, actor.user_id)
        # 依据变化：未确认回执失效重算；已入账分钟保持不动（不冲掉原服务）。
        self._recalculate_plan(updated["plan_reference"], actor.user_id)
        return updated

    def list_consents(self, actor: Actor, plan_reference: Optional[str] = None) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_consents(plan_reference)

    # ---- 回执批次 ----

    def submit_batch(self, actor: Actor, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_can(actor, "batch")
        batch_input = self.rules.validate_batch(payload)
        existing = self.repository.find_batch(batch_input["batch_no"])
        if existing is None:
            batch = self.repository.create_batch(batch_input["batch_no"], batch_input["provider"], batch_input["receipts"], actor.user_id)
            return self._process_batch(batch, actor.user_id)
        if existing["status"] == "completed":
            raise Conflict("批次已完成，不能重复推进")
        expected_version = payload.get("expected_version")
        if not isinstance(expected_version, int):
            raise ValidationError("续传批次必须提供expected_version")
        self._assert_same_payload(existing, batch_input["receipts"])
        batch = self.repository.claim_batch(existing["batch_no"], expected_version, actor.user_id)
        return self._process_batch(batch, actor.user_id)

    def resume_batch(self, actor: Actor, batch_no: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_can(actor, "batch")
        expected_version = (payload or {}).get("expected_version")
        if not isinstance(expected_version, int):
            raise ValidationError("expected_version必须是整数")
        existing = self.repository.get_batch(batch_no)
        if existing["status"] == "completed":
            raise Conflict("批次已完成，不能重复推进")
        receipts = (payload or {}).get("receipts")
        if receipts is not None:
            self._assert_same_payload(existing, [self.rules.validate_receipt(item, i) for i, item in enumerate(receipts)])
        batch = self.repository.claim_batch(batch_no, expected_version, actor.user_id)
        return self._process_batch(batch, actor.user_id)

    @staticmethod
    def _assert_same_payload(batch: Dict[str, Any], receipts: List[Dict[str, Any]]) -> None:
        stored = json.dumps(batch["payload"], ensure_ascii=False, sort_keys=True)
        incoming = json.dumps(receipts, ensure_ascii=False, sort_keys=True)
        if stored != incoming:
            raise Conflict("批次内容与已登记回执不一致")

    def _make_scorer(self, plans: Dict[str, Dict[str, Any]]):
        def scorer(connection, receipt: Dict[str, Any]) -> Dict[str, Any]:
            plan = plans.get(receipt["plan_reference"])
            consent = self.repository.covering_consent_in(connection, receipt["plan_reference"], receipt["service_date"])
            return self.rules.score_receipt(receipt, plan, consent)

        return scorer

    def _process_batch(self, batch: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        receipts = batch["payload"]
        plans = {}
        for receipt in receipts:
            reference = receipt["plan_reference"]
            if reference not in plans:
                plans[reference] = self._plan_or_none(reference)
        scorer = self._make_scorer(plans)
        summary = {"posted": 0, "held": 0, "duplicates": 0}
        try:
            for seq in range(int(batch["processed"]), len(receipts)):
                stored, created = self.repository.process_receipt(batch["id"], seq, receipts[seq], actor_id, scorer)
                if not created:
                    summary["duplicates"] += 1
                elif stored["status"] == "posted":
                    summary["posted"] += 1
                else:
                    summary["held"] += 1
        except Exception:
            # 写库失败：检查点已指向最后完整入账的回执，标记失败后可续传。
            latest = self.repository.find_batch(batch["batch_no"])
            processed = int(latest["processed"]) if latest else 0
            try:
                self.repository.finish_batch(batch["id"], "failed", dict(summary, processed=processed, total=len(receipts)), actor_id)
            except Exception:
                pass
            raise
        finished = self.repository.finish_batch(batch["id"], "completed", dict(summary, processed=len(receipts), total=len(receipts)), actor_id)
        return finished

    def get_batch(self, actor: Actor, batch_no: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get_batch(batch_no)

    # ---- 依据变化重算 ----

    def _recalculate_plan(self, plan_reference: str, actor_id: str) -> int:
        """重算未确认回执：被确认快照覆盖的保持冻结，已入账的不冲掉。"""
        plan = self._plan_or_none(plan_reference)
        confirmed = self.repository.confirmed_receipt_ids(plan_reference)
        changed = 0
        for receipt in self.repository.list_receipts(plan_reference=plan_reference, statuses=["pending", "held"]):
            if receipt["id"] in confirmed:
                continue
            consent = self.repository.covering_consent(plan_reference, receipt["service_date"])
            decision = self.rules.score_receipt(receipt, plan, consent)
            _, did_change = self.repository.rescore_receipt(receipt["id"], decision, actor_id)
            if did_change:
                changed += 1
        if changed:
            latest = self.repository.latest_consent(plan_reference)
            self.repository.add_event(
                "plan", plan_reference, plan_reference, "receipts_recalculated", actor_id,
                {"changed": changed, "basis_consent_id": latest["id"] if latest else None, "basis_consent_version": latest["version"] if latest else None},
            )
        return changed

    # ---- 复查快照与对账视图 ----

    def create_snapshot(self, actor: Actor, plan_reference: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        self._ensure_can(actor, "snapshot")
        plan = self._plan_or_raise(plan_reference)
        ledger = self.repository.get_ledger(plan_reference)
        posted = self.repository.list_receipts(plan_reference=plan_reference, statuses=["posted"])
        consent = self.repository.latest_consent(plan_reference)
        delivered = int(ledger["baseline_minutes"]) + int(ledger["receipt_minutes"])
        snapshot = {
            "plan_reference": plan_reference,
            "consent_id": consent["id"] if consent else None,
            "consent_version": consent["version"] if consent else None,
            "delivered_minutes": delivered,
            "compliance_rate": self.rules.compliance_rate(delivered, int(plan["payload"].get("service_minutes", 0))),
            "receipt_count": len(posted),
            "basis": {
                "consent_id": consent["id"] if consent else None,
                "consent_version": consent["version"] if consent else None,
                "consent_status": consent["status"] if consent else None,
                "baseline_minutes": int(ledger["baseline_minutes"]),
                "receipt_ids": [receipt["id"] for receipt in posted],
                "ledger_version": int(ledger["version"]),
            },
        }
        return self.repository.create_snapshot(snapshot, actor.user_id)

    def reconciliation(self, actor: Actor, plan_reference: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        plan = self._plan_or_raise(plan_reference)
        ledger = self.repository.get_ledger(plan_reference)
        receipts = self.repository.list_receipts(plan_reference=plan_reference)
        consent = self.repository.latest_consent(plan_reference)
        snapshots = self.repository.list_snapshots(plan_reference)
        delivered = int(ledger["baseline_minutes"]) + int(ledger["receipt_minutes"])
        held = [receipt for receipt in receipts if receipt["status"] == "held"]
        return {
            "plan_reference": plan_reference,
            "plan_state": plan["state"],
            "service_minutes": int(plan["payload"].get("service_minutes", 0)),
            "baseline_minutes": int(ledger["baseline_minutes"]),
            "receipt_minutes": int(ledger["receipt_minutes"]),
            "delivered_minutes": delivered,
            "held_minutes": sum(int(receipt["minutes"]) for receipt in held),
            "compliance_rate": self.rules.compliance_rate(delivered, int(plan["payload"].get("service_minutes", 0))),
            "receipts": {
                "posted": sum(1 for receipt in receipts if receipt["status"] == "posted"),
                "held": len(held),
                "pending": sum(1 for receipt in receipts if receipt["status"] == "pending"),
            },
            "consent": consent,
            "snapshots": snapshots,
            "events": self.repository.events(plan_reference=plan_reference, limit=50),
        }

    def events(self, actor: Actor, plan_reference: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.events(plan_reference=plan_reference, limit=limit)
