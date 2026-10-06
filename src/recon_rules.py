"""对账链领域规则：监护人同意窗口、回执计分与角色权限。

计分语义：
- 回执按外部编号只入账一次（仓储层唯一约束保证）。
- 服务日期落在有效同意窗口内才计分（posted），否则待核（held）。
- 同意被撤回后，撤回生效日之前且仍在原有效期内的服务视为当时有效；
  生效日之后的服务一律待核，已入账的累计分钟不被冲掉。
"""
from datetime import date
from typing import Any, Dict, Optional

from .domain import ValidationError, integer, text


CONSENT_CREATE_ROLES = {"parent_rep", "case_manager"}
CONSENT_WITHDRAW_ROLES = {"parent_rep"}
BATCH_ROLES = {"provider_ops", "case_manager"}
SNAPSHOT_ROLES = {"administrator"}

RECEIPT_STATUSES = ("pending", "posted", "held")
MAX_BATCH_SIZE = 500


def _parse_date(raw: str, field: str) -> date:
    try:
        return date.fromisoformat(raw)
    except ValueError as exc:
        raise ValidationError("%s必须是YYYY-MM-DD日期" % field) from exc


class ReconRules:
    def known_role(self, role: str) -> bool:
        all_roles = set(CONSENT_CREATE_ROLES) | set(CONSENT_WITHDRAW_ROLES) | set(BATCH_ROLES) | set(SNAPSHOT_ROLES)
        return role == "admin" or role in all_roles

    def role_can(self, role: str, operation: str) -> bool:
        if role == "admin":
            return True
        allowed = {
            "consent_create": CONSENT_CREATE_ROLES,
            "consent_withdraw": CONSENT_WITHDRAW_ROLES,
            "batch": BATCH_ROLES,
            "snapshot": SNAPSHOT_ROLES,
        }.get(operation, set())
        return role in allowed

    def validate_consent(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        consent = {
            "plan_reference": text(data, "plan_reference"),
            "guardian": text(data, "guardian"),
            "scope": text(data, "scope"),
            "valid_from": text(data, "valid_from"),
            "valid_to": text(data, "valid_to"),
        }
        start = _parse_date(consent["valid_from"], "valid_from")
        end = _parse_date(consent["valid_to"], "valid_to")
        if start > end:
            raise ValidationError("同意有效期起日不能晚于止日")
        return consent

    def validate_withdraw(self, payload: Dict[str, Any]) -> str:
        effective = text(dict(payload or {}), "effective_date")
        _parse_date(effective, "effective_date")
        return effective

    def validate_receipt(self, item: Dict[str, Any], index: int) -> Dict[str, Any]:
        if not isinstance(item, dict):
            raise ValidationError("第%s条回执必须是对象" % (index + 1))
        receipt = {
            "external_id": text(item, "external_id"),
            "plan_reference": text(item, "plan_reference"),
            "service_date": text(item, "service_date"),
            "minutes": integer(item, "minutes", 1, 24 * 60),
        }
        _parse_date(receipt["service_date"], "service_date")
        return receipt

    def validate_batch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        data = dict(payload or {})
        items = data.get("receipts")
        if not isinstance(items, list) or not items:
            raise ValidationError("receipts必须是非空列表")
        if len(items) > MAX_BATCH_SIZE:
            raise ValidationError("单批次回执不能超过%s条" % MAX_BATCH_SIZE)
        return {
            "batch_no": text(data, "batch_no"),
            "provider": text(data, "provider"),
            "receipts": [self.validate_receipt(item, i) for i, item in enumerate(items)],
        }

    def consent_covers(self, consent: Dict[str, Any], service_date: str) -> bool:
        """服务日期是否被该同意覆盖（见模块docstring的计分语义）。"""
        if not (consent["valid_from"] <= service_date <= consent["valid_to"]):
            return False
        if consent["status"] == "active":
            return True
        withdrawn_at = consent.get("withdrawn_at") or ""
        return bool(withdrawn_at) and service_date <= withdrawn_at

    def score_receipt(self, receipt: Dict[str, Any], plan: Optional[Dict[str, Any]], consent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """给单条回执定状态。plan为None说明计划不存在，consent为None说明无覆盖同意。"""
        baseline = 0
        if plan is not None:
            baseline = int(plan["payload"].get("delivered_minutes", 0))
        if plan is None:
            return {"status": "held", "reason": "unknown_plan", "consent_id": None, "consent_version": None, "baseline_minutes": 0}
        if consent is None:
            return {"status": "held", "reason": "no_valid_consent", "consent_id": None, "consent_version": None, "baseline_minutes": baseline}
        return {
            "status": "posted",
            "reason": None,
            "consent_id": consent["id"],
            "consent_version": consent["version"],
            "baseline_minutes": baseline,
        }

    def compliance_rate(self, delivered_minutes: int, service_minutes: int) -> float:
        if service_minutes <= 0:
            return 0.0
        return round(delivered_minutes / service_minutes * 100, 2)
