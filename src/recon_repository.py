"""对账链存储：同意、回执批次、回执、累计台账、复查快照与审计事件。

事务约定：
- 单条回执的入账、累计台账更新、审计事件和批次检查点在同一个事务里提交，
  因此写库失败时检查点永远指向最后一条完整入账的回执。
- 回执以 external_id 为幂等键，重传只推进检查点，不再累计分钟、不再追加审计。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ReconRepository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS consents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_reference TEXT NOT NULL,
                    guardian TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    version INTEGER NOT NULL DEFAULT 1,
                    withdrawn_at TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipt_batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_no TEXT NOT NULL UNIQUE,
                    provider TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'processing',
                    version INTEGER NOT NULL DEFAULT 1,
                    total INTEGER NOT NULL DEFAULT 0,
                    processed INTEGER NOT NULL DEFAULT 0,
                    checkpoint_seq INTEGER NOT NULL DEFAULT -1,
                    payload TEXT NOT NULL,
                    result TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id INTEGER NOT NULL REFERENCES receipt_batches(id),
                    external_id TEXT NOT NULL UNIQUE,
                    seq INTEGER NOT NULL,
                    plan_reference TEXT NOT NULL,
                    service_date TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    hold_reason TEXT,
                    consent_id INTEGER,
                    consent_version INTEGER,
                    posted_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS plan_ledger (
                    plan_reference TEXT PRIMARY KEY,
                    baseline_minutes INTEGER NOT NULL DEFAULT 0,
                    receipt_minutes INTEGER NOT NULL DEFAULT 0,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    plan_reference TEXT NOT NULL,
                    consent_id INTEGER,
                    consent_version INTEGER,
                    delivered_minutes INTEGER NOT NULL,
                    compliance_rate REAL NOT NULL,
                    receipt_count INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'confirmed',
                    basis TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recon_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    plan_reference TEXT NOT NULL DEFAULT '',
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_consents_plan ON consents(plan_reference, status);
                CREATE INDEX IF NOT EXISTS idx_receipts_batch ON receipts(batch_id, seq);
                CREATE INDEX IF NOT EXISTS idx_receipts_plan ON receipts(plan_reference, status);
                CREATE INDEX IF NOT EXISTS idx_snapshots_plan ON snapshots(plan_reference, id);
                CREATE INDEX IF NOT EXISTS idx_recon_events_plan ON recon_events(plan_reference, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        item["result"] = json.loads(item["result"]) if item["result"] else None
        return item

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"])
        return item

    def _add_event(self, connection: sqlite3.Connection, entity_type: str, entity_id: str, plan_reference: str, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO recon_events(entity_type,entity_id,plan_reference,action,actor_id,details,created_at) VALUES(?,?,?,?,?,?,?)",
            (entity_type, str(entity_id), plan_reference, action, actor_id, json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
        )

    # ---- 监护人同意 ----

    def create_consent(self, consent: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            overlap = connection.execute(
                "SELECT id FROM consents WHERE plan_reference=? AND status='active' AND NOT(valid_to < ? OR valid_from > ?)",
                (consent["plan_reference"], consent["valid_from"], consent["valid_to"]),
            ).fetchone()
            if overlap is not None:
                connection.rollback()
                raise Conflict("同意有效期与现有有效同意重叠")
            cursor = connection.execute(
                "INSERT INTO consents(plan_reference,guardian,scope,valid_from,valid_to,status,version,created_by,created_at,updated_at) VALUES(?,?,?,?,?,'active',1,?,?,?)",
                (consent["plan_reference"], consent["guardian"], consent["scope"], consent["valid_from"], consent["valid_to"], actor_id, now, now),
            )
            consent_id = int(cursor.lastrowid)
            self._add_event(connection, "consent", consent_id, consent["plan_reference"], "consent_registered", actor_id, {"valid_from": consent["valid_from"], "valid_to": consent["valid_to"], "scope": consent["scope"]})
            row = connection.execute("SELECT * FROM consents WHERE id=?", (consent_id,)).fetchone()
            connection.commit()
        return self._row(row)

    def get_consent(self, consent_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM consents WHERE id=?", (consent_id,)).fetchone()
        if row is None:
            raise NotFound("同意记录不存在")
        return self._row(row)

    def list_consents(self, plan_reference: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            if plan_reference:
                rows = connection.execute("SELECT * FROM consents WHERE plan_reference=? ORDER BY id DESC", (plan_reference,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM consents ORDER BY id DESC").fetchall()
        return [self._row(row) for row in rows]

    def latest_consent(self, plan_reference: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM consents WHERE plan_reference=? ORDER BY id DESC LIMIT 1", (plan_reference,)).fetchone()
        return self._row(row) if row else None

    def covering_consent_in(self, connection: sqlite3.Connection, plan_reference: str, service_date: str) -> Optional[Dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM consents WHERE plan_reference=? AND valid_from<=? AND valid_to>=? ORDER BY id DESC",
            (plan_reference, service_date, service_date),
        ).fetchall()
        for row in rows:
            consent = self._row(row)
            if consent["status"] == "active":
                return consent
            withdrawn_at = consent.get("withdrawn_at") or ""
            if withdrawn_at and service_date <= withdrawn_at:
                return consent
        return None

    def covering_consent(self, plan_reference: str, service_date: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            return self.covering_consent_in(connection, plan_reference, service_date)

    def withdraw_consent(self, consent_id: int, effective_date: str, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM consents WHERE id=?", (consent_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("同意记录不存在")
            if row["status"] == "withdrawn":
                connection.rollback()
                raise Conflict("同意已撤回，请勿重复操作")
            connection.execute(
                "UPDATE consents SET status='withdrawn', version=version+1, withdrawn_at=?, updated_at=? WHERE id=?",
                (effective_date, now, consent_id),
            )
            self._add_event(connection, "consent", consent_id, row["plan_reference"], "consent_withdrawn", actor_id, {"effective_date": effective_date, "basis_version": int(row["version"]) + 1})
            updated = connection.execute("SELECT * FROM consents WHERE id=?", (consent_id,)).fetchone()
            connection.commit()
        return self._row(updated)

    # ---- 回执批次 ----

    def create_batch(self, batch_no: str, provider: str, receipts: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                cursor = connection.execute(
                    "INSERT INTO receipt_batches(batch_no,provider,status,version,total,processed,checkpoint_seq,payload,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_no, provider, "processing", 1, len(receipts), 0, -1, json.dumps(receipts, ensure_ascii=False, sort_keys=True), actor_id, now, now),
                )
                batch_id = int(cursor.lastrowid)
                self._add_event(connection, "batch", batch_no, "", "batch_received", actor_id, {"provider": provider, "total": len(receipts)})
                row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
                connection.commit()
        except sqlite3.IntegrityError as exc:
            raise Conflict("批次已存在，请携带expected_version续传") from exc
        return self._batch_row(row)

    def get_batch(self, batch_no: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def find_batch(self, batch_no: str) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
        return self._batch_row(row) if row else None

    def claim_batch(self, batch_no: str, expected_version: int, actor_id: str) -> Dict[str, Any]:
        """乐观并发认领：只有版本匹配的先把版本推进，其余看到冲突。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipt_batches WHERE batch_no=?", (batch_no,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            if row["status"] == "completed":
                connection.rollback()
                raise Conflict("批次已完成，不能重复推进")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            connection.execute(
                "UPDATE receipt_batches SET version=version+1, status='processing', updated_at=? WHERE id=?",
                (now, row["id"]),
            )
            self._add_event(connection, "batch", batch_no, "", "batch_resumed", actor_id, {"from_version": int(row["version"]), "processed": int(row["processed"]), "total": int(row["total"])})
            updated = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (row["id"],)).fetchone()
            connection.commit()
        return self._batch_row(updated)

    def finish_batch(self, batch_id: int, status: str, result: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("批次不存在")
            connection.execute(
                "UPDATE receipt_batches SET status=?, result=?, updated_at=? WHERE id=?",
                (status, json.dumps(result, ensure_ascii=False, sort_keys=True), now, batch_id),
            )
            self._add_event(connection, "batch", row["batch_no"], "", "batch_%s" % status, actor_id, result)
            updated = connection.execute("SELECT * FROM receipt_batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(updated)

    # ---- 回执入账（幂等 + 检查点） ----

    def process_receipt(self, batch_id: int, seq: int, receipt: Dict[str, Any], actor_id: str, scorer: Callable[[sqlite3.Connection, Dict[str, Any]], Dict[str, Any]]) -> Tuple[Dict[str, Any], bool]:
        """处理单条回执：入账、累计台账、审计、检查点同事务提交。

        scorer 在同一事务内读取覆盖服务日期的同意后给出计分决定。
        返回 (receipt, created)：created=False 表示 external_id 已入账，本次只推进检查点。
        """
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute("SELECT * FROM receipts WHERE external_id=?", (receipt["external_id"],)).fetchone()
            if existing is not None:
                connection.execute(
                    "UPDATE receipt_batches SET processed=?, checkpoint_seq=?, updated_at=? WHERE id=?",
                    (seq + 1, seq, now, batch_id),
                )
                connection.commit()
                return self._row(existing), False
            decision = scorer(connection, receipt)
            status = decision["status"]
            posted_at = now if status == "posted" else None
            cursor = connection.execute(
                "INSERT INTO receipts(batch_id,external_id,seq,plan_reference,service_date,minutes,status,hold_reason,consent_id,consent_version,posted_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (batch_id, receipt["external_id"], seq, receipt["plan_reference"], receipt["service_date"], receipt["minutes"], status, decision["reason"], decision["consent_id"], decision["consent_version"], posted_at, now, now),
            )
            receipt_id = int(cursor.lastrowid)
            if status == "posted":
                connection.execute(
                    "INSERT INTO plan_ledger(plan_reference,baseline_minutes,receipt_minutes,version,updated_at) VALUES(?,?,0,1,?) ON CONFLICT(plan_reference) DO NOTHING",
                    (receipt["plan_reference"], decision["baseline_minutes"], now),
                )
                connection.execute(
                    "UPDATE plan_ledger SET receipt_minutes=receipt_minutes+?, version=version+1, updated_at=? WHERE plan_reference=?",
                    (receipt["minutes"], now, receipt["plan_reference"]),
                )
            self._add_event(
                connection, "receipt", receipt["external_id"], receipt["plan_reference"],
                "receipt_%s" % status, actor_id,
                {"batch_id": batch_id, "seq": seq, "minutes": receipt["minutes"], "service_date": receipt["service_date"], "reason": decision["reason"], "consent_id": decision["consent_id"], "consent_version": decision["consent_version"]},
            )
            connection.execute(
                "UPDATE receipt_batches SET processed=?, checkpoint_seq=?, updated_at=? WHERE id=?",
                (seq + 1, seq, now, batch_id),
            )
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        return self._row(row), True

    def list_receipts(self, plan_reference: Optional[str] = None, batch_id: Optional[int] = None, statuses: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if plan_reference is not None:
            clauses.append("plan_reference=?")
            params.append(plan_reference)
        if batch_id is not None:
            clauses.append("batch_id=?")
            params.append(batch_id)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" for _ in statuses))
            params.extend(statuses)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM receipts%s ORDER BY id" % where, params).fetchall()
        return [self._row(row) for row in rows]

    def rescore_receipt(self, receipt_id: int, decision: Dict[str, Any], actor_id: str) -> Tuple[Dict[str, Any], bool]:
        """依据变化后重算单条未确认回执。已入账(posted)的一律不动，状态无变化不写审计。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("回执不存在")
            if row["status"] == "posted":
                connection.rollback()
                return self._row(row), False
            if row["status"] == decision["status"] and (row["hold_reason"] or None) == (decision["reason"] or None):
                connection.rollback()
                return self._row(row), False
            posted_at = now if decision["status"] == "posted" else None
            connection.execute(
                "UPDATE receipts SET status=?, hold_reason=?, consent_id=?, consent_version=?, posted_at=?, updated_at=? WHERE id=?",
                (decision["status"], decision["reason"], decision["consent_id"], decision["consent_version"], posted_at, now, receipt_id),
            )
            if decision["status"] == "posted":
                connection.execute(
                    "INSERT INTO plan_ledger(plan_reference,baseline_minutes,receipt_minutes,version,updated_at) VALUES(?,?,0,1,?) ON CONFLICT(plan_reference) DO NOTHING",
                    (row["plan_reference"], decision["baseline_minutes"], now),
                )
                connection.execute(
                    "UPDATE plan_ledger SET receipt_minutes=receipt_minutes+?, version=version+1, updated_at=? WHERE plan_reference=?",
                    (row["minutes"], now, row["plan_reference"]),
                )
            self._add_event(
                connection, "receipt", row["external_id"], row["plan_reference"],
                "receipt_%s" % decision["status"], actor_id,
                {"recalculated": True, "minutes": row["minutes"], "service_date": row["service_date"], "reason": decision["reason"], "consent_id": decision["consent_id"], "consent_version": decision["consent_version"]},
            )
            updated = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            connection.commit()
        return self._row(updated), True

    # ---- 累计台账 ----

    def get_ledger(self, plan_reference: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM plan_ledger WHERE plan_reference=?", (plan_reference,)).fetchone()
        if row is None:
            return {"plan_reference": plan_reference, "baseline_minutes": 0, "receipt_minutes": 0, "version": 0, "updated_at": None}
        return self._row(row)

    # ---- 复查快照 ----

    def create_snapshot(self, snapshot: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "INSERT INTO snapshots(plan_reference,consent_id,consent_version,delivered_minutes,compliance_rate,receipt_count,status,basis,created_by,created_at) VALUES(?,?,?,?,?,?,'confirmed',?,?,?)",
                (snapshot["plan_reference"], snapshot["consent_id"], snapshot["consent_version"], snapshot["delivered_minutes"], snapshot["compliance_rate"], snapshot["receipt_count"], json.dumps(snapshot["basis"], ensure_ascii=False, sort_keys=True), actor_id, now),
            )
            snapshot_id = int(cursor.lastrowid)
            self._add_event(connection, "snapshot", snapshot_id, snapshot["plan_reference"], "snapshot_confirmed", actor_id, {"delivered_minutes": snapshot["delivered_minutes"], "compliance_rate": snapshot["compliance_rate"], "consent_version": snapshot["consent_version"]})
            row = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            connection.commit()
        return self._snapshot_row(row)

    def list_snapshots(self, plan_reference: str) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM snapshots WHERE plan_reference=? ORDER BY id", (plan_reference,)).fetchall()
        return [self._snapshot_row(row) for row in rows]

    def confirmed_receipt_ids(self, plan_reference: str) -> set:
        """已被确认快照覆盖的回执id集合（这些回执的依据已冻结，不参与重算）。"""
        confirmed = set()
        for snapshot in self.list_snapshots(plan_reference):
            confirmed.update(snapshot["basis"].get("receipt_ids", []))
        return confirmed

    # ---- 审计事件 ----

    def add_event(self, entity_type: str, entity_id: str, plan_reference: str, action: str, actor_id: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._add_event(connection, entity_type, entity_id, plan_reference, action, actor_id, details)
            connection.commit()

    def events(self, plan_reference: Optional[str] = None, entity_type: Optional[str] = None, entity_id: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        clauses, params = [], []
        if plan_reference is not None:
            clauses.append("plan_reference=?")
            params.append(plan_reference)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(str(entity_id))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM recon_events%s ORDER BY id DESC LIMIT ?" % where, (*params, limit)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result
