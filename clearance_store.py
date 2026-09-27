"""清场复核存储层：设备、清场记录、残留读数与复核记录的表结构和读写。

只负责持久化，不包含判定规则；判定见 clearance_service.py，页面见 static/clearance.html。
"""
from __future__ import annotations

import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS equipment (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  factory_id INTEGER NOT NULL REFERENCES factories(id),
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(factory_id, code)
);
CREATE TABLE IF NOT EXISTS clearances (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  equipment_id INTEGER NOT NULL REFERENCES equipment(id),
  factory_id INTEGER NOT NULL,
  revision INTEGER NOT NULL DEFAULT 1,
  parent_id INTEGER REFERENCES clearances(id),
  prev_product TEXT NOT NULL,
  prev_batch_no TEXT NOT NULL,
  sampling_points TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('cleaning','pending_review','passed','invalidated')),
  blocked_reason TEXT,
  review_window_hours INTEGER NOT NULL DEFAULT 24,
  review_valid_until TEXT,
  registered_by TEXT NOT NULL,
  registered_at TEXT NOT NULL,
  cleaned_by TEXT,
  cleaned_at TEXT,
  reviewed_by TEXT,
  reviewed_at TEXT,
  invalidated_reason TEXT,
  invalidated_at TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clearances_equipment ON clearances(equipment_id, id);
CREATE TABLE IF NOT EXISTS clearance_readings (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clearance_id INTEGER NOT NULL REFERENCES clearances(id),
  point TEXT NOT NULL,
  reading REAL NOT NULL,
  limit_value REAL NOT NULL,
  over_limit INTEGER NOT NULL,
  recorded_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(clearance_id, point)
);
CREATE TABLE IF NOT EXISTS clearance_reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  clearance_id INTEGER NOT NULL REFERENCES clearances(id),
  reviewer TEXT NOT NULL,
  result TEXT NOT NULL CHECK(result IN ('passed','rejected')),
  reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
"""

STATUS_LABELS = {"cleaning": "清洁中", "pending_review": "待复核", "passed": "已通过", "invalidated": "已失效"}


class ClearanceStore:
    """挂在共享 sqlite 连接上的清场存储，不做任何业务判断。"""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def init_schema(self) -> None:
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- 设备 ----
    def insert_equipment(self, factory_id: int, code: str, name: str, actor: str, stamp: str) -> int:
        cur = self.conn.execute("INSERT INTO equipment(factory_id,code,name,created_by,created_at) VALUES(?,?,?,?,?)",
                                (factory_id, code, name, actor, stamp))
        return cur.lastrowid

    def get_equipment(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()

    def list_equipment(self, factory_id: int | None = None) -> list[sqlite3.Row]:
        if factory_id is None:
            return self.conn.execute("SELECT * FROM equipment ORDER BY id").fetchall()
        return self.conn.execute("SELECT * FROM equipment WHERE factory_id=? ORDER BY id", (factory_id,)).fetchall()

    # ---- 清场记录 ----
    def insert_clearance(self, equipment_id: int, factory_id: int, revision: int, parent_id: int | None,
                         prev_product: str, prev_batch_no: str, sampling_points_json: str, status: str,
                         review_window_hours: int, registered_by: str, stamp: str) -> int:
        cur = self.conn.execute("""INSERT INTO clearances(equipment_id,factory_id,revision,parent_id,prev_product,prev_batch_no,
                                   sampling_points,status,review_window_hours,registered_by,registered_at,created_at,updated_at)
                                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                                (equipment_id, factory_id, revision, parent_id, prev_product, prev_batch_no,
                                 sampling_points_json, status, review_window_hours, registered_by, stamp, stamp, stamp))
        return cur.lastrowid

    def get_clearance(self, clearance_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clearances WHERE id=?", (clearance_id,)).fetchone()

    def list_clearances(self, equipment_id: int | None = None, status: str | None = None) -> list[sqlite3.Row]:
        sql = ("SELECT c.*, e.code AS equipment_code, e.name AS equipment_name "
               "FROM clearances c JOIN equipment e ON e.id=c.equipment_id")
        clauses, args = [], []
        if equipment_id is not None: clauses.append("c.equipment_id=?"); args.append(equipment_id)
        if status is not None: clauses.append("c.status=?"); args.append(status)
        if clauses: sql += " WHERE " + " AND ".join(clauses)
        return self.conn.execute(sql + " ORDER BY c.id DESC", args).fetchall()

    def active_clearance(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM clearances WHERE equipment_id=? AND status IN ('cleaning','pending_review')
                                    ORDER BY id DESC LIMIT 1""", (equipment_id,)).fetchone()

    def latest_clearance(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clearances WHERE equipment_id=? ORDER BY id DESC LIMIT 1", (equipment_id,)).fetchone()

    def history_of(self, clearance_id: int) -> list[sqlite3.Row]:
        """沿 parent_id 回溯已失效的旧版本，旧记录仍可查。"""
        chain, row = [], self.get_clearance(clearance_id)
        while row is not None and row["parent_id"] is not None:
            row = self.get_clearance(row["parent_id"])
            if row is not None: chain.append(row)
        return chain

    def mark_pending(self, clearance_id: int, cleaned_by: str, stamp: str, valid_until: str) -> None:
        self.conn.execute("""UPDATE clearances SET status='pending_review',cleaned_by=?,cleaned_at=?,
                             review_valid_until=?,blocked_reason=NULL,updated_at=? WHERE id=?""",
                          (cleaned_by, stamp, valid_until, stamp, clearance_id))

    def mark_blocked(self, clearance_id: int, reason: str, stamp: str) -> None:
        self.conn.execute("UPDATE clearances SET blocked_reason=?,updated_at=? WHERE id=? AND status='pending_review'",
                          (reason, stamp, clearance_id))

    def mark_passed(self, clearance_id: int, reviewer: str, stamp: str) -> None:
        self.conn.execute("""UPDATE clearances SET status='passed',reviewed_by=?,reviewed_at=?,blocked_reason=NULL,updated_at=?
                             WHERE id=?""", (reviewer, stamp, stamp, clearance_id))

    def mark_invalidated(self, clearance_id: int, reason: str, stamp: str) -> None:
        self.conn.execute("UPDATE clearances SET status='invalidated',invalidated_reason=?,invalidated_at=?,updated_at=? WHERE id=?",
                          (reason, stamp, stamp, clearance_id))

    # ---- 残留读数 ----
    def insert_reading(self, clearance_id: int, point: str, reading: float, limit_value: float,
                       over_limit: int, actor: str, stamp: str) -> int:
        cur = self.conn.execute("""INSERT INTO clearance_readings(clearance_id,point,reading,limit_value,over_limit,recorded_by,created_at)
                                   VALUES(?,?,?,?,?,?,?)""",
                                (clearance_id, point, reading, limit_value, over_limit, actor, stamp))
        return cur.lastrowid

    def readings_for(self, clearance_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clearance_readings WHERE clearance_id=? ORDER BY id", (clearance_id,)).fetchall()

    # ---- 复核记录 ----
    def insert_review(self, clearance_id: int, reviewer: str, result: str, reason: str, stamp: str) -> int:
        cur = self.conn.execute("INSERT INTO clearance_reviews(clearance_id,reviewer,result,reason,created_at) VALUES(?,?,?,?,?)",
                                (clearance_id, reviewer, result, reason, stamp))
        return cur.lastrowid

    def reviews_for(self, clearance_id: int) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM clearance_reviews WHERE clearance_id=? ORDER BY id", (clearance_id,)).fetchall()
