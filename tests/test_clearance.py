import sys, tempfile, unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, BatchService, Store
from clearance_store import ClearanceStore
from clearance_service import ClearanceService


class FakeClock:
    def __init__(self):
        self.t = datetime(2026, 9, 27, 8, 0, tzinfo=timezone.utc)

    def __call__(self):
        return self.t.isoformat(timespec="seconds").replace("+00:00", "Z")

    def advance(self, **kw):
        self.t += timedelta(**kw)


class ClearanceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "c.db")
        self.clock = FakeClock()
        self.cs = ClearanceService(ClearanceStore(self.store.conn), self.store.audit, clock=self.clock)
        self.svc = BatchService(self.store, self.cs)
        self.f1 = self.svc.register_factory("qa", "qa", "F1", "一厂", "CN")["id"]
        self.f2 = self.svc.register_factory("qa", "qa", "F2", "二厂", "CN")["id"]
        self.eq = self.cs.register_equipment("qa", "qa", self.f1, "EQ-1", "反应釜")["id"]
        self.points = [{"point": "搅拌桨", "limit": 10.0}, {"point": "出料口", "limit": 5.0}]

    def tearDown(self):
        self.store.close(); self.tmp.cleanup()

    def iso(self, hours=8):
        return (self.clock.t + timedelta(hours=hours)).isoformat(timespec="seconds").replace("+00:00", "Z")

    def register(self, **kw):
        args = dict(actor="op-1", role="operator", factory_id=self.f1, equipment_id=self.eq,
                    prev_product="高活性品种A", prev_batch_no="A-001", points=self.points, valid_until=self.iso())
        args.update(kw)
        return self.cs.register_run(**args)["run"]

    def fill(self, run_id, values, actor="op-1"):
        readings = [{"point": p, "value": v} for p, v in values.items()]
        return self.cs.fill_readings(actor, "operator", run_id, readings)["run"]

    def test_occupied_equipment_blocks_new_run_and_batch(self):
        self.register()
        with self.assertRaises(ApiError) as dup:
            self.register()
        self.assertEqual(409, dup.exception.status)
        self.assertIn("不得开工", dup.exception.message)
        with self.assertRaises(ApiError) as batch_blocked:
            self.svc.create_batch("op-1", "operator", self.f1, "B-1", "品种B", "2026-01-01", "2028-01-01", equipment_id=self.eq)
        self.assertIn("不得开工", batch_blocked.exception.message)

    def test_gate_blocks_tests_and_release_until_clearance_passed(self):
        batch = self.svc.create_batch("op-1", "operator", self.f1, "B-1", "品种B", "2026-01-01", "2028-01-01", equipment_id=self.eq)
        with self.assertRaises(ApiError) as no_test:
            self.svc.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])
        self.assertIn("清场通过前不能补录检验", no_test.exception.message)
        with self.assertRaises(ApiError) as no_release:
            self.svc.decide("qa", "qa", batch["id"], "release", "尝试放行", batch["revision"])
        self.assertIn("清场通过前不能申请放行", no_release.exception.message)
        run = self.register()
        self.fill(run["id"], {"搅拌桨": 3.0, "出料口": 1.0})
        passed = self.cs.review("insp-2", "inspector", run["id"])["run"]
        self.assertEqual("passed", passed["status"])
        current = self.svc.batch_detail(batch["id"])["batch"]["revision"]
        self.assertTrue(self.svc.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, current)["passed"])
        current = self.svc.batch_detail(batch["id"])["batch"]["revision"]
        result = self.svc.decide("qa", "qa", batch["id"], "release", "清场通过，检验合格", current)
        self.assertEqual("released", result["batch"]["state"])

    def test_same_person_review_held_with_reason(self):
        run = self.register()
        self.fill(run["id"], {"搅拌桨": 3.0, "出料口": 1.0})
        held = self.cs.review("op-1", "inspector", run["id"])
        self.assertEqual("pending_review", held["run"]["status"])
        self.assertIn("同人复核", held["run"]["blocked_reason"])
        self.assertEqual("held", held["reviews"][0]["outcome"])
        passed = self.cs.review("insp-2", "inspector", run["id"])["run"]
        self.assertEqual("passed", passed["status"])
        self.assertIsNone(passed["blocked_reason"])

    def test_over_limit_review_held_with_reason(self):
        run = self.register()
        self.fill(run["id"], {"搅拌桨": 12.0, "出料口": 1.0})
        held = self.cs.review("insp-2", "inspector", run["id"])
        self.assertEqual("pending_review", held["run"]["status"])
        self.assertIn("残留超限", held["run"]["blocked_reason"])
        self.assertIn("搅拌桨", held["run"]["blocked_reason"])

    def test_late_review_held_and_resample_recovers(self):
        run = self.register()
        self.fill(run["id"], {"搅拌桨": 3.0, "出料口": 1.0})
        self.clock.advance(hours=9)
        held = self.cs.review("insp-2", "inspector", run["id"])
        self.assertEqual("pending_review", held["run"]["status"])
        self.assertIn("有效期", held["run"]["blocked_reason"])
        new_run = self.cs.resample("op-1", "operator", run["id"], valid_until=self.iso())["run"]
        self.assertEqual(2, new_run["version"])
        self.assertEqual("cleaning", new_run["status"])
        old = self.cs.run_detail(run["id"])
        self.assertEqual("superseded", old["run"]["status"])
        self.assertEqual(2, len(old["readings"]))
        self.fill(new_run["id"], {"搅拌桨": 2.0, "出料口": 1.0})
        self.assertEqual("passed", self.cs.review("insp-2", "inspector", new_run["id"])["run"]["status"])

    def test_revise_invalidates_passed_conclusion_and_history_kept(self):
        run = self.register()
        self.fill(run["id"], {"搅拌桨": 3.0, "出料口": 1.0})
        self.cs.review("insp-2", "inspector", run["id"])
        batch = self.svc.create_batch("op-1", "operator", self.f1, "B-1", "品种B", "2026-01-01", "2028-01-01", equipment_id=self.eq)
        self.assertTrue(self.svc.record_test("lab", "lab", self.f1, batch["id"], "含量", 99, 95, 105, batch["revision"])["passed"])
        new_points = self.points + [{"point": "釜壁", "limit": 8.0}]
        new_run = self.cs.revise("op-1", "operator", run["id"], points=new_points, valid_until=self.iso())["run"]
        self.assertEqual(2, new_run["version"])
        self.assertEqual("superseded", self.cs.run_detail(run["id"])["run"]["status"])
        current = self.svc.batch_detail(batch["id"])["batch"]["revision"]
        with self.assertRaises(ApiError) as blocked:
            self.svc.record_test("lab", "lab", self.f1, batch["id"], "水分", 1, 0, 2, current)
        self.assertIn("清场通过前不能补录检验", blocked.exception.message)
        history = self.cs.equipment_history(self.eq)
        self.assertEqual([2, 1], [r["run"]["version"] for r in history["runs"]])
        self.assertEqual(2, len(history["runs"][1]["readings"]))

    def test_role_and_payload_validation(self):
        with self.assertRaises(ApiError) as no_role:
            self.cs.review("op-1", "operator", 1)
        self.assertEqual(403, no_role.exception.status)
        with self.assertRaises(ApiError): self.register(points=[])
        with self.assertRaises(ApiError): self.register(valid_until=self.iso(hours=-1))
        with self.assertRaises(ApiError) as cross_factory:
            self.register(factory_id=self.f2)
        self.assertEqual(403, cross_factory.exception.status)
        run = self.register()
        with self.assertRaises(ApiError) as missing:
            self.fill(run["id"], {"搅拌桨": 3.0})
        self.assertIn("缺少取样点", missing.exception.message)
        with self.assertRaises(ApiError) as unknown:
            self.fill(run["id"], {"搅拌桨": 3.0, "出料口": 1.0, "天花板": 1.0})
        self.assertIn("未登记", unknown.exception.message)
        with self.assertRaises(ApiError) as not_ready:
            self.cs.review("insp-2", "inspector", run["id"])
        self.assertIn("尚未填写", not_ready.exception.message)


if __name__ == "__main__":
    unittest.main()
