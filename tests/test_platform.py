"""领域层测试：启动互斥、检查完整、封控阻断、职责分离、事件时序等。"""

import sys
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from escalator_platform.demo_data import build_service
from escalator_platform import errors as E


class FakeClock:
    def __init__(self, t=None):
        self.t = t or datetime(2026, 9, 30, 7, 30, 0)

    def __call__(self):
        return self.t

    def advance(self, **kw):
        self.t += timedelta(**kw)


def make_service():
    tmp = tempfile.mkdtemp()
    clock = FakeClock()
    svc = build_service(f"{tmp}/p.db", f"{tmp}/photos", reset=True, clock=clock)
    return svc, clock


ALL_PASS = {"steps": "pass", "comb": "pass", "handrail": "pass",
            "lighting": "pass", "estop": "pass"}


class BaseMixin:
    def pass_precheck_start(self, svc, eid="E12-A", staff="D01"):
        svc.submit_precheck("I01", eid, ALL_PASS)
        svc.start(staff, eid, "up", 1)


class TestPrecheck(unittest.TestCase, BaseMixin):
    def setUp(self):
        self.svc, self.clock = make_service()

    def test_all_pass_allows_start(self):
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)
        self.svc.start("D01", "E12-A", "up", 1)
        self.assertEqual(self.svc.escalator_detail("E12-A")["status"], "running")

    def test_missing_item_is_incomplete(self):
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.submit_precheck("I01", "E12-A",
                                     {"steps": "pass", "comb": "pass"})

    def test_na_item_blocks_start(self):
        # 不适用也不允许放行：每日开梯必须逐项合格
        items = dict(ALL_PASS, lighting="na")
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.submit_precheck("I01", "E12-A", items)
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.start("D01", "E12-A", "up", 1)

    def test_failed_item_blocks_start(self):
        items = dict(ALL_PASS, comb="fail")
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.submit_precheck("I01", "E12-A", items)
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.start("D01", "E12-A", "up", 1)

    def test_estop_failure_is_separate_error(self):
        items = dict(ALL_PASS, estop="fail")
        with self.assertRaises(E.EstopTestFailed):
            self.svc.submit_precheck("I01", "E12-A", items)
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.start("D01", "E12-A", "up", 1)

    def test_resubmit_all_pass_next_day_allowed(self):
        with self.assertRaises(E.InspectionIncomplete):
            self.svc.submit_precheck("I01", "E12-A", dict(ALL_PASS, steps="fail"))
        # 当日补检全部合格
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)
        self.svc.start("D01", "E12-A", "up", 1)

    def test_only_inspector_submits_precheck(self):
        with self.assertRaises(E.DutySeparation):
            self.svc.submit_precheck("D01", "E12-A", ALL_PASS)

    def test_cs_cannot_start(self):
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)
        with self.assertRaises(E.DutySeparation):
            self.svc.start("C01", "E12-A", "up", 1)


class TestStartVersionAndDirection(unittest.TestCase, BaseMixin):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)

    def test_wrong_direction_rejected(self):
        with self.assertRaises(E.DirectionMismatch):
            self.svc.start("D01", "E12-A", "down", 1)

    def test_stale_version_rejected(self):
        self.svc.set_direction("D02", "E12-A", "down")
        with self.assertRaises(E.DirectionMismatch):
            self.svc.start("D01", "E12-A", "down", 1)   # 版本已升到 2
        self.svc.start("D01", "E12-A", "down", 2)

    def test_direction_change_blocked_while_running_or_locked(self):
        self.svc.start("D01", "E12-A", "up", 1)
        with self.assertRaises(E.InvalidTransition):
            self.svc.set_direction("D02", "E12-A", "down")


class TestStartMutex(unittest.TestCase, BaseMixin):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)

    def test_two_duty_cannot_prepare_concurrently(self):
        self.svc.prepare_start("D01", "E12-A")
        with self.assertRaises(E.ConflictError):
            self.svc.prepare_start("D02", "E12-A")

    def test_second_start_rejected_while_lock_held(self):
        self.svc.prepare_start("D01", "E12-A")
        with self.assertRaises(E.ConflictError):
            self.svc.start("D02", "E12-A", "up", 1)

    def test_lock_released_after_cancel(self):
        self.svc.prepare_start("D01", "E12-A")
        self.svc.release_start_lock("D01", "E12-A")
        self.svc.prepare_start("D02", "E12-A")  # 不抛异常

    def test_lock_expires_after_ttl(self):
        self.svc.prepare_start("D01", "E12-A")
        self.clock.advance(seconds=31)
        self.svc.prepare_start("D02", "E12-A")

    def test_concurrent_threads_only_one_starts(self):
        outcomes = []

        def attempt(staff):
            try:
                self.svc.prepare_start(staff, "E12-A")
                self.svc.start(staff, "E12-A", "up", 1)
                outcomes.append(staff)
            except E.PlatformError:
                pass

        threads = [threading.Thread(target=attempt, args=(s,))
                   for s in ("D01", "D02")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(self.svc.escalator_detail("E12-A")["status"], "running")


class TestLockdownBarrierRepairReopen(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()

    def _full_fault_to_fixed(self, eid="E34-A"):
        self.svc.lock_down(eid, "fault", "测试故障", "D01")
        self.svc.set_barrier("D01", eid, True)
        self.svc.report_repair("I01", eid, "故障")
        with self.assertRaises(E.BarrierMissing):
            # 另起一台无围挡设备验证：围挡缺失维修不得进场
            self._fresh_lock_no_barrier()
        self.svc.arrive("M01", eid)
        self.svc.mark_fixed("M01", eid, "已修复")

    def _fresh_lock_no_barrier(self, eid="E23-A"):
        self.svc.lock_down(eid, "fault", "测试故障", "D01")
        self.svc.report_repair("I01", eid, "故障")
        self.svc.arrive("M01", eid)

    def test_locked_blocks_start_stop_direction(self):
        self.svc.submit_precheck("I01", "E34-A", ALL_PASS)
        self.svc.lock_down("E34-A", "fault", "故障", "D01")
        with self.assertRaises(E.InvalidTransition):
            self.svc.start("D01", "E34-A", "up", 1)
        with self.assertRaises(E.InvalidTransition):
            self.svc.stop("D01", "E34-A")
        with self.assertRaises(E.InvalidTransition):
            self.svc.set_direction("D01", "E34-A", "down")

    def test_early_reopen_blocked_at_each_stage(self):
        self.svc.lock_down("E34-A", "fault", "故障", "D01")
        # 未设围挡
        with self.assertRaises(E.BarrierMissing):
            self.svc.reopen("D01", "E34-A")
        self.svc.set_barrier("D01", "E34-A", True)
        # 围挡未撤 + 无报修
        with self.assertRaises(E.EarlyReopen):
            self.svc.reopen("D01", "E34-A")
        self.svc.report_repair("I01", "E34-A", "故障")
        with self.assertRaises(E.EarlyReopen):
            self.svc.set_barrier("D01", "E34-A", False)  # 维修未完成不能撤围挡
        self.svc.arrive("M01", "E34-A")
        with self.assertRaises(E.EarlyReopen):
            self.svc.reopen("D01", "E34-A")           # 未修复
        self.svc.mark_fixed("M01", "E34-A", "修复")
        with self.assertRaises(E.EarlyReopen):
            self.svc.set_barrier("D01", "E34-A", False)  # 复检未批不能撤
        rid = self.svc.submit_recheck("I01", "E34-A")
        with self.assertRaises(E.EarlyReopen):
            self.svc.reopen("D01", "E34-A")           # 复检待批
        self.svc.decide_recheck("I02", rid, True)
        # 复检批准后方可撤围挡并复开
        self.svc.set_barrier("D01", "E34-A", False)
        self.svc.reopen("D01", "E34-A")
        d = self.svc.escalator_detail("E34-A")
        self.assertEqual(d["status"], "stopped")
        self.assertIsNone(d["active_lockdown"])

    def test_recheck_rejection_requires_rework(self):
        self._full_fault_to_fixed()
        rid = self.svc.submit_recheck("I01", "E34-A", "复检")
        self.svc.decide_recheck("I02", rid, False, "退回")
        # 退回后仍不能撤围挡/复开
        with self.assertRaises(E.EarlyReopen):
            self.svc.set_barrier("D01", "E34-A", False)
        self.svc.rework("M01", "E34-A", "异响仍在")
        # 返工后 fixed_at 被清空，需再次修复→复检→批准
        with self.assertRaises(E.InvalidTransition):
            self.svc.submit_recheck("I01", "E34-A")
        self.svc.mark_fixed("M01", "E34-A", "再次修复")
        rid2 = self.svc.submit_recheck("I02", "E34-A")
        self.svc.decide_recheck("I01", rid2, True)
        self.svc.set_barrier("D01", "E34-A", False)
        self.svc.reopen("D01", "E34-A")


class TestDutySeparation(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.lock_down("E34-A", "fault", "故障", "D01")
        self.svc.set_barrier("D01", "E34-A", True)
        self.svc.report_repair("I01", "E34-A", "故障")
        self.svc.arrive("M01", "E34-A")
        self.svc.mark_fixed("M01", "E34-A", "修复")

    def test_inspector_cannot_approve_own_recheck(self):
        rid = self.svc.submit_recheck("I01", "E34-A")
        with self.assertRaises(E.DutySeparation):
            self.svc.decide_recheck("I01", rid, True)
        # 仍为待审批，可由另一名巡检员批准
        self.svc.decide_recheck("I02", rid, True)

    def test_cs_only_publishes_detour(self):
        with self.assertRaises(E.DutySeparation):
            self.svc.report_repair("C01", "E34-A", "x")
        with self.assertRaises(E.DutySeparation):
            self.svc.set_barrier("C01", "E34-A", True)
        did = self.svc.publish_detour("C01", "E34-A", "请绕行直梯")
        self.assertTrue(did)
        # 其他角色不能发绕行
        with self.assertRaises(E.DutySeparation):
            self.svc.publish_detour("D01", "E34-A", "x")

    def test_service_cannot_reopen(self):
        rid = self.svc.submit_recheck("I01", "E34-A")
        self.svc.decide_recheck("I02", rid, True)
        self.svc.set_barrier("D01", "E34-A", False)
        with self.assertRaises(E.DutySeparation):
            self.svc.reopen("M01", "E34-A")


class TestTripsAndCrowd(unittest.TestCase):
    def setUp(self):
        self.svc, self.clock = make_service()
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)
        self.svc.start("D01", "E12-A", "up", 1)

    def test_repeated_trips_auto_lockdown_order(self):
        self.assertEqual(self.svc.handle_trip("E12-A"), "stopped")
        self.svc.start("D01", "E12-A", "up", 1)
        self.assertEqual(self.svc.handle_trip("E12-A"), "stopped")
        self.svc.start("D01", "E12-A", "up", 1)
        self.assertEqual(self.svc.handle_trip("E12-A"), "locked")
        d = self.svc.escalator_detail("E12-A")
        self.assertEqual(d["status"], "locked")
        self.assertIsNotNone(d["active_order"])   # 自动报修
        self.assertEqual(d["active_lockdown"]["reason"], "fault")

    def test_trip_window_resets(self):
        self.svc.handle_trip("E12-A"); self.svc.start("D01", "E12-A", "up", 1)
        self.svc.handle_trip("E12-A")
        self.clock.advance(hours=25)
        # 新的一天，重新班前检查后窗口计数重置
        self.svc.submit_precheck("I01", "E12-A", ALL_PASS)
        self.svc.start("D01", "E12-A", "up", 1)
        self.assertEqual(self.svc.handle_trip("E12-A"), "stopped")  # 窗口重置

    def test_crowd_lockdown_flow(self):
        self.svc.lock_down("E12-A", "crowd", "客流临界")
        self.svc.set_barrier("D01", "E12-A", True)
        # 客流仍 critical：不能撤围挡/复开
        self.svc.crowd.inject("E12-A", "critical",
                              self.clock() + timedelta(minutes=10))
        with self.assertRaises(E.EarlyReopen):
            self.svc.set_barrier("D01", "E12-A", False)
        # 客流回落后放行
        self.svc.crowd.inject("E12-A", "normal",
                              self.clock() + timedelta(hours=1))
        self.svc.set_barrier("D01", "E12-A", False)
        self.svc.reopen("D01", "E12-A")


class TestRepairOverdue(unittest.TestCase):
    def test_overdue_generates_alert(self):
        svc, clock = make_service()
        svc.lock_down("E34-A", "fault", "故障", "D01")
        svc.set_barrier("D01", "E34-A", True)
        oid = svc.report_repair("I01", "E34-A", "故障")
        svc.arrive("M01", "E34-A")
        fired = svc.check_repair_overdue()
        self.assertEqual(fired, [])
        clock.advance(minutes=61)
        fired = svc.check_repair_overdue()
        self.assertEqual(fired, [oid])
        evs = svc.events("E34-A", "REPAIR_OVERDUE")
        self.assertEqual(len(evs), 1)
        # 节流：立刻再检查不重复告警
        self.assertEqual(svc.check_repair_overdue(), [])


class TestEventTimeline(unittest.TestCase):
    def test_full_flow_event_order(self):
        svc, clock = make_service()
        svc.submit_precheck("I01", "E34-A", ALL_PASS)
        svc.start("D01", "E34-A", "up", 1)
        svc.handle_trip("E34-A")
        svc.lock_down("E34-A", "fault", "故障", "D01")
        svc.set_barrier("D01", "E34-A", True)
        svc.report_repair("I01", "E34-A", "故障")
        svc.arrive("M01", "E34-A")
        svc.mark_fixed("M01", "E34-A", "修复")
        rid = svc.submit_recheck("I01", "E34-A")
        svc.decide_recheck("I02", rid, True)
        svc.set_barrier("I02", "E34-A", False)
        svc.reopen("D01", "E34-A")

        types = [e["type"] for e in reversed(svc.events("E34-A", limit=50))]
        expected = ["PRECHECK_PASS", "START", "TRIP", "LOCKDOWN",
                    "BARRIER_SET", "REPAIR_REPORT", "REPAIR_ARRIVE",
                    "REPAIR_FIXED", "RECHECK_SUBMIT", "RECHECK_APPROVED",
                    "BARRIER_REMOVE", "REOPEN"]
        self.assertEqual(types, expected)

    def test_events_filter_and_payloads(self):
        svc, clock = make_service()
        svc.submit_precheck("I01", "E12-A", ALL_PASS)
        svc.start("D01", "E12-A", "up", 1)
        only = svc.events("E12-A", "START")
        self.assertEqual({e["type"] for e in only}, {"START"})
        self.assertEqual(only[0]["payload"]["version"], 1)
        self.assertEqual(only[0]["actor_name"], "王磊(值班员)")


class TestSchedulerTick(unittest.TestCase):
    def test_scheduled_start_skips_without_precheck(self):
        from escalator_platform.scheduler import Scheduler
        svc, clock = make_service()
        sched = Scheduler(svc, interval_sec=999)
        # 08:00 无检查 -> 跳过
        sched.tick(datetime(2026, 9, 30, 8, 0))
        self.assertEqual(svc.escalator_detail("E12-A")["status"], "stopped")
        types = [e["type"] for e in svc.events("E12-A")]
        self.assertIn("SCHEDULED_START_SKIPPED", types)

    def test_scheduled_start_and_stop(self):
        from escalator_platform.scheduler import Scheduler
        svc, clock = make_service()
        sched = Scheduler(svc, interval_sec=999)
        svc.submit_precheck("I01", "E12-A", ALL_PASS,
                            check_date="2026-09-30")
        sched.tick(datetime(2026, 9, 30, 8, 0))
        self.assertEqual(svc.escalator_detail("E12-A")["status"], "running")
        sched.tick(datetime(2026, 9, 30, 22, 0))
        self.assertEqual(svc.escalator_detail("E12-A")["status"], "stopped")


if __name__ == "__main__":
    unittest.main(verbosity=2)
