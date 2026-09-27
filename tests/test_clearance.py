import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store
from clearance_service import ClearanceService
from clearance_store import ClearanceStore

POINTS = [{"name": "搅拌桨", "limit": 10}, {"name": "内壁", "limit": 5}]


class ClearanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.cstore = ClearanceStore(self.store.conn); self.cstore.init_schema()
        self.batches = BatchService(self.store)
        self.cs = ClearanceService(self.cstore, self.store.audit)
        self.batches.clearance_gate = self.cs.gate_for_batch
        self.f1 = self.batches.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.eq = self.cs.register_equipment("op1", "operator", self.f1, "EQ-1", "灌装线1")["id"]

    def tearDown(self): self.store.close(); self.tmp.cleanup()

    def open(self, actor="op1", role="operator", points=None, window=24):
        return self.cs.open_clearance(actor, role, self.f1, self.eq, "高活性品种A", "P-001",
                                      points if points is not None else POINTS, window)["clearance"]

    def fill(self, cid, values, actor="op1", role="operator"):
        readings = [{"point": p["name"], "value": v} for p, v in zip(POINTS, values)]
        return self.cs.record_readings(actor, role, self.f1, cid, readings)["clearance"]

    @staticmethod
    def good(): return [3.0, 1.0]
    @staticmethod
    def over(): return [12.0, 1.0]

    def passed_clearance(self):
        c = self.fill(self.open()["id"], self.good())
        return self.cs.review("insp2", "inspector", self.f1, c["id"])["clearance"]

    def test_happy_path_and_batch_gate(self):
        batch = self.batches.create_batch("op1", "operator", self.f1, "B-1", "新品种B", "2026-01-01", "2028-01-01", equipment_id=self.eq)
        with self.assertRaises(ApiError) as no_record:
            self.batches.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertIn("清场", no_record.exception.message)
        c = self.open()
        self.assertEqual("cleaning", c["status"])
        with self.assertRaises(ApiError) as occupied:
            self.open()
        self.assertIn("设备被占用", occupied.exception.message)
        c = self.fill(c["id"], self.good())
        self.assertEqual("pending_review", c["status"])
        with self.assertRaises(ApiError) as blocked_test:
            self.batches.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertIn("清场未通过", blocked_test.exception.message)
        current = self.batches.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked_release:
            self.batches.decide("qa", "qa", batch["id"], "release", "尝试放行", current)
        self.assertIn("清场未通过", blocked_release.exception.message)
        c = self.cs.review("insp2", "inspector", self.f1, c["id"])["clearance"]
        self.assertEqual("passed", c["status"])
        t = self.batches.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertTrue(t["passed"])
        current = self.batches.batch_detail(batch["id"])["batch"]["revision"]
        result = self.batches.decide("qa", "qa", batch["id"], "release", "清场通过，检验合格", current)
        self.assertEqual("released", result["batch"]["state"])

    def test_same_person_review_stays_pending(self):
        c = self.open(actor="insp1", role="inspector")
        c = self.fill(c["id"], self.good(), actor="insp1", role="inspector")
        c = self.cs.review("insp1", "inspector", self.f1, c["id"])["clearance"]
        self.assertEqual("pending_review", c["status"])
        self.assertIn("同一人复核", c["blocked_reason"])
        c = self.cs.review("insp2", "inspector", self.f1, c["id"])["clearance"]
        self.assertEqual("passed", c["status"])

    def test_over_limit_and_resample_invalidates(self):
        c = self.fill(self.open()["id"], self.over())
        c = self.cs.review("insp2", "inspector", self.f1, c["id"])["clearance"]
        self.assertEqual("pending_review", c["status"])
        self.assertIn("超限", c["blocked_reason"])
        new = self.fill(c["id"], self.good(), actor="op2")  # 重新取样
        self.assertEqual(2, new["revision"])
        self.assertEqual(c["id"], new["parent_id"])
        old = self.cs.detail(c["id"])["clearance"]
        self.assertEqual("invalidated", old["status"])
        self.assertIn("重新取样", old["invalidated_reason"])
        listed = self.cs.list_clearances()["clearances"]  # 旧记录仍可查
        self.assertEqual({new["id"], c["id"]}, {row["id"] for row in listed})
        new = self.cs.review("insp2", "inspector", self.f1, new["id"])["clearance"]
        self.assertEqual("passed", new["status"])
        self.assertEqual([c["id"]], [h["id"] for h in self.cs.detail(new["id"])["history"]])

    def test_late_review_stays_pending(self):
        c = self.fill(self.open(window=1)["id"], self.good())
        self.store.conn.execute("UPDATE clearances SET review_valid_until=? WHERE id=?", ("2020-01-01T00:00:00Z", c["id"]))
        self.store.conn.commit()
        c = self.cs.review("insp2", "inspector", self.f1, c["id"])["clearance"]
        self.assertEqual("pending_review", c["status"])
        self.assertIn("超出有效期", c["blocked_reason"])

    def test_amend_invalidates_and_blocks_batch_again(self):
        batch = self.batches.create_batch("op1", "operator", self.f1, "B-2", "新品种B", "2026-01-01", "2028-01-01", equipment_id=self.eq)
        c = self.passed_clearance()
        new = self.cs.amend("op1", "operator", self.f1, c["id"], "高活性品种C", "P-002", POINTS)["clearance"]
        self.assertEqual("cleaning", new["status"])
        old = self.cs.detail(c["id"])["clearance"]
        self.assertEqual("invalidated", old["status"])
        self.assertIn("修改品种或参数", old["invalidated_reason"])
        with self.assertRaises(ApiError) as blocked:
            self.batches.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertIn("清场未通过", blocked.exception.message)

    def test_resample_after_pass_invalidates_conclusion(self):
        c = self.passed_clearance()
        new = self.fill(c["id"], self.good(), actor="op2")
        self.assertEqual("invalidated", self.cs.detail(c["id"])["clearance"]["status"])
        self.assertEqual("pending_review", new["status"])

    def test_validation_and_roles(self):
        with self.assertRaises(ApiError): self.open(points=[])
        with self.assertRaises(ApiError): self.open(actor="lab1", role="lab")
        c = self.open()
        with self.assertRaises(ApiError):  # 缺少取样点读数
            self.cs.record_readings("op1", "operator", self.f1, c["id"], [{"point": "搅拌桨", "value": 1}])
        with self.assertRaises(ApiError):  # 未知取样点
            self.cs.record_readings("op1", "operator", self.f1, c["id"],
                                    [{"point": "搅拌桨", "value": 1}, {"point": "内壁", "value": 1}, {"point": "地漏", "value": 1}])
        c = self.fill(c["id"], self.good())
        with self.assertRaises(ApiError):  # 操作员不能复核
            self.cs.review("op9", "operator", self.f1, c["id"])
        with self.assertRaises(ApiError):  # 未做任何修改
            self.cs.amend("op1", "operator", self.f1, c["id"], "高活性品种A", "P-001", POINTS)

    def test_batch_without_equipment_unaffected(self):
        batch = self.batches.create_batch("op1", "operator", self.f1, "B-3", "普通品种", "2026-01-01", "2028-01-01")
        t = self.batches.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertTrue(t["passed"])


if __name__ == "__main__": unittest.main()
