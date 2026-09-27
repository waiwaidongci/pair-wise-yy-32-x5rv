"""清场复核判定层：开工登记、残留读数填报、复核判定、失效规则与批次门禁。

存储见 clearance_store.py，页面见 static/clearance.html，本模块不直接写 SQL 之外的展示逻辑。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from clearance_store import ClearanceStore
from errors import ApiError


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse(value: str) -> datetime:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise ApiError(400, "时间格式不合法") from None


def judge_review(run: sqlite3.Row, readings: list[sqlite3.Row], reviewer: str, at: str) -> tuple[bool, list[str]]:
    """纯判定：同人、迟到、超限任一成立则维持待复核并给出全部原因。"""
    reasons: list[str] = []
    if reviewer in {run["registered_by"], run["filled_by"]}:
        reasons.append("同人复核")
    if _parse(at) > _parse(run["valid_until"]):
        reasons.append("复核超出有效期")
    over = [r["point"] for r in readings if r["over"]]
    if over:
        reasons.append("残留超限：" + "、".join(over))
    return (not reasons, reasons)


class ClearanceService:
    def __init__(self, store: ClearanceStore, audit, clock=None):
        self.store, self.audit = store, audit
        self.clock = clock or _utcnow

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _run(self, run_id: int) -> sqlite3.Row:
        row = self.store.run(run_id)
        if not row: raise ApiError(404, "清场记录不存在")
        return row

    def _equipment(self, equipment_id: int) -> sqlite3.Row:
        row = self.store.equipment(equipment_id)
        if not row: raise ApiError(404, "设备不存在")
        return row

    @staticmethod
    def _validate_points(points: object) -> list[dict]:
        if not isinstance(points, list) or not points: raise ApiError(400, "取样点不能为空")
        seen, out = set(), []
        for item in points:
            if not isinstance(item, dict): raise ApiError(400, "取样点格式不合法")
            point = str(item.get("point", "")).strip()
            try: limit = float(item.get("limit"))
            except (TypeError, ValueError): raise ApiError(400, f"取样点 {point or '?'} 限值不合法") from None
            if not point: raise ApiError(400, "取样点名称不能为空")
            if limit < 0: raise ApiError(400, "取样点限值不能为负")
            if point in seen: raise ApiError(400, f"取样点 {point} 重复")
            seen.add(point); out.append({"point": point, "limit": limit})
        return out

    def _future_valid_until(self, value: object) -> str:
        if not value: raise ApiError(400, "必须提供复核有效期")
        if _parse(str(value)) <= _parse(self.clock()): raise ApiError(400, "复核有效期必须晚于当前时间")
        return str(value)

    # ---- 设备台账 ----
    def register_equipment(self, actor: str | None, role: str | None, factory_id: int, code: str, name: str) -> dict:
        actor = self._actor(actor, role, {"qa"})
        if not self.store.factory(factory_id): raise ApiError(404, "工厂不存在")
        code, name = (code or "").strip(), (name or "").strip()
        if not code or not name: raise ApiError(400, "设备代号和名称不能为空")
        try:
            with self.store.conn:
                equipment_id = self.store.add_equipment(factory_id, code, name)
                self.audit(actor, "clearance.equipment", "equipment", equipment_id, {"factory_id": factory_id, "code": code})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "设备代号已存在") from exc
        return dict(self.store.equipment(equipment_id))

    # ---- 开工登记 ----
    def register_run(self, actor: str | None, role: str | None, factory_id: int, equipment_id: int,
                     prev_product: str, prev_batch_no: str, points: object, valid_until: object) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        if not self.store.factory(factory_id): raise ApiError(404, "工厂不存在")
        equipment = self._equipment(equipment_id)
        if int(equipment["factory_id"]) != int(factory_id): raise ApiError(403, "不能操作其他工厂的设备")
        prev_product, prev_batch_no = (prev_product or "").strip(), (prev_batch_no or "").strip()
        if not prev_product or not prev_batch_no: raise ApiError(400, "上一品种和上一批次不能为空")
        points = self._validate_points(points)
        valid_until = self._future_valid_until(valid_until)
        try:
            with self.store.conn:
                if self.store.active_run(equipment_id): raise ApiError(409, "设备被占用，不得开工")
                version = self.store.next_version(equipment_id)
                run_id = self.store.insert_run(equipment_id, version, prev_product, prev_batch_no,
                                               json.dumps(points, ensure_ascii=False), valid_until, actor, self.clock(), None)
                self.audit(actor, "clearance.register", "clearance_run", run_id,
                           {"equipment_id": equipment_id, "version": version, "prev_product": prev_product, "prev_batch_no": prev_batch_no})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "设备被占用，不得开工") from exc
        return self.run_detail(run_id)

    # ---- 清洁后填残留读数 ----
    def fill_readings(self, actor: str | None, role: str | None, run_id: int, readings: object) -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        run = self._run(run_id)
        if run["status"] == "superseded": raise ApiError(409, "记录已失效，不能填写读数")
        if run["status"] != "cleaning": raise ApiError(409, "残留读数已填写，如需重测请重新取样")
        points = {p["point"]: p["limit"] for p in json.loads(run["points_json"])}
        if not isinstance(readings, list) or not readings: raise ApiError(400, "残留读数不能为空")
        seen, rows = set(), []
        for item in readings:
            if not isinstance(item, dict): raise ApiError(400, "残留读数格式不合法")
            point = str(item.get("point", "")).strip()
            if point not in points: raise ApiError(400, f"取样点 {point or '?'} 未登记")
            if point in seen: raise ApiError(400, f"取样点 {point} 重复填写")
            try: value = float(item.get("value"))
            except (TypeError, ValueError): raise ApiError(400, f"取样点 {point} 读数不合法") from None
            if value < 0: raise ApiError(400, "残留读数不能为负")
            seen.add(point); rows.append((point, value, points[point]))
        missing = set(points) - seen
        if missing: raise ApiError(400, "缺少取样点读数：" + "、".join(sorted(missing)))
        at = self.clock()
        with self.store.conn:
            for point, value, limit in rows:
                self.store.add_reading(run_id, point, value, limit, int(value > limit), actor, at)
            if self.store.mark_filled(run_id, actor, at) != 1: raise ApiError(409, "残留读数已填写，如需重测请重新取样")
            self.audit(actor, "clearance.fill", "clearance_run", run_id,
                       {"points": len(rows), "over": [p for p, v, l in rows if v > l]})
        return self.run_detail(run_id)

    # ---- 复核判定 ----
    def review(self, actor: str | None, role: str | None, run_id: int) -> dict:
        actor = self._actor(actor, role, {"inspector"})
        run = self._run(run_id)
        if run["status"] == "superseded": raise ApiError(409, "记录已失效，不能复核")
        if run["status"] == "cleaning": raise ApiError(409, "尚未填写残留读数")
        if run["status"] == "passed": raise ApiError(409, "清场已复核通过")
        at = self.clock()
        ok, reasons = judge_review(run, self.store.readings(run_id), actor, at)
        with self.store.conn:
            if ok:
                self.store.mark_passed(run_id, actor, at)
                self.store.add_review(run_id, "passed", None, actor, at)
            else:
                reason = "；".join(reasons)
                self.store.mark_held(run_id, reason)
                self.store.add_review(run_id, "held", reason, actor, at)
            self.audit(actor, "clearance.review", "clearance_run", run_id,
                       {"outcome": "passed" if ok else "held", "reasons": reasons})
        return self.run_detail(run_id)

    # ---- 失效：改品种、参数或重新取样 ----
    def revise(self, actor: str | None, role: str | None, run_id: int, prev_product: str | None = None,
               prev_batch_no: str | None = None, points: object = None, valid_until: object = None, note: str = "") -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        run = self._run(run_id)
        if run["status"] == "superseded": raise ApiError(409, "记录已失效，请在最新版本上操作")
        new_product = (prev_product or "").strip() or run["prev_product"]
        new_batch = (prev_batch_no or "").strip() or run["prev_batch_no"]
        new_points = self._validate_points(points) if points else json.loads(run["points_json"])
        new_valid = self._future_valid_until(valid_until or run["valid_until"])
        return self._supersede("clearance.revise", actor, run, new_product, new_batch, new_points, new_valid, (note or "").strip() or "修改品种或参数")

    def resample(self, actor: str | None, role: str | None, run_id: int, valid_until: object = None, note: str = "") -> dict:
        actor = self._actor(actor, role, {"operator", "inspector"})
        run = self._run(run_id)
        if run["status"] == "superseded": raise ApiError(409, "记录已失效，请在最新版本上操作")
        new_valid = self._future_valid_until(valid_until or run["valid_until"])
        return self._supersede("clearance.resample", actor, run, run["prev_product"], run["prev_batch_no"],
                               json.loads(run["points_json"]), new_valid, (note or "").strip() or "重新取样")

    def _supersede(self, action: str, actor: str, run: sqlite3.Row, prev_product: str, prev_batch_no: str,
                   points: list[dict], valid_until: str, change_note: str) -> dict:
        at = self.clock()
        with self.store.conn:
            self.store.mark_superseded(run["id"], at)
            new_id = self.store.insert_run(run["equipment_id"], self.store.next_version(run["equipment_id"]),
                                           prev_product, prev_batch_no, json.dumps(points, ensure_ascii=False),
                                           valid_until, actor, at, change_note)
            self.audit(actor, action, "clearance_run", new_id, {"old_id": run["id"], "change_note": change_note})
        return self.run_detail(new_id)

    # ---- 批次门禁 ----
    def assert_equipment_available(self, factory_id: int, equipment_id: int) -> None:
        equipment = self._equipment(equipment_id)
        if int(equipment["factory_id"]) != int(factory_id): raise ApiError(403, "设备不属于该工厂")
        if self.store.active_run(equipment_id): raise ApiError(409, "设备被占用，不得开工")

    def require_passed(self, equipment_id: int, action: str) -> None:
        latest = self.store.latest_run(equipment_id)
        if latest is None or latest["status"] != "passed":
            raise ApiError(409, f"清场通过前不能{action}")

    # ---- 查询（旧记录仍可查） ----
    def equipment_list(self, factory_id: int | None = None) -> list[dict]:
        return [dict(row) for row in self.store.equipment_list(factory_id)]

    def recent_runs(self, limit: int = 50) -> list[dict]:
        return [self._run_dict(row) for row in self.store.recent_runs(limit)]

    def run_detail(self, run_id: int) -> dict:
        run = self._run(run_id)
        return {"run": self._run_dict(run),
                "readings": [dict(row) for row in self.store.readings(run_id)],
                "reviews": [dict(row) for row in self.store.reviews(run_id)]}

    def equipment_history(self, equipment_id: int) -> dict:
        equipment = self._equipment(equipment_id)
        return {"equipment": dict(equipment),
                "runs": [self.run_detail(row["id"]) for row in self.store.runs_for_equipment(equipment_id)]}

    def factory_board(self, factory_id: int) -> dict:
        if not self.store.factory(factory_id): raise ApiError(404, "工厂不存在")
        runs = []
        for row in self.store.runs_for_factory(factory_id):
            detail = self.run_detail(row["id"])
            detail["run"]["equipment_code"] = row["equipment_code"]
            runs.append(detail)
        return {"equipment": self.equipment_list(factory_id), "runs": runs}

    @staticmethod
    def _run_dict(row: sqlite3.Row) -> dict:
        return {"id": row["id"], "equipment_id": row["equipment_id"], "version": row["version"], "status": row["status"],
                "prev_product": row["prev_product"], "prev_batch_no": row["prev_batch_no"],
                "points": json.loads(row["points_json"]), "valid_until": row["valid_until"],
                "registered_by": row["registered_by"], "registered_at": row["registered_at"],
                "filled_by": row["filled_by"], "filled_at": row["filled_at"],
                "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
                "blocked_reason": row["blocked_reason"], "change_note": row["change_note"], "superseded_at": row["superseded_at"]}
