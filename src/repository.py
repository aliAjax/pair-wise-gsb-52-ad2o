"""SQLite 表结构与事务访问。

对账链的每条回执、每次依据变化和每张快照都在单事务内提交，
保证“写库失败即整体回滚到上一完整检查点”，重试不会重复累计分钟或追加审计。
"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from .domain import Conflict, NotFound
from .rules import (
    RECEIPT_POSTED,
    DomainRules,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        # 测试钩子：book_receipt 提交前按 (batch_id, seq) 触发，模拟写库失败。
        self.fault_hook: Optional[Callable[[int, int], None]] = None
        self._rules = DomainRules()
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
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS batches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    expected_version INTEGER NOT NULL,
                    total_items INTEGER NOT NULL,
                    checkpoint INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    items TEXT NOT NULL,
                    submitted_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS receipts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    batch_id INTEGER REFERENCES batches(id),
                    external_id TEXT NOT NULL UNIQUE,
                    service_date TEXT NOT NULL,
                    minutes INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    booked_seq INTEGER,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id),
                    status TEXT NOT NULL,
                    basis TEXT NOT NULL,
                    confirmed_by TEXT,
                    created_at TEXT NOT NULL,
                    confirmed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_batches_record ON batches(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_receipts_record ON receipts(record_id, id);
                CREATE INDEX IF NOT EXISTS idx_receipts_status ON receipts(record_id, status);
                CREATE INDEX IF NOT EXISTS idx_snapshots_record ON snapshots(record_id, id);
                """
            )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    @staticmethod
    def _receipt_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["minutes"] = int(item["minutes"])
        return item

    @staticmethod
    def _batch_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["items"] = json.loads(item["items"])
        for key in ("record_id", "expected_version", "total_items", "checkpoint"):
            item[key] = int(item[key])
        return item

    @staticmethod
    def _snapshot_row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["basis"] = json.loads(item["basis"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, _dump(payload), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, _dump({"state": state}), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, _dump(payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), _dump(details), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False

    # ---- 批次 ---------------------------------------------------------------------

    def create_batch(self, reference: str, record_id: int, expected_version: int, items: List[Dict[str, Any]], actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT state, version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if row["state"] != "active":
                connection.rollback()
                raise Conflict("仅生效中的计划可以回传服务回执")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("批次版本冲突，请刷新后重试")
            try:
                cursor = connection.execute(
                    "INSERT INTO batches(reference,record_id,expected_version,total_items,checkpoint,status,items,submitted_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,0,'processing',?,?,?,?)",
                    (reference, record_id, int(expected_version), len(items), _dump(items), actor_id, now, now),
                )
            except sqlite3.IntegrityError as exc:
                connection.rollback()
                raise Conflict("批次编号已存在：%s" % reference) from exc
            batch_id = int(cursor.lastrowid)
            result = connection.execute("SELECT * FROM batches WHERE id=?", (batch_id,)).fetchone()
            connection.commit()
        return self._batch_row(result)

    def get_batch(self, reference: str) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM batches WHERE reference=?", (reference,)).fetchone()
        if row is None:
            raise NotFound("批次不存在")
        return self._batch_row(row)

    def list_batches(self, record_id: int) -> List[Dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM batches WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        return [self._batch_row(row) for row in rows]

    def book_receipt(self, batch: Dict[str, Any], item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """登记单条回执，返回入账结果；外部编号已存在时返回 None（重传跳过，不累计不审计）。

        回执插入、状态判定、分钟重算、计划版本推进、审计与检查点更新在同一事务。
        """
        batch_id = int(batch["id"])
        record_id = int(batch["record_id"])
        seq = int(batch["checkpoint"]) + 1
        now = _now()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT id FROM receipts WHERE external_id=?", (item["external_id"],)
            ).fetchone()
            if exists is not None:
                # 重传回执：只推进检查点，分钟与审计保持原样。
                connection.execute(
                    "UPDATE batches SET checkpoint=?, updated_at=? WHERE id=?",
                    (seq, now, batch_id),
                )
                connection.commit()
                return None
            record_row = connection.execute("SELECT state, version, payload FROM records WHERE id=?", (record_id,)).fetchone()
            if record_row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            payload = json.loads(record_row["payload"])
            status = self._rules.classify_receipt(payload, item["service_date"])
            reason = "" if status == RECEIPT_POSTED else self._rules.reason_for(payload, item["service_date"])
            cursor = connection.execute(
                "INSERT INTO receipts(record_id,batch_id,external_id,service_date,minutes,status,reason,booked_seq,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (record_id, batch_id, item["external_id"], item["service_date"], int(item["minutes"]),
                 status, reason, seq, now, now),
            )
            receipt_id = int(cursor.lastrowid)
            totals = connection.execute(
                "SELECT status, SUM(minutes) AS m FROM receipts WHERE record_id=? GROUP BY status",
                (record_id,),
            ).fetchall()
            by_status = [{"status": str(r["status"]), "minutes": int(r["m"] or 0)} for r in totals]
            payload = self._rules.totals_from_receipts(payload, by_status)
            version = int(record_row["version"]) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (record_row["state"], version, _dump(payload), batch["submitted_by"], now, record_id),
            )
            action = "receipt_posted" if status == RECEIPT_POSTED else "receipt_pending"
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, batch["submitted_by"], version, _dump({
                    "batch_ref": batch["reference"],
                    "external_id": item["external_id"],
                    "service_date": item["service_date"],
                    "minutes": int(item["minutes"]),
                    "status": status,
                    "reason": reason,
                    "seq": seq,
                }), now),
            )
            connection.execute(
                "UPDATE batches SET checkpoint=?, updated_at=? WHERE id=?",
                (seq, now, batch_id),
            )
            if self.fault_hook is not None:
                self.fault_hook(batch_id, seq)
            connection.commit()
            receipt = connection.execute("SELECT * FROM receipts WHERE id=?", (receipt_id,)).fetchone()
            record = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return {"receipt": self._receipt_row(receipt), "record": self._row(record), "status": status, "version": version}

    def complete_batch(self, batch_id: int) -> None:
        now = _now()
        with self._connect() as connection:
            connection.execute(
                "UPDATE batches SET status='completed', checkpoint=total_items, updated_at=? WHERE id=?",
                (now, batch_id),
            )

    # ---- 回执查询 -----------------------------------------------------------------

    def list_receipts(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM receipts WHERE record_id=? ORDER BY service_date, id", (record_id,)
            ).fetchall()
        return [self._receipt_row(row) for row in rows]

    def locked_external_ids(self, record_id: int) -> set:
        """已确认快照冻结的回执外部编号：依据变化时不参与失效重算。"""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT basis FROM snapshots WHERE record_id=? AND status='confirmed'", (record_id,)
            ).fetchall()
        locked = set()
        for row in rows:
            basis = json.loads(row["basis"])
            for entry in basis.get("posted_fingerprint", []):
                locked.add(entry[0])
        return locked

    def pending_candidates(self, record_id: int, locked: Optional[set] = None) -> List[Dict[str, Any]]:
        """依据变化时参与重算的未确认回执：待核或此前因依据失效，且未被已确认快照冻结。"""
        locked = locked or set()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM receipts WHERE record_id=? AND status IN ('pending','invalid') ORDER BY service_date, id",
                (record_id,),
            ).fetchall()
        return [self._receipt_row(row) for row in rows if row["external_id"] not in locked]

    # ---- 依据变化重算 -------------------------------------------------------------

    def apply_basis_change(self, record_id: int, expected_version: int, new_state: str, new_payload: Dict[str, Any],
                           receipts: List[Dict[str, Any]], stale_snapshot_ids: List[int],
                           actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        """同意依据变化：原子地重写待核回执状态、失效未确认快照、推进版本并记一条审计。"""
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            for receipt in receipts:
                connection.execute(
                    "UPDATE receipts SET status=?, reason=?, updated_at=? WHERE id=? AND status IN ('pending','invalid')",
                    (receipt["status"], receipt.get("reason", ""), now, receipt["id"]),
                )
            if stale_snapshot_ids:
                placeholders = ",".join("?" for _ in stale_snapshot_ids)
                connection.execute(
                    "UPDATE snapshots SET status='stale' WHERE id IN (%s) AND status='unconfirmed'" % placeholders,
                    tuple(stale_snapshot_ids),
                )
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, _dump(new_payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    # ---- 快照 ---------------------------------------------------------------------

    def list_snapshots(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM snapshots WHERE record_id=? ORDER BY id", (record_id,)
            ).fetchall()
        return [self._snapshot_row(row) for row in rows]

    def get_latest_snapshot(self, record_id: int) -> Optional[Dict[str, Any]]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM snapshots WHERE record_id=? ORDER BY id DESC LIMIT 1", (record_id,)
            ).fetchone()
        return self._snapshot_row(row) if row else None

    def unconfirmed_snapshot_ids(self, record_id: int) -> List[int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM snapshots WHERE record_id=? AND status='unconfirmed'", (record_id,)
            ).fetchall()
        return [int(row["id"]) for row in rows]

    def create_snapshot(self, record_id: int, expected_version: int, new_state: str, new_payload: Dict[str, Any],
                        basis: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            connection.execute(
                "UPDATE snapshots SET status='stale' WHERE record_id=? AND status='unconfirmed'",
                (record_id,),
            )
            cursor = connection.execute(
                "INSERT INTO snapshots(record_id,status,basis,confirmed_by,created_at,confirmed_at) VALUES(?,?,?,?,?,NULL)",
                (record_id, "unconfirmed", _dump(basis), actor_id, now),
            )
            snapshot_id = int(cursor.lastrowid)
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (new_state, version, _dump(new_payload), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, _dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            snapshot = connection.execute("SELECT * FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            connection.commit()
        return {"record": self._row(result), "snapshot": self._snapshot_row(snapshot)}

    def confirm_snapshot(self, record_id: int, expected_version: int, actor_id: str) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            snapshot_row = connection.execute(
                "SELECT * FROM snapshots WHERE record_id=? AND status='unconfirmed' ORDER BY id DESC LIMIT 1",
                (record_id,),
            ).fetchone()
            if snapshot_row is None:
                connection.rollback()
                raise Conflict("没有待确认的复查快照")
            connection.execute(
                "UPDATE snapshots SET status='confirmed', confirmed_by=?, confirmed_at=? WHERE id=?",
                (actor_id, now, int(snapshot_row["id"])),
            )
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET version=?, updated_by=?, updated_at=? WHERE id=?",
                (version, actor_id, now, record_id),
            )
            details = {
                "summary": "复查快照已确认，依据已冻结",
                "snapshot_id": int(snapshot_row["id"]),
            }
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, "confirm_snapshot", actor_id, version, _dump(details), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            snapshot = connection.execute("SELECT * FROM snapshots WHERE id=?", (int(snapshot_row["id"]),)).fetchone()
            connection.commit()
        return {"record": self._row(result), "snapshot": self._snapshot_row(snapshot)}
