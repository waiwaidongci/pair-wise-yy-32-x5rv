"""清场复核判定层：开工登记、残留读数、复核判定、失效重建与批次放行门禁。

存储见 clearance_store.py，页面见 static/clearance.html。
"""
from __future__ import annotations

import json
import sqlite3

from common import ApiError, after_now, hours_from_now, now, require_actor
from clearance_store import STATUS_LABELS, ClearanceStore

DEFAULT_REVIEW_WINDOW_HOURS = 24


class ClearanceService:
    def __init__(self, store: ClearanceStore, audit):
        self.store, self.audit, self.conn = store, audit, store.conn

    # ---- 设备 ----
    def register_equipment(self, actor: str | None, role: str | None, factory_id: int, code: str, name: str) -> dict:
        actor = require_actor(actor, role, {"operator", "qa"})
        if not self.conn.execute("SELECT id FROM factories WHERE id=?", (factory_id,)).fetchone():
            raise ApiError(404, "工厂不存在")
        if not str(code).strip() or not str(name).strip(): raise ApiError(400, "设备代号和名称不能为空")
        try:
            with self.conn:
                equipment_id = self.store.insert_equipment(factory_id, code.strip(), name.strip(), actor, now())
                self.audit(actor, "equipment.register", "equipment", equipment_id, {"factory_id": factory_id, "code": code})
        except sqlite3.IntegrityError as exc: raise ApiError(409, "设备代号已存在") from exc
        return self._equipment_dict(self.store.get_equipment(equipment_id))

    def list_equipment(self, factory_id: int | None = None) -> dict:
        return {"equipment": [self._equipment_dict(row) for row in self.store.list_equipment(factory_id)]}

    def _equipment_dict(self, row: sqlite3.Row) -> dict:
        active = self.store.active_clearance(row["id"])
        return {"id": row["id"], "factory_id": row["factory_id"], "code": row["code"], "name": row["name"],
                "active_clearance_id": active["id"] if active else None,
                "active_status": active["status"] if active else None,
                "active_status_label": STATUS_LABELS[active["status"]] if active else None}

    def _equipment(self, equipment_id: int, factory_id: int) -> sqlite3.Row:
        row = self.store.get_equipment(equipment_id)
        if not row: raise ApiError(404, "设备不存在")
        if int(row["factory_id"]) != int(factory_id): raise ApiError(403, "不能操作其他工厂的设备")
        return row

    def _clearance(self, clearance_id: int, factory_id: int) -> sqlite3.Row:
        row = self.store.get_clearance(clearance_id)
        if not row: raise ApiError(404, "清场记录不存在")
        if int(row["factory_id"]) != int(factory_id): raise ApiError(403, "不能操作其他工厂的清场记录")
        return row

    # ---- 开工登记 ----
    def open_clearance(self, actor: str | None, role: str | None, factory_id: int, equipment_id: int,
                       prev_product: str, prev_batch_no: str, sampling_points: list,
                       review_window_hours: int = DEFAULT_REVIEW_WINDOW_HOURS) -> dict:
        actor = require_actor(actor, role, {"operator", "inspector"})
        equipment = self._equipment(equipment_id, factory_id)
        if not str(prev_product).strip() or not str(prev_batch_no).strip():
            raise ApiError(400, "上一品种和上一批次不能为空")
        points = self._validate_points(sampling_points)
        try: window = int(review_window_hours)
        except (TypeError, ValueError): raise ApiError(400, "复核有效期不合法") from None
        if not 1 <= window <= 720: raise ApiError(400, "复核有效期需在 1-720 小时之间")
        if self.store.active_clearance(equipment_id): raise ApiError(409, "设备被占用，不得开工")
        stamp = now()
        with self.conn:
            clearance_id = self.store.insert_clearance(equipment_id, int(equipment["factory_id"]), 1, None,
                                                       prev_product.strip(), prev_batch_no.strip(),
                                                       self._points_json(points), "cleaning", window, actor, stamp)
            self.audit(actor, "clearance.open", "clearance", clearance_id,
                       {"equipment_id": equipment_id, "prev_product": prev_product, "prev_batch_no": prev_batch_no})
        return self.detail(clearance_id)

    # ---- 残留读数 ----
    def record_readings(self, actor: str | None, role: str | None, factory_id: int, clearance_id: int, readings: list) -> dict:
        actor = require_actor(actor, role, {"operator", "inspector"})
        clearance = self._clearance(clearance_id, factory_id)
        if clearance["status"] == "invalidated": raise ApiError(409, "清场记录已失效，请操作最新版本")
        values = self._validate_readings(clearance, readings)
        target = clearance
        if self.store.readings_for(clearance_id):
            # 重新取样：原结论（含已通过）失效，生成新版本，旧记录保留可查
            target = self._supersede(clearance, actor, "重新取样，原结论失效")
        self._fill_readings(target, values, actor)
        return self.detail(target["id"])

    def _fill_readings(self, clearance: sqlite3.Row, values: dict[str, float], actor: str) -> None:
        stamp = now()
        valid_until = hours_from_now(clearance["review_window_hours"])
        points = json.loads(clearance["sampling_points"])
        with self.conn:
            for point in points:
                value = values[point["name"]]
                self.store.insert_reading(clearance["id"], point["name"], value, point["limit"],
                                          int(value > point["limit"]), actor, stamp)
            self.store.mark_pending(clearance["id"], actor, stamp, valid_until)
            over = [p["name"] for p in points if values[p["name"]] > p["limit"]]
            self.audit(actor, "clearance.readings", "clearance", clearance["id"],
                       {"over_limit": over, "review_valid_until": valid_until})

    # ---- 复核 ----
    def review(self, actor: str | None, role: str | None, factory_id: int, clearance_id: int) -> dict:
        actor = require_actor(actor, role, {"inspector", "qa"})
        clearance = self._clearance(clearance_id, factory_id)
        if clearance["status"] == "invalidated": raise ApiError(409, "清场记录已失效，请复核最新版本")
        if clearance["status"] != "pending_review": raise ApiError(409, "当前状态不能复核")
        reasons = []
        over = [r["point"] for r in self.store.readings_for(clearance_id) if r["over_limit"]]
        if over: reasons.append("残留读数超限：" + "、".join(over))
        if not after_now(clearance["review_valid_until"]): reasons.append("复核超出有效期")
        if actor in {clearance["registered_by"], clearance["cleaned_by"]}: reasons.append("同一人复核")
        stamp = now()
        with self.conn:
            if reasons:
                reason = "；".join(reasons)
                self.store.insert_review(clearance_id, actor, "rejected", reason, stamp)
                self.store.mark_blocked(clearance_id, reason, stamp)  # 停在待复核并写明原因
                self.audit(actor, "clearance.review_rejected", "clearance", clearance_id, {"reason": reason})
            else:
                self.store.insert_review(clearance_id, actor, "passed", "", stamp)
                self.store.mark_passed(clearance_id, actor, stamp)
                self.audit(actor, "clearance.review_passed", "clearance", clearance_id, {})
        return self.detail(clearance_id)

    # ---- 修改品种/参数 ----
    def amend(self, actor: str | None, role: str | None, factory_id: int, clearance_id: int,
              prev_product: str, prev_batch_no: str, sampling_points: list) -> dict:
        actor = require_actor(actor, role, {"operator", "inspector"})
        clearance = self._clearance(clearance_id, factory_id)
        if clearance["status"] == "invalidated": raise ApiError(409, "清场记录已失效，请修改最新版本")
        if not str(prev_product).strip() or not str(prev_batch_no).strip():
            raise ApiError(400, "上一品种和上一批次不能为空")
        points_json = self._points_json(self._validate_points(sampling_points))
        if (prev_product == clearance["prev_product"] and prev_batch_no == clearance["prev_batch_no"]
                and points_json == clearance["sampling_points"]):
            raise ApiError(400, "未做任何修改")
        new_row = self._supersede(clearance, actor, "修改品种或参数，原结论失效",
                                  prev_product=prev_product.strip(), prev_batch_no=prev_batch_no.strip(),
                                  sampling_points_json=points_json)
        return self.detail(new_row["id"])

    def _supersede(self, clearance: sqlite3.Row, actor: str, reason: str, *, prev_product: str | None = None,
                   prev_batch_no: str | None = None, sampling_points_json: str | None = None) -> sqlite3.Row:
        """作废旧记录并开出下一版本；旧记录保留，仍可查询。"""
        stamp = now()
        with self.conn:
            self.store.mark_invalidated(clearance["id"], reason, stamp)
            new_id = self.store.insert_clearance(
                clearance["equipment_id"], clearance["factory_id"], clearance["revision"] + 1, clearance["id"],
                prev_product if prev_product is not None else clearance["prev_product"],
                prev_batch_no if prev_batch_no is not None else clearance["prev_batch_no"],
                sampling_points_json if sampling_points_json is not None else clearance["sampling_points"],
                "cleaning", clearance["review_window_hours"], actor, stamp)
            self.audit(actor, "clearance.invalidate", "clearance", clearance["id"], {"reason": reason, "new_id": new_id})
            self.audit(actor, "clearance.reopen", "clearance", new_id,
                       {"parent_id": clearance["id"], "revision": clearance["revision"] + 1})
        return self.store.get_clearance(new_id)

    # ---- 批次门禁 ----
    def gate_for_batch(self, batch: sqlite3.Row, action: str) -> None:
        """批次挂了设备时，设备最新清场未通过就阻止补录检验或申请放行。"""
        if batch["equipment_id"] is None: return
        latest = self.store.latest_clearance(batch["equipment_id"])
        if latest is None: raise ApiError(409, f"设备缺少清场记录，不能{action}")
        if latest["status"] != "passed":
            raise ApiError(409, f"清场未通过（{STATUS_LABELS[latest['status']]}），不能{action}")

    # ---- 查询 ----
    def detail(self, clearance_id: int) -> dict:
        row = self.store.get_clearance(clearance_id)
        if not row: raise ApiError(404, "清场记录不存在")
        return {"clearance": self._clearance_dict(row),
                "readings": [self._reading_dict(r) for r in self.store.readings_for(clearance_id)],
                "reviews": [self._review_dict(r) for r in self.store.reviews_for(clearance_id)],
                "history": [self._clearance_dict(r) for r in self.store.history_of(clearance_id)]}

    def list_clearances(self, equipment_id: int | None = None, status: str | None = None) -> dict:
        if status is not None and status not in STATUS_LABELS: raise ApiError(400, "状态筛选不合法")
        return {"clearances": [self._clearance_dict(r) for r in self.store.list_clearances(equipment_id, status)]}

    @staticmethod
    def _clearance_dict(row: sqlite3.Row) -> dict:
        data = {"id": row["id"], "equipment_id": row["equipment_id"], "factory_id": row["factory_id"],
                "revision": row["revision"], "parent_id": row["parent_id"],
                "prev_product": row["prev_product"], "prev_batch_no": row["prev_batch_no"],
                "sampling_points": json.loads(row["sampling_points"]),
                "status": row["status"], "status_label": STATUS_LABELS[row["status"]],
                "blocked_reason": row["blocked_reason"],
                "review_window_hours": row["review_window_hours"], "review_valid_until": row["review_valid_until"],
                "registered_by": row["registered_by"], "registered_at": row["registered_at"],
                "cleaned_by": row["cleaned_by"], "cleaned_at": row["cleaned_at"],
                "reviewed_by": row["reviewed_by"], "reviewed_at": row["reviewed_at"],
                "invalidated_reason": row["invalidated_reason"], "invalidated_at": row["invalidated_at"]}
        if "equipment_code" in row.keys():
            data["equipment_code"], data["equipment_name"] = row["equipment_code"], row["equipment_name"]
        return data

    @staticmethod
    def _reading_dict(row: sqlite3.Row) -> dict:
        return {"point": row["point"], "reading": row["reading"], "limit": row["limit_value"],
                "over_limit": bool(row["over_limit"]), "recorded_by": row["recorded_by"], "created_at": row["created_at"]}

    @staticmethod
    def _review_dict(row: sqlite3.Row) -> dict:
        return {"reviewer": row["reviewer"], "result": row["result"], "reason": row["reason"], "created_at": row["created_at"]}

    # ---- 校验 ----
    @staticmethod
    def _validate_points(raw: list) -> list[dict]:
        if not isinstance(raw, list) or not raw: raise ApiError(400, "取样点不能为空")
        points, seen = [], set()
        for item in raw:
            name = str(item.get("name", "")).strip() if isinstance(item, dict) else ""
            try: limit = float(item.get("limit")) if isinstance(item, dict) else None
            except (TypeError, ValueError): limit = None
            if not name or limit is None or limit < 0: raise ApiError(400, "取样点名称或残留限度不合法")
            if name in seen: raise ApiError(400, f"取样点重复：{name}")
            seen.add(name)
            points.append({"name": name, "limit": limit})
        return points

    @staticmethod
    def _points_json(points: list[dict]) -> str:
        return json.dumps(points, ensure_ascii=False)

    @staticmethod
    def _validate_readings(clearance: sqlite3.Row, raw: list) -> dict[str, float]:
        points = {p["name"] for p in json.loads(clearance["sampling_points"])}
        if not isinstance(raw, list) or not raw: raise ApiError(400, "残留读数不能为空")
        values = {}
        for item in raw:
            point = item.get("point") if isinstance(item, dict) else None
            if point not in points: raise ApiError(400, f"未知取样点：{point}")
            try: values[point] = float(item.get("value"))
            except (TypeError, ValueError): raise ApiError(400, f"取样点 {point} 的读数不合法") from None
        missing = points - set(values)
        if missing: raise ApiError(400, "缺少取样点读数：" + "、".join(sorted(missing)))
        return values
