"""清场复核存储层：设备台账、清场记录版本、残留读数与复核记录的建表和读写。

只负责 SQL 与数据存取，不包含任何判定规则（判定见 clearance_service.py，
页面见 static/clearance.html）。
"""
from __future__ import annotations

import sqlite3


class ClearanceStore:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.init_schema()

    def init_schema(self) -> None:
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS equipment (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          factory_id INTEGER NOT NULL REFERENCES factories(id),
          code TEXT NOT NULL, name TEXT NOT NULL,
          UNIQUE(factory_id, code)
        );
        CREATE TABLE IF NOT EXISTS clearance_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          equipment_id INTEGER NOT NULL REFERENCES equipment(id),
          version INTEGER NOT NULL,
          status TEXT NOT NULL CHECK(status IN ('cleaning','pending_review','passed','superseded')),
          prev_product TEXT NOT NULL, prev_batch_no TEXT NOT NULL,
          points_json TEXT NOT NULL, valid_until TEXT NOT NULL,
          registered_by TEXT NOT NULL, registered_at TEXT NOT NULL,
          filled_by TEXT, filled_at TEXT,
          reviewed_by TEXT, reviewed_at TEXT,
          blocked_reason TEXT, change_note TEXT, superseded_at TEXT,
          UNIQUE(equipment_id, version)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_run_per_equipment
          ON clearance_runs(equipment_id) WHERE status IN ('cleaning','pending_review');
        CREATE TABLE IF NOT EXISTS clearance_readings (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL REFERENCES clearance_runs(id),
          point TEXT NOT NULL, value REAL NOT NULL, limit_value REAL NOT NULL,
          over INTEGER NOT NULL, recorded_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS clearance_reviews (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          run_id INTEGER NOT NULL REFERENCES clearance_runs(id),
          outcome TEXT NOT NULL CHECK(outcome IN ('passed','held')),
          reason TEXT, reviewed_by TEXT NOT NULL, created_at TEXT NOT NULL
        );
        """)
        self.conn.commit()

    # ---- 工厂 / 设备 ----
    def factory(self, factory_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM factories WHERE id=?", (factory_id,)).fetchone()

    def add_equipment(self, factory_id: int, code: str, name: str) -> int:
        cur = self.conn.execute("INSERT INTO equipment(factory_id,code,name) VALUES(?,?,?)", (factory_id, code, name))
        return cur.lastrowid

    def equipment(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()

    def equipment_list(self, factory_id: int | None = None) -> list[sqlite3.Row]:
        if factory_id is None:
            return list(self.conn.execute("SELECT * FROM equipment ORDER BY id"))
        return list(self.conn.execute("SELECT * FROM equipment WHERE factory_id=? ORDER BY id", (factory_id,)))

    # ---- 清场记录 ----
    def insert_run(self, equipment_id: int, version: int, prev_product: str, prev_batch_no: str,
                   points_json: str, valid_until: str, actor: str, at: str, change_note: str | None) -> int:
        cur = self.conn.execute("""INSERT INTO clearance_runs(equipment_id,version,status,prev_product,prev_batch_no,
                                     points_json,valid_until,registered_by,registered_at,change_note)
                                   VALUES(?,?,'cleaning',?,?,?,?,?,?,?)""",
                                (equipment_id, version, prev_product, prev_batch_no, points_json, valid_until, actor, at, change_note))
        return cur.lastrowid

    def run(self, run_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clearance_runs WHERE id=?", (run_id,)).fetchone()

    def active_run(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("""SELECT * FROM clearance_runs WHERE equipment_id=? AND status IN ('cleaning','pending_review')
                                    ORDER BY version DESC LIMIT 1""", (equipment_id,)).fetchone()

    def latest_run(self, equipment_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM clearance_runs WHERE equipment_id=? ORDER BY version DESC LIMIT 1", (equipment_id,)).fetchone()

    def next_version(self, equipment_id: int) -> int:
        return self.conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM clearance_runs WHERE equipment_id=?", (equipment_id,)).fetchone()[0]

    def mark_filled(self, run_id: int, actor: str, at: str) -> int:
        return self.conn.execute("""UPDATE clearance_runs SET status='pending_review',filled_by=?,filled_at=?
                                    WHERE id=? AND status='cleaning'""", (actor, at, run_id)).rowcount

    def mark_passed(self, run_id: int, reviewer: str, at: str) -> None:
        self.conn.execute("""UPDATE clearance_runs SET status='passed',reviewed_by=?,reviewed_at=?,blocked_reason=NULL
                             WHERE id=? AND status='pending_review'""", (reviewer, at, run_id))

    def mark_held(self, run_id: int, reason: str) -> None:
        self.conn.execute("UPDATE clearance_runs SET blocked_reason=? WHERE id=? AND status='pending_review'", (reason, run_id))

    def mark_superseded(self, run_id: int, at: str) -> None:
        self.conn.execute("UPDATE clearance_runs SET status='superseded',superseded_at=? WHERE id=? AND status!='superseded'", (at, run_id))

    def runs_for_equipment(self, equipment_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM clearance_runs WHERE equipment_id=? ORDER BY version DESC", (equipment_id,)))

    def runs_for_factory(self, factory_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("""SELECT c.*, e.code AS equipment_code FROM clearance_runs c
                                         JOIN equipment e ON e.id=c.equipment_id
                                         WHERE e.factory_id=? ORDER BY c.id DESC""", (factory_id,)))

    def recent_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM clearance_runs ORDER BY id DESC LIMIT ?", (limit,)))

    # ---- 残留读数 / 复核记录 ----
    def add_reading(self, run_id: int, point: str, value: float, limit_value: float, over: int, actor: str, at: str) -> None:
        self.conn.execute("""INSERT INTO clearance_readings(run_id,point,value,limit_value,over,recorded_by,created_at)
                             VALUES(?,?,?,?,?,?,?)""", (run_id, point, value, limit_value, over, actor, at))

    def readings(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM clearance_readings WHERE run_id=? ORDER BY id", (run_id,)))

    def add_review(self, run_id: int, outcome: str, reason: str | None, reviewer: str, at: str) -> None:
        self.conn.execute("INSERT INTO clearance_reviews(run_id,outcome,reason,reviewed_by,created_at) VALUES(?,?,?,?,?)",
                          (run_id, outcome, reason, reviewer, at))

    def reviews(self, run_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM clearance_reviews WHERE run_id=? ORDER BY id", (run_id,)))
