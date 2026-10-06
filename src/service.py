"""业务用例编排、权限检查与审计。

新增对账链用例：
- submit_batch/resume_batch：批量回执入账，按外部编号幂等，从最后完整检查点续作。
- 撤回/重新同意：未确认待核回执失效重算，已确认快照依据冻结，原入账服务保留。
- review/confirm_snapshot：复查生成未确认快照，确认后依据冻结。
"""
from typing import Any, Dict, List, Optional

from .audit import AuditRecorder
from .domain import Actor, BatchInterrupted, DomainError, PermissionDenied, text
from .repository import Repository
from .rules import (
    RECEIPT_INVALID,
    RECEIPT_PENDING,
    RECEIPT_POSTED,
    DomainRules,
)


# 依据变化（撤回/重新同意）后需要重算待核回执的动作。
BASIS_CHANGE_ACTIONS = {"consent", "withdraw_consent"}
# 进入复查需要生成未确认快照的动作。
SNAPSHOT_CREATING_ACTIONS = {"review"}
CONFIRM_SNAPSHOT_ACTION = "confirm_snapshot"


class Service:
    def __init__(self, repository: Repository, rules: DomainRules, audit: AuditRecorder = None) -> None:
        self.repository = repository
        self.rules = rules
        self.audit = audit or AuditRecorder(repository)

    @staticmethod
    def _actor(actor: Actor) -> Actor:
        if actor is None or not actor.user_id.strip() or not actor.role.strip():
            raise PermissionDenied("缺少调用身份")
        return actor

    def _ensure_known_role(self, actor: Actor) -> None:
        if not self.rules.known_role(actor.role):
            raise PermissionDenied("角色无权访问该服务")

    def create(self, actor: Actor, reference: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if not self.rules.role_can_create(actor.role):
            raise PermissionDenied("角色无权创建记录")
        reference = text({"reference": reference}, "reference")
        prepared = self.rules.prepare_create(payload or {})
        self.rules.check_create_conflicts(prepared, self.repository.list_records(limit=500))
        return self.repository.create(reference, self.rules.INITIAL_STATE, prepared, actor.user_id)

    def list_records(self, actor: Actor, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_records(state=state, limit=limit)

    def get_record(self, actor: Actor, record_id: int) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.get(record_id)

    # ---- 回执批次 -----------------------------------------------------------------

    def submit_batch(self, actor: Actor, record_id: int, expected_version: int,
                     batch_ref: str, items: List[Dict[str, Any]]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and not self.rules.role_can_action(actor.role, "log_service"):
            raise PermissionDenied("角色无权回传服务回执")
        batch_ref = text({"batch_ref": batch_ref}, "batch_ref")
        cleaned = self.rules.validate_receipt_items(items)
        # create_batch 内持有写锁并校验计划版本：两名经办同批并发只让先到版本推进。
        batch = self.repository.create_batch(batch_ref, record_id, int(expected_version), cleaned, actor.user_id)
        return self._run_batch(batch)

    def resume_batch(self, actor: Actor, batch_ref: str) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        if actor.role != "admin" and not self.rules.role_can_action(actor.role, "log_service"):
            raise PermissionDenied("角色无权回传服务回执")
        batch_ref = text({"batch_ref": batch_ref}, "batch_ref")
        batch = self.repository.get_batch(batch_ref)
        if batch["status"] == "completed":
            return self._batch_summary(batch)
        return self._run_batch(batch)

    def _run_batch(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        """从最后完整检查点逐条入账；中途写库失败则抛出可续作异常，已完成回执不重放。"""
        started_at = int(batch["checkpoint"])
        posted = pending = duplicate = 0
        for offset in range(int(batch["checkpoint"]), int(batch["total_items"])):
            item = batch["items"][offset]
            try:
                result = self.repository.book_receipt(batch, item)
            except DomainError:
                raise
            except Exception as exc:
                raise BatchInterrupted(batch["reference"], offset) from exc
            batch = self.repository.get_batch(batch["reference"])
            if result is None:
                duplicate += 1
            elif result["status"] == RECEIPT_POSTED:
                posted += 1
            else:
                pending += 1
        self.repository.complete_batch(int(batch["id"]))
        summary = self._batch_summary(self.repository.get_batch(batch["reference"]))
        summary["posted"] = posted
        summary["pending"] = pending
        summary["duplicate"] = duplicate
        summary["resumed_from"] = started_at
        return summary

    def _batch_summary(self, batch: Dict[str, Any]) -> Dict[str, Any]:
        receipts = self.repository.list_receipts(int(batch["record_id"]))
        ours = [r for r in receipts if r.get("batch_id") == int(batch["id"])]
        return {
            "batch_ref": batch["reference"],
            "record_id": int(batch["record_id"]),
            "status": batch["status"],
            "checkpoint": int(batch["checkpoint"]),
            "total_items": int(batch["total_items"]),
            "posted": sum(1 for r in ours if r["status"] == RECEIPT_POSTED),
            "pending": sum(1 for r in ours if r["status"] == RECEIPT_PENDING),
            "invalid": sum(1 for r in ours if r["status"] == RECEIPT_INVALID),
            "duplicate": int(batch["total_items"]) - len(ours),
            "resumed_from": 0,
        }

    def list_batches(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_batches(record_id)

    def list_receipts(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_receipts(record_id)

    # ---- 计划动作（含同意变化重算与快照） -----------------------------------------

    def act(self, actor: Actor, record_id: int, expected_version: int, action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        action = text({"action": action}, "action")
        if not self.rules.role_can_action(actor.role, action):
            raise PermissionDenied("角色无权执行该操作")
        record = self.repository.get(record_id)
        self.rules.require_transition(record, action)
        if action == CONFIRM_SNAPSHOT_ACTION:
            return self.repository.confirm_snapshot(record_id, int(expected_version), actor.user_id)["record"]
        if action in SNAPSHOT_CREATING_ACTIONS:
            return self._act_with_snapshot(actor, record, int(expected_version), action, data or {})
        if action in BASIS_CHANGE_ACTIONS:
            return self._act_basis_change(actor, record, int(expected_version), action, data or {})
        new_state, new_payload, summary = self.rules.apply_action(record, action, data or {})
        return self.repository.mutate(
            record_id=record_id,
            expected_version=int(expected_version),
            state=new_state,
            payload=new_payload,
            actor_id=actor.user_id,
            action=action,
            details={"summary": summary, "input": data or {}, "from": record["state"], "to": new_state},
        )

    def _act_with_snapshot(self, actor: Actor, record: Dict[str, Any], expected_version: int,
                           action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        receipts = self.repository.list_receipts(int(record["id"]))
        basis = self.rules.snapshot_basis(new_payload, receipts)
        result = self.repository.create_snapshot(
            int(record["id"]),
            expected_version,
            new_state,
            new_payload,
            basis,
            actor.user_id,
            action,
            {"summary": summary, "input": data, "from": record["state"], "to": new_state, "snapshot_basis": basis},
        )
        return result["record"]

    def _act_basis_change(self, actor: Actor, record: Dict[str, Any], expected_version: int,
                          action: str, data: Dict[str, Any]) -> Dict[str, Any]:
        new_state, new_payload, summary = self.rules.apply_action(record, action, data)
        # 已确认快照冻结的回执不参与重算；只对未确认的待核回执失效重算。
        locked = self.repository.locked_external_ids(int(record["id"]))
        candidates = self.repository.pending_candidates(int(record["id"]), locked)
        decisions = self.rules.reevaluate_candidates(new_payload, candidates)
        applied = [d for d in decisions if d["status"] != d["previous_status"]]
        final_payload = self.rules.totals_from_receipts(
            new_payload,
            self._receipts_after_decisions(int(record["id"]), decisions),
        )
        stale_ids = self.repository.unconfirmed_snapshot_ids(int(record["id"]))
        details = {
            "summary": summary,
            "input": data,
            "from": record["state"],
            "to": new_state,
            "reevaluated": decisions,
            "stale_snapshot_ids": stale_ids,
            "locked_receipts": sorted(locked),
        }
        return self.repository.apply_basis_change(
            int(record["id"]),
            expected_version,
            new_state,
            final_payload,
            applied,
            stale_ids,
            actor.user_id,
            action,
            details,
        )

    def _receipts_after_decisions(self, record_id: int, decisions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        changed = {int(d["id"]): d["status"] for d in decisions}
        result = []
        for receipt in self.repository.list_receipts(record_id):
            status = changed.get(int(receipt["id"]), receipt["status"])
            result.append({"status": status, "minutes": int(receipt["minutes"])})
        return result

    def snapshots(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.list_snapshots(record_id)

    def timeline(self, actor: Actor, record_id: int) -> List[Dict[str, Any]]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.audit.timeline(record_id)

    def stats(self, actor: Actor) -> Dict[str, int]:
        actor = self._actor(actor)
        self._ensure_known_role(actor)
        return self.repository.stats()
