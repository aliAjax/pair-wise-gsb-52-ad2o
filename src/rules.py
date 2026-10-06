"""特殊教育支持计划合规领域规则与状态转换。

对账链核心概念：
- 同意窗口：监护人同意按 [effective_from, withdrawn_on) 的半开区间生效，撤回关闭当前窗口。
- 服务回执：外部编号全局唯一，按服务日期是否落在任一同意窗口内决定
  posted（入账计分）/ pending（待核不计分）；依据变化时 pending 回执失效重算。
- 累计分钟：posted 回执分钟数 + 计划初始/手工补录分钟，重算时从头汇总，绝不追加。
- 复查快照：已确认快照冻结确认当时的依据（分钟数、回执指纹、同意窗口），之后依据变化不改写。
"""
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .domain import (
    Conflict,
    ValidationError,
    boolean,
    integer,
    iso_date,
    text,
    text_list,
    today,
)


INITIAL_STATE = "draft"
CREATE_ROLES = {'case_manager'}
ACTION_ROLES = {
    'consent': {'parent_rep'},
    'withdraw_consent': {'parent_rep'},
    'activate': {'case_manager'},
    'log_service': {'case_manager', 'specialist'},
    'review': {'administrator'},
    'confirm_snapshot': {'administrator'},
    'amend': {'case_manager'},
    'close': {'administrator'},
}
TRANSITIONS = {
    'consent': {'draft': 'consented', 'active': 'active', 'under_review': 'under_review'},
    'withdraw_consent': {'consented': 'active', 'active': 'active', 'under_review': 'under_review'},
    'activate': {'consented': 'active'},
    'log_service': {'active': 'active'},
    'review': {'active': 'under_review'},
    'confirm_snapshot': {'under_review': 'under_review'},
    'amend': {'under_review': 'active'},
    'close': {'active': 'closed', 'under_review': 'closed'},
}

RECEIPT_POSTED = "posted"
RECEIPT_PENDING = "pending"
RECEIPT_INVALID = "invalid"
RECEIPT_STATUSES = (RECEIPT_POSTED, RECEIPT_PENDING, RECEIPT_INVALID)
SNAPSHOT_UNCONFIRMED = "unconfirmed"
SNAPSHOT_CONFIRMED = "confirmed"
SNAPSHOT_STALE = "stale"


class DomainRules:
    INITIAL_STATE = INITIAL_STATE
    RECEIPT_POSTED = RECEIPT_POSTED
    RECEIPT_PENDING = RECEIPT_PENDING
    RECEIPT_INVALID = RECEIPT_INVALID

    def known_role(self, role: str) -> bool:
        all_roles = set(CREATE_ROLES)
        for roles in ACTION_ROLES.values():
            all_roles.update(roles)
        return role == "admin" or role in all_roles

    def role_can_create(self, role: str) -> bool:
        return role == "admin" or role in CREATE_ROLES

    def role_can_action(self, role: str, action: str) -> bool:
        return role == "admin" or role in ACTION_ROLES.get(action, set())

    def validate_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = dict(payload)
        text(p, "student_id")
        text(p, "disability")
        integer(p, "service_minutes", 1)
        integer(p, "delivered_minutes", 0)
        integer(p, "review_due_days", 0)
        integer(p, "goals_count", 1)
        boolean(p, "consent")
        if p["delivered_minutes"] > p["service_minutes"]:
            raise ValidationError("已提供服务不能超过计划服务")
        return p

    def prepare_create(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        p = self.validate_create(payload)
        # 初始已交付分钟视为手工/历史补录，与回执分钟分开存放后再汇总。
        p["logged_minutes"] = int(p.pop("delivered_minutes"))
        p["posted_minutes"] = 0
        p["pending_minutes"] = 0
        p["invalid_minutes"] = 0
        self._recompute_totals(p)
        p["review_overdue"] = int(p["review_due_days"]) <= 0
        p["plan_status"] = "draft"
        p.setdefault("consent_windows", [])
        return p

    def check_create_conflicts(self, payload: Dict[str, Any], existing: Iterable[Dict[str, Any]]) -> None:
        for item in existing:
            if item["state"] in {"active", "under_review", "consented"} and item["payload"].get("student_id") == payload.get("student_id"):
                raise Conflict("该学生已有有效的支持计划")

    def require_transition(self, record: Dict[str, Any], action: str) -> str:
        allowed = TRANSITIONS.get(action, {}).get(record["state"])
        if allowed is None:
            raise Conflict("当前状态不允许执行%s" % action)
        return allowed

    # ---- 同意窗口 -----------------------------------------------------------------

    @staticmethod
    def open_window(payload: Dict[str, Any]) -> Optional[Dict[str, str]]:
        for window in reversed(payload.get("consent_windows", [])):
            if not window.get("withdrawn_on"):
                return window
        return None

    @staticmethod
    def service_date_covered(payload: Dict[str, Any], service_date: str) -> bool:
        """服务日期落在任一同意窗口 [effective_from, withdrawn_on) 内才算有同意依据。"""
        for window in payload.get("consent_windows", []):
            start = window.get("effective_from")
            end = window.get("withdrawn_on")
            if start and service_date >= start and (not end or service_date < end):
                return True
        return False

    # ---- 回执 ---------------------------------------------------------------------

    @staticmethod
    def validate_receipt_item(item: Any, index: int = None) -> Dict[str, Any]:
        where = "第%s项" % index if index is not None else "receipt"
        if not isinstance(item, dict):
            raise ValidationError("%s必须是对象" % where)
        external_id = text(item, "external_id")
        minutes = integer(item, "minutes", 1)
        service_date = iso_date(item, "service_date")
        return {"external_id": external_id, "minutes": minutes, "service_date": service_date}

    def validate_receipt_items(self, items: Any, limit: int = 500) -> List[Dict[str, Any]]:
        if not isinstance(items, list) or not items:
            raise ValidationError("receipts必须是非空列表")
        if len(items) > limit:
            raise ValidationError("单批次回执不能超过%s条" % limit)
        cleaned: List[Dict[str, Any]] = []
        seen = set()
        for index, item in enumerate(items, 1):
            receipt = self.validate_receipt_item(item, index)
            if receipt["external_id"] in seen:
                raise ValidationError("批次内外部编号重复：%s" % receipt["external_id"])
            seen.add(receipt["external_id"])
            cleaned.append(receipt)
        return cleaned

    def classify_receipt(self, payload: Dict[str, Any], service_date: str) -> str:
        """批次入账判定：同意窗口内 posted，窗口外 pending（待核，不冲原服务）。"""
        if self.service_date_covered(payload, service_date):
            return RECEIPT_POSTED
        return RECEIPT_PENDING

    def reevaluate_status(self, payload: Dict[str, Any], service_date: str) -> str:
        """依据变化重算判定：窗口内重新入账，窗口外直接失效（不再保留待核）。"""
        if self.service_date_covered(payload, service_date):
            return RECEIPT_POSTED
        return RECEIPT_INVALID

    @staticmethod
    def reason_for(payload: Dict[str, Any], service_date: str) -> str:
        windows = payload.get("consent_windows", [])
        starts = [w["effective_from"] for w in windows if w.get("effective_from")]
        ends = [w["withdrawn_on"] for w in windows if w.get("withdrawn_on")]
        if starts and service_date < min(starts):
            return "before_consent"
        if ends and service_date >= min(ends):
            return "after_withdrawal"
        return "outside_consent_window"

    # ---- 累计分钟 -----------------------------------------------------------------

    @staticmethod
    def _recompute_totals(p: Dict[str, Any]) -> Dict[str, Any]:
        """依据分钟构成从头汇总，任何重算路径都不做增量追加。"""
        delivered = int(p.get("logged_minutes", 0)) + int(p.get("posted_minutes", 0))
        delivered = min(delivered, int(p["service_minutes"]))
        p["delivered_minutes"] = delivered
        p["missing_minutes"] = max(0, int(p["service_minutes"]) - delivered)
        p["compliance_rate"] = round(delivered / int(p["service_minutes"]) * 100, 2)
        return p

    def totals_from_receipts(self, payload: Dict[str, Any], receipts: List[Dict[str, Any]]) -> Dict[str, Any]:
        """按回执当前状态重算分钟构成，回执本身分钟不变，只是重新归类。"""
        p = dict(payload)
        p["posted_minutes"] = sum(int(r["minutes"]) for r in receipts if r["status"] == RECEIPT_POSTED)
        p["pending_minutes"] = sum(int(r["minutes"]) for r in receipts if r["status"] == RECEIPT_PENDING)
        p["invalid_minutes"] = sum(int(r["minutes"]) for r in receipts if r["status"] == RECEIPT_INVALID)
        return self._recompute_totals(p)

    # ---- 依据变化重算 -------------------------------------------------------------

    def reevaluate_candidates(self, payload: Dict[str, Any], receipts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """依据（同意窗口）变化时重算待核回执：覆盖则入账，否则失效；状态与原因写回。"""
        results: List[Dict[str, Any]] = []
        for receipt in receipts:
            status = self.reevaluate_status(payload, receipt["service_date"])
            reason = ""
            if status == RECEIPT_INVALID:
                reason = self.reason_for(payload, receipt["service_date"])
            results.append({
                "id": int(receipt["id"]),
                "external_id": receipt["external_id"],
                "service_date": receipt["service_date"],
                "minutes": int(receipt["minutes"]),
                "previous_status": receipt["status"],
                "status": status,
                "reason": reason,
            })
        return results

    # ---- 快照依据 -----------------------------------------------------------------

    @staticmethod
    def snapshot_basis(payload: Dict[str, Any], receipts: List[Dict[str, Any]]) -> Dict[str, Any]:
        posted = [r for r in receipts if r["status"] == RECEIPT_POSTED]
        fingerprint = sorted((r["external_id"], int(r["minutes"]), r["service_date"]) for r in posted)
        return {
            "service_minutes": int(payload["service_minutes"]),
            "logged_minutes": int(payload.get("logged_minutes", 0)),
            "posted_minutes": sum(int(r["minutes"]) for r in posted),
            "delivered_minutes": int(payload.get("delivered_minutes", 0)),
            "compliance_rate": float(payload.get("compliance_rate", 0.0)),
            "posted_receipt_count": len(posted),
            "consent_windows": [dict(w) for w in payload.get("consent_windows", [])],
            "posted_fingerprint": fingerprint,
        }

    # ---- 状态机动作 ---------------------------------------------------------------

    def apply_action(self, record: Dict[str, Any], action: str, data: Dict[str, Any]) -> Tuple[str, Dict[str, Any], str]:
        new_state = self.require_transition(record, action)
        data = dict(data or {})
        p = dict(record["payload"])
        changes: Dict[str, Any] = {}
        summary = ""
        if action == "consent":
            if self.open_window(p):
                raise ValidationError("当前同意仍然有效，无需重复同意")
            if not boolean(data, "guardian_confirmed"):
                raise ValidationError("监护人尚未确认")
            if not text(data, "consent_scope"):
                raise ValidationError("同意范围不能为空")
            effective_from = data.get("effective_from")
            if effective_from:
                effective_from = iso_date(data, "effective_from")
            else:
                effective_from = today()
            changes["consent"] = True
            changes["consent_scope"] = data["consent_scope"]
            windows = list(p.get("consent_windows", []))
            windows.append({"effective_from": effective_from, "withdrawn_on": None})
            changes["consent_windows"] = windows
            summary = "监护人同意已记录"
        elif action == "withdraw_consent":
            withdrawn_on = data.get("withdrawn_on")
            if withdrawn_on:
                withdrawn_on = iso_date(data, "withdrawn_on")
            else:
                withdrawn_on = today()
            window = self.open_window(p)
            if not window:
                raise ValidationError("没有可撤回的有效同意")
            if withdrawn_on < window["effective_from"]:
                raise ValidationError("撤回日期不能早于同意生效日期")
            windows = [dict(w) for w in p.get("consent_windows", [])]
            for item in reversed(windows):
                if not item.get("withdrawn_on"):
                    item["withdrawn_on"] = withdrawn_on
                    break
            changes["consent"] = False
            changes["withdrawn_on"] = withdrawn_on
            changes["consent_windows"] = windows
            summary = "监护人同意已撤回，待核回执失效重算，原入账服务保留"
        elif action == "activate":
            if not p.get("consent") or not self.open_window(p):
                raise ValidationError("缺少有效同意")
            if int(p["goals_count"]) <= 0:
                raise ValidationError("计划必须包含目标")
            changes["plan_status"] = "active"
            summary = "支持计划生效"
        elif action == "log_service":
            session = integer(data, "session_minutes", 1)
            current = int(p.get("logged_minutes", 0)) + int(p.get("posted_minutes", 0))
            if session + current > int(p["service_minutes"]):
                raise ValidationError("记录服务超过计划分钟数")
            changes["logged_minutes"] = int(p.get("logged_minutes", 0)) + session
            changes["last_provider"] = text(data, "provider")
            merged = dict(p, **changes)
            self._recompute_totals(merged)
            changes["delivered_minutes"] = merged["delivered_minutes"]
            changes["missing_minutes"] = merged["missing_minutes"]
            changes["compliance_rate"] = merged["compliance_rate"]
            summary = "服务记录已登记"
        elif action == "review":
            changes["progress_note"] = text(data, "progress_note")
            changes["review_overdue"] = False
            summary = "进入计划复查，待确认快照已生成"
        elif action == "confirm_snapshot":
            if not boolean(data, "snapshot_confirmed", False):
                raise ValidationError("必须确认快照")
            summary = "复查快照已确认，依据已冻结"
        elif action == "amend":
            changes["amendment_reason"] = text(data, "amendment_reason")
            changes["updated_goals"] = text_list(data, "updated_goals", 1)
            changes["goals_count"] = len(changes["updated_goals"])
            changes["plan_status"] = "active"
            summary = "计划已修订"
        elif action == "close":
            if not boolean(data, "review_complete"):
                raise ValidationError("复查尚未完成")
            changes["plan_status"] = "closed"
            summary = "支持计划结束"
        p.update(changes)
        return new_state, p, summary or ("已执行%s" % action)
