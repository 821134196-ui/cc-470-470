"""领域规则测试：启动互斥、检查完整、封控阻断、职责分离、事件时序等。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from datetime import timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import connect, init_db
from app.domain import (ControlCabinetAdapter, CrowdAdapter, PASS, FAIL, NA, UP,
                        DOWN, CHECK_ITEMS, Clock, ST_RUNNING, ST_LOCKED, ST_STOPPED)
from app.service import PlatformService, BusinessError
from app.seed import seed


class FakeClock(Clock):
    def __init__(self):
        self.offset = timedelta(0)

    def now(self):
        from datetime import datetime
        return datetime.now() + self.offset

    def advance(self, minutes: float):
        self.offset += timedelta(minutes=minutes)


class PlatformTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.photo_dir = os.path.join(self.tmp.name, "photos")
        self.cabinet = ControlCabinetAdapter()
        self.crowd = CrowdAdapter()
        seed(self.db_path, self.cabinet, self.crowd, verbose=False)
        self.conn = connect(self.db_path)
        init_db(self.conn)
        self.clock = FakeClock()
        self.svc = PlatformService(self.conn, self.cabinet, self.crowd,
                                   clock=self.clock, photo_dir=self.photo_dir)
        # 人员：D01/D02 值班员，I01/I02 巡检，R01 维修，M01 主管，S01 客服
        self.esc = "ESC-101"

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    # ---- 辅助 ----
    def pass_inspection(self, esc="ESC-101", inspector="I01"):
        iid = self.svc.open_or_get_inspection(esc, inspector)
        for key, _l, _c in CHECK_ITEMS:
            self.svc.record_check_item(iid, key, PASS, inspector)
        return iid

    def start_ready(self, esc="ESC-101", duty="D01", inspector="I01"):
        self.pass_inspection(esc, inspector)
        self.svc.submit_emergency_test(esc, inspector, PASS, 300)
        v = self.svc._esc(esc)["config_version"]
        self.svc.start(esc, duty, v)
        return v

    def event_types(self, esc="ESC-101"):
        return [r["type"] for r in reversed(self.svc.events(esc, 500))]


class TestStartMutexAndVersion(PlatformTestBase):
    def test_two_duties_cannot_start_simultaneously(self):
        """两名值班员不能同时启动同一扶梯。"""
        self.pass_inspection()
        v = self.svc._esc(self.esc)["config_version"]
        self.svc.acquire_start(self.esc, "D01", v)
        with self.assertRaises(BusinessError) as cm:
            self.svc.acquire_start(self.esc, "D02", v)
        self.assertEqual(cm.exception.code, "START_BUSY")
        # D01 放弃后 D02 可以启动
        self.svc.release_start(self.esc, "D01")
        self.svc.acquire_start(self.esc, "D02", v)

    def test_stale_version_rejected(self):
        """启动命令必须按当前版本提交。"""
        self.pass_inspection()
        v = self.svc._esc(self.esc)["config_version"]
        # 改一次方向再改回（物理允许的反方向梯在 ESC-102 上演示）：用同方向重设不可，
        # 直接停梯状态下方向受物理约束，故版本递增通过 detail 路径验证：
        # ESC-101 物理上行不能设 DOWN；这里通过 set_direction 到允许值失败时版本不变。
        with self.assertRaises(BusinessError):
            self.svc.set_direction(self.esc, DOWN, "D01")
        # 对本梯：旧版本号必拒
        with self.assertRaises(BusinessError) as cm:
            self.svc.acquire_start(self.esc, "D01", v + 99)
        self.assertEqual(cm.exception.code, "VERSION_STALE")

    def test_version_bumps_then_start_requires_new_version(self):
        """配置版本变更后，持有旧版本的启动命令失效。"""
        # ESC-201 是 2F->3F 上行；制造版本递增：先停梯（本就停），直接验证 set_direction
        # 物理约束只允许 UP，无法改成相反；改为验证版本字段在每次合法 set 后递增。
        before = self.svc._esc("ESC-201")["config_version"]
        self.svc.set_direction("ESC-201", UP, "D01")
        after = self.svc._esc("ESC-201")["config_version"]
        self.assertEqual(after, before + 1)
        self.pass_inspection("ESC-201")
        with self.assertRaises(BusinessError) as cm:
            self.svc.start("ESC-201", "D01", before)
        self.assertEqual(cm.exception.code, "VERSION_STALE")
        self.svc.start("ESC-201", "D01", after)
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_RUNNING)


class TestDirectionErrors(PlatformTestBase):
    def test_wrong_direction_conflicts_with_physics(self):
        """方向设置错误：上行井道不能设下行。"""
        with self.assertRaises(BusinessError) as cm:
            self.svc.set_direction(self.esc, DOWN, "D01")
        self.assertEqual(cm.exception.code, "DIRECTION_CONFLICT")
        with self.assertRaises(BusinessError) as cm:
            self.svc.set_direction("ESC-102", UP, "D01")
        self.assertEqual(cm.exception.code, "DIRECTION_CONFLICT")

    def test_direction_change_while_running_blocked(self):
        self.start_ready()
        with self.assertRaises(BusinessError) as cm:
            self.svc.set_direction(self.esc, UP, "D01")
        self.assertEqual(cm.exception.code, "DIRECTION_WHILE_RUNNING")


class TestInspectionCompleteness(PlatformTestBase):
    def test_start_blocked_without_inspection(self):
        v = self.svc._esc(self.esc)["config_version"]
        with self.assertRaises(BusinessError) as cm:
            self.svc.start(self.esc, "D01", v)
        self.assertEqual(cm.exception.code, "CHECK_INCOMPLETE")
        self.assertIn("尚未做班前检查", cm.exception.message)

    def test_start_blocked_with_missing_item(self):
        """检查漏项独立处理。"""
        iid = self.svc.open_or_get_inspection(self.esc, "I01")
        # 只填 4 项，漏急停
        for key, _l, _c in CHECK_ITEMS[:4]:
            self.svc.record_check_item(iid, key, PASS, "I01")
        v = self.svc._esc(self.esc)["config_version"]
        with self.assertRaises(BusinessError) as cm:
            self.svc.start(self.esc, "D01", v)
        self.assertEqual(cm.exception.code, "CHECK_INCOMPLETE")
        self.assertIn("急停装置", cm.exception.message)

    def test_start_blocked_with_failed_item(self):
        iid = self.svc.open_or_get_inspection(self.esc, "I01")
        for key, _l, _c in CHECK_ITEMS:
            self.svc.record_check_item(iid, key, FAIL if key == "comb" else PASS, "I01",
                                       "梳齿板缺齿" if key == "comb" else "")
        v = self.svc._esc(self.esc)["config_version"]
        with self.assertRaises(BusinessError) as cm:
            self.svc.start(self.esc, "D01", v)
        self.assertIn("梳齿板检查不合格", cm.exception.message)

    def test_critical_item_cannot_be_na(self):
        iid = self.svc.open_or_get_inspection(self.esc, "I01")
        with self.assertRaises(BusinessError) as cm:
            self.svc.record_check_item(iid, "estop", NA, "I01")
        self.assertEqual(cm.exception.code, "NA_FORBIDDEN")

    def test_all_pass_allows_start(self):
        self.start_ready()
        self.assertEqual(self.svc._esc(self.esc)["run_state"], ST_RUNNING)


class TestEstopFailureSeals(PlatformTestBase):
    def test_estop_fail_triggers_lockdown(self):
        """急停测试失败独立处理：立即封控。"""
        self.pass_inspection()
        r = self.svc.submit_emergency_test(self.esc, "I01", FAIL, detail="制停距离超标")
        self.assertTrue(r["sealed"])
        self.assertEqual(self.svc._esc(self.esc)["run_state"], ST_LOCKED)
        self.assertIn("ESTOP_TEST_FAILED", self.event_types())
        self.assertIn("SEALED", self.event_types())
        # 封控阻断启动
        v = self.svc._esc(self.esc)["config_version"]
        with self.assertRaises(BusinessError) as cm:
            self.svc.acquire_start(self.esc, "D01", v)
        self.assertEqual(cm.exception.code, "SEALED")


class TestSealAndBarricade(PlatformTestBase):
    def _seal(self, esc="ESC-201", source="FAULT", reason="驱动链异常"):
        self.pass_inspection(esc)
        self.svc.submit_emergency_test(esc, "I01", PASS, 300)
        v = self.svc._esc(esc)["config_version"]
        self.cabinet.arm_trip(esc)
        r = self.svc.start(esc, "D01", v)
        self.assertTrue(r["tripped"])
        return self.svc.detail(esc)["active_fault"]["id"]

    def test_seal_blocks_start_and_requires_barricade(self):
        fid = self._seal()
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_LOCKED)
        b = self.conn.execute("SELECT * FROM barricades WHERE fault_id=?", (fid,)).fetchone()
        self.assertEqual(b["status"], "PENDING")
        # 围挡缺失时不能完成维修
        self.svc.dispatch(fid, "R01", "M01")
        self.svc.enroute(fid, "R01")
        self.svc.arrive(fid, "R01")
        self.svc.begin_repair(fid, "R01")
        with self.assertRaises(BusinessError) as cm:
            self.svc.complete_repair(fid, "R01")
        self.assertEqual(cm.exception.code, "BARRICADE_REQUIRED")
        # 就位后可以
        self.svc.place_barricade(fid, "D01")
        self.svc.complete_repair(fid, "R01")

    def test_barricade_missing_after_grace_raises_event(self):
        fid = self._seal()
        # 宽限期内无告警
        self.svc.check_barricade_sla()
        self.assertNotIn("BARRICADE_MISSING", self.event_types("ESC-201"))
        self.clock.advance(11)
        missing = self.svc.check_barricade_sla()
        self.assertIn(fid, missing)
        self.assertIn("BARRICADE_MISSING", self.event_types("ESC-201"))
        # 幂等：不再重复告警
        self.svc.check_barricade_sla()
        self.assertEqual(self.event_types("ESC-201").count("BARRICADE_MISSING"), 1)

    def test_repeated_trips_lock_for_overhaul(self):
        """反复跳停：24h 内 3 次封控，工单标记大修锁定。"""
        esc = "ESC-301"
        self.pass_inspection(esc, "I01")
        self.svc.submit_emergency_test(esc, "I01", PASS)
        for i in range(3):
            self.svc.seal(esc, "D01", "FAULT", f"第{i+1}次异常停梯")
        e = self.svc._esc(esc)
        self.assertEqual(e["trip_count"], 3)
        self.assertIn("锁定并安排大修", e["lock_reason"])
        f = self.svc.detail(esc)["active_fault"]
        self.assertEqual(f["source"], "REPEAT")

    def test_trip_window_resets_after_24h(self):
        esc = "ESC-301"
        self.svc.seal(esc, "D01", "FAULT", "跳停")
        self.clock.advance(25 * 60)
        self.svc.seal(esc, "D01", "FAULT", "次日跳停")
        self.assertEqual(self.svc._esc(esc)["trip_count"], 1)


class TestRepairSLA(PlatformTestBase):
    def test_late_arrival_flagged(self):
        """维修延期：超时到场产生事件。"""
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        self.svc.place_barricade(fid, "D01")
        self.svc.dispatch(fid, "R01", "M01")
        self.clock.advance(45)  # 到场时限 30 分钟
        r = self.svc.arrive(fid, "R01")
        self.assertTrue(r["late"])
        self.assertIn("SLA_ARRIVAL_LATE", self.event_types("ESC-201"))

    def test_repair_overdue_event(self):
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        self.svc.place_barricade(fid, "D01")
        self.svc.dispatch(fid, "R01", "M01")
        self.svc.enroute(fid, "R01")
        self.svc.arrive(fid, "R01")
        self.svc.begin_repair(fid, "R01")
        self.clock.advance(121)
        overdue = self.svc.check_repair_sla()
        self.assertIn(fid, overdue)
        self.assertIn("SLA_REPAIR_OVERDUE", self.event_types("ESC-201"))
        # 通知一次
        self.assertEqual(self.svc.check_repair_sla(), [])


class TestReinspectionSeparation(PlatformTestBase):
    def _full_to_repaired(self, esc="ESC-201"):
        # 当日班前检查合格（设备早班已开梯，故障发生在运行期间）
        self.pass_inspection(esc, "I01")
        fid = self.svc.seal(esc, "D01", "FAULT", "故障")
        self.svc.place_barricade(fid, "D01")
        self.svc.dispatch(fid, "R01", "M01")
        self.svc.enroute(fid, "R01")
        self.svc.arrive(fid, "R01")
        self.svc.begin_repair(fid, "R01")
        self.svc.complete_repair(fid, "R01")
        return fid

    def test_inspector_cannot_approve_own_reinspection(self):
        """巡检员不能批准自己的复检（同人直接拦截，即使角色提升）。"""
        fid = self._full_to_repaired()
        rid = self.svc.submit_reinspection(fid, "I01", PASS, "合格")
        with self.assertRaises(BusinessError) as cm:
            self.svc.approve_reinspection(rid, "I01", True)
        self.assertEqual(cm.exception.code, "SELF_APPROVAL")

    def test_inspector_role_cannot_approve(self):
        fid = self._full_to_repaired()
        rid = self.svc.submit_reinspection(fid, "I01", PASS)
        with self.assertRaises(BusinessError) as cm:
            self.svc.approve_reinspection(rid, "I02", True)
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")

    def test_supervisor_approves_different_inspector(self):
        """不同人员复检 + 主管审批通过 -> 可复开。"""
        fid = self._full_to_repaired()
        rid = self.svc.submit_reinspection(fid, "I02", PASS, "复检合格")
        self.svc.approve_reinspection(rid, "M01", True)
        self.assertEqual(self.svc.fault(fid)["status"], "APPROVED")

    def test_rejection_sends_fault_back_to_repair(self):
        fid = self._full_to_repaired()
        rid = self.svc.submit_reinspection(fid, "I01", PASS, "外观正常")
        self.svc.approve_reinspection(rid, "M01", False, reason="梳齿板卡阻")
        self.assertEqual(self.svc.fault(fid)["status"], "REINSPECT_REJECTED")
        self.assertEqual(self.svc.fault(fid)["reject_count"], 1)
        # 退回后重新维修再复检通过
        self.svc.begin_repair(fid, "R01", "拆检")
        self.svc.complete_repair(fid, "R01")
        rid2 = self.svc.submit_reinspection(fid, "I01", PASS, "复检合格")
        self.svc.approve_reinspection(rid2, "M01", True)
        self.assertEqual(self.svc.fault(fid)["status"], "APPROVED")


class TestReopenRules(PlatformTestBase):
    def test_early_reopen_blocked_at_each_stage(self):
        """提前复开独立阻断并记录事件。"""
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        self.svc.place_barricade(fid, "D01")
        with self.assertRaises(BusinessError) as cm:
            self.svc.remove_barricade_and_reopen(fid, "D01", confirmed=True)
        self.assertEqual(cm.exception.code, "EARLY_REOPEN")
        self.assertIn("REOPEN_BLOCKED", self.event_types("ESC-201"))

    def test_reopen_requires_confirm(self):
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        with self.assertRaises(BusinessError) as cm:
            self.svc.remove_barricade_and_reopen(fid, "D01", confirmed=False)
        self.assertEqual(cm.exception.code, "CONFIRM_REQUIRED")

    def test_full_reopen_restores_running(self):
        """完整链路：封控 -> 围挡 -> 维修 -> 复检(不同人) -> 主管批准 -> 撤围挡复开。"""
        self.pass_inspection("ESC-201", "I01")
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        self.svc.place_barricade(fid, "D01")
        self.svc.dispatch(fid, "R01", "M01")
        self.svc.enroute(fid, "R01")
        self.svc.arrive(fid, "R01")
        self.svc.begin_repair(fid, "R01")
        self.svc.complete_repair(fid, "R01")
        rid = self.svc.submit_reinspection(fid, "I02", PASS)
        self.svc.approve_reinspection(rid, "M01", True)
        r = self.svc.remove_barricade_and_reopen(fid, "D01", confirmed=True)
        self.assertTrue(r["reopened"])
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_RUNNING)
        types = self.event_types("ESC-201")
        self.assertLess(types.index("SEALED"), types.index("REPAIR_DONE"))
        self.assertLess(types.index("REPAIR_DONE"), types.index("REINSPECT_APPROVED"))
        self.assertLess(types.index("REINSPECT_APPROVED"), types.index("BARRICADE_REMOVED"))
        self.assertLess(types.index("BARRICADE_REMOVED"), types.index("REOPENED"))


class TestRoleBoundaries(PlatformTestBase):
    def test_only_duty_starts(self):
        self.pass_inspection()
        v = self.svc._esc(self.esc)["config_version"]
        for code in ("I01", "R01", "S01"):
            with self.assertRaises(BusinessError) as cm:
                self.svc.acquire_start(self.esc, code, v)
            self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")

    def test_service_only_detours(self):
        """客服仅能发布绕行信息，且只在封控时。"""
        # 未封控不能发
        with self.assertRaises(BusinessError) as cm:
            self.svc.publish_detour("ESC-201", "S01", "请绕行")
        self.assertEqual(cm.exception.code, "NOT_SEALED")
        fid = self.svc.seal("ESC-201", "D01", "INCIDENT", "客流")
        # 非客服不能发
        with self.assertRaises(BusinessError) as cm:
            self.svc.publish_detour("ESC-201", "D01", "请绕行")
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")
        did = self.svc.publish_detour("ESC-201", "S01", "请改乘直梯")
        self.assertTrue(did)

    def test_inspector_cannot_dispatch(self):
        fid = self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        with self.assertRaises(BusinessError) as cm:
            self.svc.dispatch(fid, "R01", "I01")
        self.assertEqual(cm.exception.code, "FORBIDDEN_ROLE")


class TestEventChronology(PlatformTestBase):
    def test_events_append_only_and_ordered(self):
        """事件时序：事件只追加、按时间排序、封控事件齐全。"""
        self.start_ready()
        self.crowd.arm_incident(self.esc)
        poll = self.svc.poll_running(self.esc)
        self.assertTrue(poll.get("incident"))
        fid = self.svc.detail(self.esc)["active_fault"]["id"]
        types = self.event_types()
        self.assertEqual(types[0], "INSPECTION_OPENED")
        self.assertIn("ESTOP_TEST_PASS", types)
        self.assertIn("STARTED", types)
        self.assertIn("SEALED", types)
        # 每个事件 id 单调
        ids = [r["id"] for r in reversed(self.svc.events(self.esc, 500))]
        self.assertEqual(ids, sorted(ids))
        # 封控后必有对应工单与待就位围挡
        self.assertTrue(fid)
        self.assertEqual(self.svc._esc(self.esc)["run_state"], ST_LOCKED)

    def test_trip_from_running_poll_seals(self):
        self.start_ready("ESC-301")
        self.cabinet.inject_running_fault("ESC-301", "E-44 超速")
        poll = self.svc.poll_running("ESC-301")
        self.assertTrue(poll.get("tripped"))
        self.assertEqual(self.svc._esc("ESC-301")["run_state"], ST_LOCKED)


class TestSchedulerPlannedActions(PlatformTestBase):
    def _scheduler_at(self, hour_min: str):
        from app.scheduler import Scheduler
        from datetime import datetime
        sched = Scheduler(self.svc, clock=self.clock, interval=999)
        at = datetime.now().replace(hour=int(hour_min[:2]), minute=int(hour_min[3:]),
                                    second=0, microsecond=0)
        return sched, at

    def test_planned_start_requires_passed_inspection(self):
        """计划开梯：检查未完成则阻断并记录事件。"""
        sched, at = self._scheduler_at("09:30")  # ESC-201 计划 09:30
        sched.tick(at)
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_STOPPED)
        self.assertIn("SCHEDULE_BLOCKED", self.event_types("ESC-201"))

    def test_planned_start_then_stop(self):
        self.pass_inspection("ESC-201")
        sched, at = self._scheduler_at("09:30")
        sched.tick(at)
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_RUNNING)
        self.assertIn("SCHEDULED_START", self.event_types("ESC-201"))
        # 同一分钟内不重复触发
        sched.tick(at)
        self.assertEqual(self.event_types("ESC-201").count("SCHEDULED_START"), 1)
        # 计划关梯 21:30
        sched.tick(at.replace(hour=21, minute=30))
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_STOPPED)
        self.assertIn("SCHEDULED_STOP", self.event_types("ESC-201"))

    def test_planned_start_blocked_when_locked(self):
        self.svc.seal("ESC-201", "D01", "FAULT", "故障")
        sched, at = self._scheduler_at("09:30")
        sched.tick(at)
        self.assertEqual(self.svc._esc("ESC-201")["run_state"], ST_LOCKED)
        self.assertIn("SCHEDULE_BLOCKED", self.event_types("ESC-201"))


if __name__ == "__main__":
    unittest.main()
