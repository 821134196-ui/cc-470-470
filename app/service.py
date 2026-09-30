"""核心领域服务：启停互斥、检查完整性、封控阻断、职责分离、复开规则。

所有写操作在同一把进程锁 + SQLite 事务内完成，事件只追加。
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime

from . import domain as D
from .domain import (
    ARRIVAL_SLA_MIN, BARRICADE_GRACE_MIN, CHECK_ITEMS, DOWN, REPAIR_SLA_MIN,
    TRIP_LIMIT, UP, FAIL, NA, PASS, Clock, iso, parse_iso, today, due_after,
    ROLE_DUTY, ROLE_INSPECTOR, ROLE_REPAIR, ROLE_SERVICE, ROLE_SUPERVISOR,
    F_REPORTED, F_ASSIGNED, F_ENROUTE, F_ARRIVED, F_REPAIRING, F_REPAIRED,
    F_REJECTED, F_APPROVED, F_CLOSED,
    ST_STOPPED, ST_STARTING, ST_RUNNING, ST_LOCKED,
    SRC_FAULT, SRC_INCIDENT, SRC_ESTOP, SRC_REPEAT,
)


class BusinessError(Exception):
    """业务规则拒绝，携带机器码与中文说明。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class PlatformService:
    def __init__(self, conn: sqlite3.Connection, cabinet: D.ControlCabinetAdapter,
                 crowd: D.CrowdAdapter, clock: Clock | None = None,
                 photo_dir: str | None = None):
        self.conn = conn
        self.cabinet = cabinet
        self.crowd = crowd
        self.clock = clock or Clock()
        self.photo_dir = photo_dir
        self._lock = threading.RLock()

    # ---------- 基础查询 ----------
    def _person(self, person_code: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM people WHERE code=?", (person_code,)).fetchone()
        if not row:
            raise BusinessError("NO_PERSON", f"人员不存在：{person_code}")
        return row

    def _require_role(self, person_code: str, *roles: str) -> sqlite3.Row:
        p = self._person(person_code)
        if p["role"] not in roles:
            allowed = "、".join(D.ROLE_NAMES[r] for r in roles)
            raise BusinessError("FORBIDDEN_ROLE",
                                f"{p['name']}（{D.ROLE_NAMES[p['role']]}）无权执行，仅 {allowed} 可操作")
        return p

    def _esc(self, esc_code: str) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM escalators WHERE code=?", (esc_code,)).fetchone()
        if not row:
            raise BusinessError("NO_ESCALATOR", f"扶梯不存在：{esc_code}")
        return row

    def _event(self, esc_id: int, etype: str, actor_id, version, detail: str, now: datetime):
        self.conn.execute(
            "INSERT INTO run_events(escalator_id,type,actor_id,from_version,detail,created_at)"
            " VALUES(?,?,?,?,?,?)",
            (esc_id, etype, actor_id, version, detail, iso(now)))

    def list_escalators(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM escalators ORDER BY floor_from DESC, code"))

    def list_people(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM people ORDER BY role, id"))

    def events(self, esc_code: str | None = None, limit: int = 200) -> list[sqlite3.Row]:
        if esc_code:
            return list(self.conn.execute(
                "SELECT ev.*, e.code esc_code, p.name actor_name FROM run_events ev"
                " JOIN escalators e ON e.id=ev.escalator_id"
                " LEFT JOIN people p ON p.id=ev.actor_id"
                " WHERE e.code=? ORDER BY ev.id DESC LIMIT ?", (esc_code, limit)))
        return list(self.conn.execute(
            "SELECT ev.*, e.code esc_code, p.name actor_name FROM run_events ev"
            " JOIN escalators e ON e.id=ev.escalator_id"
            " LEFT JOIN people p ON p.id=ev.actor_id"
            " ORDER BY ev.id DESC LIMIT ?", (limit,)))

    # ---------- 方向配置 ----------
    def set_direction(self, esc_code: str, direction: str, person_code: str) -> int:
        """设置运行方向。方向错误独立处理：仅允许 UP/DOWN，且不得与楼层物理关系矛盾。"""
        with self._lock:
            p = self._require_role(person_code, ROLE_DUTY, ROLE_SUPERVISOR)
            if direction not in (UP, DOWN):
                raise BusinessError("BAD_DIRECTION", f"方向非法：{direction}")
            e = self._esc(esc_code)
            if e["run_state"] == ST_RUNNING or e["run_state"] == ST_STARTING:
                raise BusinessError("DIRECTION_WHILE_RUNNING", "运行中不可改方向，须先停梯")
            # 楼层物理校验：floor_from -> floor_to，上行必须 to>from
            physically_up = e["floor_to"] > e["floor_from"]
            if (direction == UP) != physically_up:
                raise BusinessError(
                    "DIRECTION_CONFLICT",
                    f"方向设置错误：{e['name']}物理方向为 {e['floor_from']}F→{e['floor_to']}F，"
                    f"不能设置为{'上行' if direction == UP else '下行'}")
            new_version = e["config_version"] + 1
            now = self.clock.now()
            self.conn.execute(
                "UPDATE escalators SET desired_direction=?, config_version=?, updated_at=? WHERE id=?",
                (direction, new_version, iso(now), e["id"]))
            self._event(e["id"], "DIRECTION_SET", p["id"], e["config_version"],
                        f"方向设为{'上行' if direction == UP else '下行'}（v{new_version}）", now)
            self.conn.commit()
            return new_version

    # ---------- 班前检查 ----------
    def open_or_get_inspection(self, esc_code: str, person_code: str) -> int:
        with self._lock:
            p = self._require_role(person_code, ROLE_INSPECTOR)
            e = self._esc(esc_code)
            date = today(self.clock.now())
            row = self.conn.execute(
                "SELECT * FROM inspections WHERE escalator_id=? AND date=?", (e["id"], date)).fetchone()
            if row:
                return row["id"]
            now = self.clock.now()
            cur = self.conn.execute(
                "INSERT INTO inspections(escalator_id,date,inspector_id,created_at) VALUES(?,?,?,?)",
                (e["id"], date, p["id"], iso(now)))
            ins_id = cur.lastrowid
            for key, _label, critical in CHECK_ITEMS:
                self.conn.execute(
                    "INSERT INTO inspection_items(inspection_id,item_key,critical) VALUES(?,?,?)",
                    (ins_id, key, critical))
            self._event(e["id"], "INSPECTION_OPENED", p["id"], e["config_version"],
                        f"{date} 班前检查表已创建", now)
            self.conn.commit()
            return ins_id

    def record_check_item(self, inspection_id: int, item_key: str, result: str,
                          person_code: str, remark: str = "") -> None:
        with self._lock:
            p = self._require_role(person_code, ROLE_INSPECTOR)
            if result not in (PASS, FAIL, NA):
                raise BusinessError("BAD_RESULT", "检查结果只能是 通过/失败/不适用")
            ins = self.conn.execute("SELECT * FROM inspections WHERE id=?", (inspection_id,)).fetchone()
            if not ins:
                raise BusinessError("NO_INSPECTION", "检查表不存在")
            if ins["status"] != "OPEN":
                raise BusinessError("INSPECTION_CLOSED", "检查表已关闭，不可修改")
            item = self.conn.execute(
                "SELECT * FROM inspection_items WHERE inspection_id=? AND item_key=?",
                (inspection_id, item_key)).fetchone()
            if not item:
                raise BusinessError("BAD_ITEM", f"检查项不存在：{item_key}")
            if item["critical"] and result == NA:
                raise BusinessError("NA_FORBIDDEN",
                                    f"【{D.CHECK_ITEM_LABELS[item_key]}】是必检项，不能选不适用")
            now = self.clock.now()
            self.conn.execute(
                "UPDATE inspection_items SET result=?, remark=?, updated_at=? WHERE id=?",
                (result, remark, iso(now), item["id"]))
            self.conn.commit()

    def get_inspection(self, esc_code: str, date: str | None = None) -> sqlite3.Row | None:
        e = self._esc(esc_code)
        date = date or today(self.clock.now())
        return self.conn.execute(
            "SELECT * FROM inspections WHERE escalator_id=? AND date=?", (e["id"], date)).fetchone()

    def inspection_items(self, inspection_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT * FROM inspection_items WHERE inspection_id=? ORDER BY id", (inspection_id,)))

    def _check_completeness(self, e: sqlite3.Row) -> tuple[bool, list[str]]:
        """返回(是否全部合格, 问题描述列表)。处理检查漏项。"""
        date = today(self.clock.now())
        ins = self.conn.execute(
            "SELECT * FROM inspections WHERE escalator_id=? AND date=?", (e["id"], date)).fetchone()
        problems: list[str] = []
        if not ins:
            return False, ["今日尚未做班前检查"]
        items = self.inspection_items(ins["id"])
        by_key = {r["item_key"]: r for r in items}
        for key, label, _critical in CHECK_ITEMS:
            r = by_key.get(key)
            if r is None or r["result"] is None:
                problems.append(f"检查漏项：{label}未填写")
            elif r["result"] == FAIL:
                problems.append(f"{label}检查不合格")
            # 必检项不允许 NA（录入时已拦截），历史脏数据再兜底
            elif r["critical"] and r["result"] == NA:
                problems.append(f"{label}为必检项，不能不适用")
        return (len(problems) == 0), problems

    def submit_emergency_test(self, esc_code: str, person_code: str,
                              result: str, response_ms: int | None = None,
                              detail: str = "") -> dict:
        """急停测试。失败 -> 独立处理：立即停梯封控（E-STOP 源）。"""
        with self._lock:
            p = self._require_role(person_code, ROLE_INSPECTOR)
            e = self._esc(esc_code)
            if result not in (PASS, FAIL):
                raise BusinessError("BAD_RESULT", "急停测试结果只能为通过/失败")
            now = self.clock.now()
            self.conn.execute(
                "INSERT INTO emergency_tests(escalator_id,date,tester_id,result,response_ms,detail,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (e["id"], today(now), p["id"], result, response_ms, detail, iso(now)))
            if result == FAIL:
                self._event(e["id"], "ESTOP_TEST_FAILED", p["id"], e["config_version"],
                            detail or "急停装置测试失败", now)
                self.conn.commit()
                self.seal(esc_code, person_code, SRC_ESTOP,
                          reason="急停测试失败：" + (detail or "装置未可靠动作"),
                          _actor=p, _esc=e, _now=now)
                return {"result": FAIL, "sealed": True}
            self._event(e["id"], "ESTOP_TEST_PASS", p["id"], e["config_version"],
                        f"急停测试通过（{response_ms}ms）" if response_ms else "急停测试通过", now)
            self.conn.commit()
            return {"result": PASS, "sealed": False}

    # ---------- 启动（互斥 + 版本 + 检查完整性 + 封控阻断） ----------
    def acquire_start(self, esc_code: str, person_code: str, version: int) -> dict:
        """占用启动权。两名值班员不能同时启动同一扶梯。"""
        with self._lock:
            p = self._require_role(person_code, ROLE_DUTY)
            e = self._esc(esc_code)
            if e["lock_holder"] and e["lock_holder"] != person_code:
                holder = self._person(e["lock_holder"])
                raise BusinessError("START_BUSY",
                                    f"{holder['name']} 正在启动该扶梯，启动互斥中，请稍后")
            if version != e["config_version"]:
                raise BusinessError("VERSION_STALE",
                                    f"启动命令版本过期：你的 v{version}，当前 v{e['config_version']}，"
                                    f"方向配置已变更，请刷新后按当前版本提交")
            if e["run_state"] == ST_LOCKED:
                raise BusinessError("SEALED", "扶梯处于封控状态，禁止启动，须完成维修复检与复开流程")
            if e["run_state"] == ST_RUNNING:
                raise BusinessError("ALREADY_RUNNING", "扶梯已在运行")
            ok, problems = self._check_completeness(e)
            if not ok:
                raise BusinessError("CHECK_INCOMPLETE", "不允许启动：" + "；".join(problems))
            now = self.clock.now()
            self.conn.execute(
                "UPDATE escalators SET lock_holder=?, lock_at=?, run_state=?, updated_at=? WHERE id=?",
                (person_code, iso(now), ST_STARTING, iso(now), e["id"]))
            self._event(e["id"], "START_ACQUIRED", p["id"], version,
                        f"{p['name']} 占用启动权（v{version}）", now)
            self.conn.commit()
            return {"version": version, "holder": person_code}

    def start(self, esc_code: str, person_code: str, version: int) -> dict:
        """完整启动：占锁 -> 校验 -> 控制柜下发。控制柜返回跳停时进入封控。"""
        with self._lock:
            e = self._esc(esc_code)
            if not e["lock_holder"]:
                self.acquire_start(esc_code, person_code, version)
                e = self._esc(esc_code)
            elif e["lock_holder"] != person_code:
                holder = self._person(e["lock_holder"])
                raise BusinessError("START_BUSY", f"{holder['name']} 占用启动权")
            elif version != e["config_version"]:
                raise BusinessError("VERSION_STALE",
                                    f"启动命令版本过期：你的 v{version}，当前 v{e['config_version']}")
            p = self._person(person_code)
            now = self.clock.now()
            result = self.cabinet.start(e["code"], e["desired_direction"])
            if result.get("tripped"):
                self._event(e["id"], "TRIP", p["id"], version,
                            f"启动后跳停：{result.get('fault_code')}", now)
                self.conn.commit()
                # 跳停 -> 封控（内部统计反复跳停）
                self.seal(esc_code, person_code, SRC_FAULT,
                          reason=f"启动后跳停：{result.get('fault_code')}",
                          _actor=p, _now=now)
                return {"running": False, "tripped": True, "sealed": True}
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=?, lock_holder=NULL,"
                " lock_at=NULL, updated_at=? WHERE id=?",
                (ST_RUNNING, e["desired_direction"], iso(now), e["id"]))
            self._event(e["id"], "STARTED", p["id"], version,
                        f"扶梯启动，方向{'上行' if e['desired_direction'] == UP else '下行'}", now)
            self.conn.commit()
            return {"running": True, "tripped": False}

    def release_start(self, esc_code: str, person_code: str) -> None:
        """放弃占用启动权。"""
        with self._lock:
            p = self._person(person_code)
            e = self._esc(esc_code)
            if e["lock_holder"] == person_code:
                now = self.clock.now()
                self.conn.execute(
                    "UPDATE escalators SET lock_holder=NULL, lock_at=NULL, run_state=?, updated_at=? WHERE id=?",
                    (ST_STOPPED, iso(now), e["id"]))
                self._event(e["id"], "START_RELEASED", p["id"], e["config_version"],
                            "放弃启动", now)
                self.conn.commit()

    def scheduled_start(self, esc_code: str) -> None:
        """计划任务自动启动：只在检查合格且未封控时执行，否则记录阻断事件。"""
        with self._lock:
            e = self._esc(esc_code)
            if e["run_state"] == ST_RUNNING:
                return
            if e["run_state"] == ST_LOCKED:
                self._event(e["id"], "SCHEDULE_BLOCKED", None, e["config_version"],
                            "计划开梯被封控阻断", self.clock.now())
                self.conn.commit()
                return
            ok, problems = self._check_completeness(e)
            if not ok:
                self._event(e["id"], "SCHEDULE_BLOCKED", None, e["config_version"],
                            "计划开梯被班前检查阻断：" + "；".join(problems), self.clock.now())
                self.conn.commit()
                return
            self.cabinet.start(e["code"], e["desired_direction"])
            now = self.clock.now()
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=?, updated_at=? WHERE id=?",
                (ST_RUNNING, e["desired_direction"], iso(now), e["id"]))
            self._event(e["id"], "SCHEDULED_START", None, e["config_version"], "计划开梯", now)
            self.conn.commit()

    def stop(self, esc_code: str, person_code: str, reason: str = "手动停梯") -> None:
        with self._lock:
            p = self._require_role(person_code, ROLE_DUTY)
            e = self._esc(esc_code)
            self.cabinet.stop(e["code"])
            now = self.clock.now()
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=NULL, lock_holder=NULL,"
                " lock_at=NULL, updated_at=? WHERE id=?",
                (ST_STOPPED, iso(now), e["id"]))
            self._event(e["id"], "STOPPED", p["id"], e["config_version"], reason, now)
            self.conn.commit()

    def scheduled_stop(self, esc_code: str) -> None:
        with self._lock:
            e = self._esc(esc_code)
            if e["run_state"] != ST_RUNNING:
                return
            self.cabinet.stop(e["code"])
            now = self.clock.now()
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=NULL, updated_at=? WHERE id=?",
                (ST_STOPPED, iso(now), e["id"]))
            self._event(e["id"], "SCHEDULED_STOP", None, e["config_version"], "计划关梯", now)
            self.conn.commit()

    def poll_running(self, esc_code: str) -> dict:
        """运行中轮询控制柜：读到跳闸/客流事件时触发停梯封控。"""
        with self._lock:
            e = self._esc(esc_code)
            if e["run_state"] != ST_RUNNING:
                return {"state": e["run_state"]}
            cab = self.cabinet.read(e["code"])
            if cab.get("fault_code"):
                code = cab["fault_code"]
                self.cabinet.clear_fault(e["code"])
                self._event(e["id"], "TRIP", None, e["config_version"], f"运行中跳停：{code}",
                            self.clock.now())
                self.conn.commit()
                self.seal(esc_code, None, SRC_REPEAT, reason=f"运行中跳停：{code}")
                return {"tripped": True}
            if self.crowd.consume_incident(e["code"]):
                self.seal(esc_code, None, SRC_INCIDENT, reason="客流事件：现场拥挤需临时封控")
                return {"incident": True}
            return {"state": ST_RUNNING, "people": self.crowd.read(e["code"]).get(
                "people_per_5min", 0)}

    # ---------- 封控 / 围挡 ----------
    def seal(self, esc_code: str, actor_code: str | None, source: str, reason: str,
             _actor=None, _esc=None, _now=None) -> int:
        """停梯封控：停控制柜 -> LOCKED -> 建工单 -> 要求围挡（宽限期内未就位即告警事件）。

        actor_code 为 None 表示系统（调度轮询）自动封控；人工封控限值班员/巡检员。
        """
        with self._lock:
            if _actor is not None:
                p = _actor
            elif actor_code:
                p = self._require_role(actor_code, ROLE_DUTY, ROLE_INSPECTOR)
            else:
                p = None
            e = _esc or self._esc(esc_code)
            now = _now or self.clock.now()
            self.cabinet.stop(e["code"])
            # 反复跳停统计（滚动 24h 窗口）
            trip_count = e["trip_count"]
            window_start = parse_iso(e["trip_window_start"]) if e["trip_window_start"] else None
            if window_start is None or (now - window_start).total_seconds() > 86400:
                trip_count, window_start = 0, now
            trip_count += 1
            locked_reason = reason
            major_overhaul = trip_count >= TRIP_LIMIT and source in (SRC_FAULT, SRC_REPEAT)
            if major_overhaul:
                locked_reason += f"（24h 内第 {trip_count} 次跳停，锁定并安排大修）"
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=NULL, lock_holder=NULL,"
                " lock_at=NULL, lock_reason=?, trip_count=?, trip_window_start=?, updated_at=? WHERE id=?",
                (ST_LOCKED, locked_reason, trip_count, iso(window_start), iso(now), e["id"]))
            cur = self.conn.execute(
                "INSERT INTO faults(escalator_id,reporter_id,source,reason,status,arrival_due_at,created_at)"
                " VALUES(?,?,?,?,?,?,?)",
                (e["id"], p["id"] if p else None,
                 SRC_REPEAT if major_overhaul else source,
                 locked_reason, F_REPORTED, due_after(now, ARRIVAL_SLA_MIN), iso(now)))
            fault_id = cur.lastrowid
            self.conn.execute(
                "INSERT INTO repair_logs(fault_id,person_id,action,note,created_at)"
                " VALUES(?,?,?,?,?)",
                (fault_id, p["id"] if p else None, "REPORTED", reason, iso(now)))
            # 围挡记录（待就位）
            self.conn.execute(
                "INSERT INTO barricades(escalator_id,fault_id,placed_by,placed_at,expected_removal_at,status)"
                " VALUES(?,?,?,?,?,?)",
                (e["id"], fault_id, p["id"] if p else None, iso(now),
                 due_after(now, BARRICADE_GRACE_MIN), "PENDING"))
            source_label = {SRC_FAULT: "设备故障", SRC_INCIDENT: "客流事件",
                            SRC_ESTOP: "急停测试失败", SRC_REPEAT: "反复跳停"}.get(source, source)
            self._event(e["id"], "SEALED", p["id"] if p else None, e["config_version"],
                       f"停梯封控（{source_label}）：{locked_reason}；须在 {BARRICADE_GRACE_MIN} 分钟内设置围挡",
                       now)
            self.conn.commit()
            return fault_id

    def place_barricade(self, fault_id: int, person_code: str) -> None:
        """围挡就位。"""
        with self._lock:
            p = self._require_role(person_code, ROLE_DUTY, ROLE_INSPECTOR)
            b = self.conn.execute("SELECT * FROM barricades WHERE fault_id=? AND status='PENDING'",
                                  (fault_id,)).fetchone()
            if not b:
                raise BusinessError("NO_BARRICADE", "没有待就位的围挡记录")
            now = self.clock.now()
            late = parse_iso(b["expected_removal_at"]) < now  # 超过宽限期
            self.conn.execute(
                "UPDATE barricades SET status='UP', placed_by=?, placed_at=? WHERE id=?",
                (p["id"], iso(now), b["id"]))
            self._event(b["escalator_id"], "BARRICADE_UP", p["id"], None,
                        ("围挡已就位（超时，记录违规）" if late else "围挡已就位"), now)
            self.conn.commit()

    def check_barricade_sla(self) -> list[int]:
        """调度器周期调用：封控后宽限期仍无围挡 -> 记录围挡缺失事件。"""
        late_ids: list[int] = []
        with self._lock:
            now = self.clock.now()
            rows = self.conn.execute(
                "SELECT b.* FROM barricades b WHERE b.status='PENDING'").fetchall()
            for b in rows:
                if parse_iso(b["expected_removal_at"]) < now:
                    existed = self.conn.execute(
                        "SELECT 1 FROM run_events WHERE escalator_id=? AND type='BARRICADE_MISSING'"
                        " AND detail LIKE ? AND created_at >= ?",
                        (b["escalator_id"], f"%围挡{b['id']}%", b["placed_at"])).fetchone()
                    if not existed:
                        self._event(b["escalator_id"], "BARRICADE_MISSING", None, None,
                                    f"围挡{b['id']}缺失：封控超过 {BARRICADE_GRACE_MIN} 分钟仍未设置围挡",
                                    now)
                        late_ids.append(b["id"])
            if late_ids:
                self.conn.commit()
        return late_ids

    def open_faults(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT f.*, e.code esc_code, e.name esc_name, rp.name repair_name,"
            " pr.name reporter_name,"
            " (SELECT status FROM barricades b WHERE b.fault_id=f.id ORDER BY b.id DESC LIMIT 1)"
            " barricade_status FROM faults f"
            " JOIN escalators e ON e.id=f.escalator_id"
            " LEFT JOIN people rp ON rp.id=f.repair_person_id"
            " LEFT JOIN people pr ON pr.id=f.reporter_id"
            " WHERE f.status NOT IN (?,?) ORDER BY f.id DESC",
            (F_APPROVED, F_CLOSED)))

    def fault(self, fault_id: int) -> sqlite3.Row:
        row = self.conn.execute(
            "SELECT f.*, e.code esc_code, e.name esc_name FROM faults f"
            " JOIN escalators e ON e.id=f.escalator_id WHERE f.id=?", (fault_id,)).fetchone()
        if not row:
            raise BusinessError("NO_FAULT", "工单不存在")
        return row

    # ---------- 维修流程 ----------
    def dispatch(self, fault_id: int, repair_person_code: str, operator_code: str) -> None:
        """派单给指定维修员。"""
        with self._lock:
            self._require_role(operator_code, ROLE_SUPERVISOR, ROLE_DUTY)
            rp = self._require_role(repair_person_code, ROLE_REPAIR)
            f = self.fault(fault_id)
            if f["status"] not in (F_REPORTED,):
                raise BusinessError("BAD_STATUS", f"工单状态 {f['status']} 不可派单")
            now = self.clock.now()
            self.conn.execute(
                "UPDATE faults SET status=?, repair_person_id=? WHERE id=?",
                (F_ASSIGNED, rp["id"], fault_id))
            self._log(fault_id, operator_code, "ASSIGNED", f"派单给 {rp['name']}", now)
            self._event(f["escalator_id"], "REPAIR_DISPATCHED", rp["id"], None,
                        f"工单#{fault_id} 派单给 {rp['name']}，到场时限 {ARRIVAL_SLA_MIN} 分钟", now)
            self.conn.commit()

    def enroute(self, fault_id: int, person_code: str) -> None:
        with self._lock:
            rp = self._require_role(person_code, ROLE_REPAIR)
            f = self.fault(fault_id)
            if f["repair_person_id"] != rp["id"]:
                raise BusinessError("NOT_ASSIGNEE", "这不是你的工单")
            if f["status"] != F_ASSIGNED:
                raise BusinessError("BAD_STATUS", f"工单状态 {f['status']} 不可出发")
            now = self.clock.now()
            self.conn.execute("UPDATE faults SET status=? WHERE id=?", (F_ENROUTE, fault_id))
            self._log(fault_id, person_code, "ENROUTE", "维修员已出发", now)
            self.conn.commit()

    def arrive(self, fault_id: int, person_code: str) -> dict:
        """到场登记；超过到场 SLA 独立标记维修延期。"""
        with self._lock:
            rp = self._require_role(person_code, ROLE_REPAIR)
            f = self.fault(fault_id)
            if f["repair_person_id"] != rp["id"]:
                raise BusinessError("NOT_ASSIGNEE", "这不是你的工单")
            if f["status"] not in (F_ASSIGNED, F_ENROUTE):
                raise BusinessError("BAD_STATUS", f"工单状态 {f['status']} 不可到场")
            now = self.clock.now()
            late = parse_iso(f["arrival_due_at"]) < now
            self.conn.execute(
                "UPDATE faults SET status=?, arrived_at=?, repair_due_at=? WHERE id=?",
                (F_ARRIVED, iso(now),
                 due_after(now, REPAIR_SLA_MIN) if not late else f["repair_due_at"],
                 fault_id))
            if not late and not f["repair_due_at"]:
                # 正常到场，设定修复期限
                self.conn.execute("UPDATE faults SET repair_due_at=? WHERE id=?",
                                  (due_after(now, REPAIR_SLA_MIN), fault_id))
            self._log(fault_id, person_code, "ARRIVED",
                      "维修员到场（延期）" if late else "维修员到场", now)
            self._event(f["escalator_id"], "REPAIR_ARRIVED", rp["id"], None,
                        f"工单#{fault_id} {rp['name']}到场"
                        + ("（超过到场时限，维修延期）" if late else ""), now)
            if late:
                self._event(f["escalator_id"], "SLA_ARRIVAL_LATE", rp["id"], None,
                            f"工单#{fault_id} 到场超时（应于 {f['arrival_due_at']} 前）", now)
            self.conn.commit()
            return {"late": late}

    def begin_repair(self, fault_id: int, person_code: str, note: str = "") -> None:
        with self._lock:
            rp = self._require_role(person_code, ROLE_REPAIR)
            f = self.fault(fault_id)
            if f["repair_person_id"] != rp["id"]:
                raise BusinessError("NOT_ASSIGNEE", "这不是你的工单")
            if f["status"] not in (F_ARRIVED, F_REJECTED):
                raise BusinessError("BAD_STATUS", f"工单状态 {f['status']} 不可开始维修")
            now = self.clock.now()
            self.conn.execute("UPDATE faults SET status=? WHERE id=?", (F_REPAIRING, fault_id))
            self._log(fault_id, person_code, "REPAIRING", note or "开始维修", now)
            self.conn.commit()

    def complete_repair(self, fault_id: int, person_code: str, note: str = "") -> None:
        """修复完成：必须先确认围挡仍在（封控阻断维修完成的前提），等待复检。"""
        with self._lock:
            rp = self._require_role(person_code, ROLE_REPAIR)
            f = self.fault(fault_id)
            if f["repair_person_id"] != rp["id"]:
                raise BusinessError("NOT_ASSIGNEE", "这不是你的工单")
            if f["status"] != F_REPAIRING:
                raise BusinessError("BAD_STATUS", f"工单状态 {f['status']} 不可完成维修")
            b = self.conn.execute(
                "SELECT * FROM barricades WHERE fault_id=? AND status='UP'", (fault_id,)).fetchone()
            if not b:
                raise BusinessError("BARRICADE_REQUIRED",
                                    "围挡缺失：封控区域必须保持围挡，禁止在无围挡状态下完成维修")
            now = self.clock.now()
            self.conn.execute(
                "UPDATE faults SET status=?, repaired_at=? WHERE id=?",
                (F_REPAIRED, iso(now), fault_id))
            self._log(fault_id, person_code, "REPAIRED", note or "维修完成，待复检", now)
            self._event(f["escalator_id"], "REPAIR_DONE", rp["id"], None,
                        f"工单#{fault_id} 维修完成，等待巡检复检（围挡保持）", now)
            self.conn.commit()

    def check_repair_sla(self) -> list[int]:
        """调度器：修复期限超时 -> 维修延期事件（只通知一次）。"""
        overdue: list[int] = []
        with self._lock:
            now = self.clock.now()
            rows = self.conn.execute(
                "SELECT * FROM faults WHERE status IN (?,?,?,?)"
                " AND repair_due_at IS NOT NULL AND sla_notified=0",
                (F_ASSIGNED, F_ENROUTE, F_ARRIVED, F_REPAIRING)).fetchall()
            for f in rows:
                if parse_iso(f["repair_due_at"]) < now:
                    self._event(f["escalator_id"], "SLA_REPAIR_OVERDUE", None, None,
                                f"工单#{f['id']} 维修延期：超过修复时限 {f['repair_due_at']}", now)
                    self.conn.execute("UPDATE faults SET sla_notified=1 WHERE id=?", (f["id"],))
                    overdue.append(f["id"])
            if overdue:
                self.conn.commit()
        return overdue

    # ---------- 复检 + 审批（职责分离） ----------
    def submit_reinspection(self, fault_id: int, person_code: str, result: str,
                            note: str = "") -> int:
        with self._lock:
            insp = self._require_role(person_code, ROLE_INSPECTOR)
            if result not in (PASS, FAIL):
                raise BusinessError("BAD_RESULT", "复检结论只能为通过/失败")
            f = self.fault(fault_id)
            if f["status"] not in (F_REPAIRED, F_REJECTED):
                raise BusinessError("BAD_STATUS",
                                    f"工单状态 {f['status']}：仅维修完成后可提交复检")
            now = self.clock.now()
            cur = self.conn.execute(
                "INSERT INTO reinspections(fault_id,escalator_id,inspector_id,result,note,"
                "submitted_at,approval) VALUES(?,?,?,?,?,?, 'PENDING')",
                (fault_id, f["escalator_id"], insp["id"], result, note, iso(now)))
            rid = cur.lastrowid
            self._event(f["escalator_id"], "REINSPECT_SUBMITTED", insp["id"], None,
                        f"工单#{fault_id} 复检提交：{'合格' if result == PASS else '不合格'}，待主管审批",
                        now)
            self.conn.commit()
            return rid

    def approve_reinspection(self, reinspection_id: int, approver_code: str,
                             approve: bool, reason: str = "") -> None:
        """主管审批复检。巡检员不能批准自己的复检（同人拦截优先于角色校验）。"""
        with self._lock:
            ap = self._person(approver_code)
            r = self.conn.execute("SELECT * FROM reinspections WHERE id=?",
                                  (reinspection_id,)).fetchone()
            if not r:
                raise BusinessError("NO_REINSPECTION", "复检单不存在")
            if r["inspector_id"] == ap["id"]:
                raise BusinessError("SELF_APPROVAL", "职责分离：巡检员不能批准自己提交的复检")
            if ap["role"] != ROLE_SUPERVISOR:
                raise BusinessError("FORBIDDEN_ROLE",
                                    f"{ap['name']}（{D.ROLE_NAMES[ap['role']]}）无权审批，仅主管可操作")
            if r["approval"] != "PENDING":
                raise BusinessError("ALREADY_DECIDED", "复检单已审批")
            f = self.fault(r["fault_id"])
            now = self.clock.now()
            if approve and r["result"] == PASS:
                self.conn.execute(
                    "UPDATE reinspections SET approver_id=?, approval='APPROVED', decided_at=? WHERE id=?",
                    (ap["id"], iso(now), reinspection_id))
                self.conn.execute("UPDATE faults SET status=? WHERE id=?", (F_APPROVED, f["id"]))
                self._event(f["escalator_id"], "REINSPECT_APPROVED", ap["id"], None,
                            f"工单#{f['id']} 复检经主管审批通过，可进入复开流程", now)
            else:
                why = reason or ("复检结论不合格" if r["result"] == FAIL else "主管退回")
                self.conn.execute(
                    "UPDATE reinspections SET approver_id=?, approval='REJECTED', reject_reason=?,"
                    " decided_at=? WHERE id=?", (ap["id"], why, iso(now), reinspection_id))
                self.conn.execute(
                    "UPDATE faults SET status=?, reject_count=reject_count+1 WHERE id=?",
                    (F_REJECTED, f["id"]))
                self._event(f["escalator_id"], "REINSPECT_REJECTED", ap["id"], None,
                            f"工单#{f['id']} 复检被退回：{why}，重新维修", now)
            self.conn.commit()

    def list_reinspections(self, pending_only: bool = False) -> list[sqlite3.Row]:
        sql = ("SELECT r.*, f.reason fault_reason, e.code esc_code, e.name esc_name,"
               " pi.name inspector_name, pa.name approver_name FROM reinspections r"
               " JOIN faults f ON f.id=r.fault_id"
               " JOIN escalators e ON e.id=r.escalator_id"
               " JOIN people pi ON pi.id=r.inspector_id"
               " LEFT JOIN people pa ON pa.id=r.approver_id")
        if pending_only:
            sql += " WHERE r.approval='PENDING'"
        sql += " ORDER BY r.id DESC"
        return list(self.conn.execute(sql))

    # ---------- 围挡撤除 + 复开（提前复开独立拦截） ----------
    def remove_barricade_and_reopen(self, fault_id: int, person_code: str,
                                    confirmed: bool = False) -> dict:
        """复开确认：复检审批通过才可撤围挡复开；提前复开被阻断。

        复开是高风险动作，要求 confirmed=True（前端复开按钮与普通操作明显隔离，
        并弹二次确认）。
        """
        with self._lock:
            p = self._require_role(person_code, ROLE_DUTY)
            f = self.fault(fault_id)
            e = self._esc_by_id(f["escalator_id"])
            if not confirmed:
                raise BusinessError("CONFIRM_REQUIRED", "复开必须二次确认")
            if f["status"] != F_APPROVED:
                stage = {
                    F_REPORTED: "尚未派单维修", F_ASSIGNED: "已派单未到场",
                    F_ENROUTE: "维修员在途", F_ARRIVED: "维修员刚到场",
                    F_REPAIRING: "维修进行中", F_REPAIRED: "维修完成但复检尚未审批通过",
                    F_REJECTED: "复检被退回，需重新维修",
                }.get(f["status"], f["status"])
                self._event(e["id"], "REOPEN_BLOCKED", p["id"], None,
                            f"工单#{fault_id} 提前复开被阻断：{stage}", self.clock.now())
                self.conn.commit()
                raise BusinessError("EARLY_REOPEN", f"提前复开被阻断：{stage}，禁止恢复运行")
            b = self.conn.execute(
                "SELECT * FROM barricades WHERE fault_id=? AND status='UP'", (fault_id,)).fetchone()
            if not b:
                raise BusinessError("NO_BARRICADE", "围挡记录异常：未找到就位围挡")
            now = self.clock.now()
            self.conn.execute(
                "UPDATE barricades SET status='DOWN', removed_by=?, removed_at=? WHERE id=?",
                (p["id"], iso(now), b["id"]))
            # 复开仍需当日班前检查合格
            ok, problems = self._check_completeness(e)
            if not ok:
                self._event(e["id"], "REOPEN_BLOCKED", p["id"], None,
                            "复开被班前检查阻断：" + "；".join(problems), now)
                self.conn.commit()
                raise BusinessError("CHECK_INCOMPLETE", "复开被阻断：" + "；".join(problems))
            result = self.cabinet.start(e["code"], e["desired_direction"])
            if result.get("tripped"):
                self._event(e["id"], "TRIP", p["id"], e["config_version"],
                            f"复开后再次跳停：{result.get('fault_code')}", now)
                self.conn.commit()
                self.seal(e["code"], person_code, SRC_FAULT,
                          reason=f"复开后再次跳停：{result.get('fault_code')}", _actor=p, _now=now)
                return {"reopened": False, "tripped": True}
            self.conn.execute(
                "UPDATE escalators SET run_state=?, actual_direction=?, lock_reason=NULL,"
                " updated_at=? WHERE id=?",
                (ST_RUNNING, e["desired_direction"], iso(now), e["id"]))
            self.conn.execute("UPDATE faults SET status=?, closed_at=? WHERE id=?",
                              (F_CLOSED, iso(now), fault_id))
            self._event(e["id"], "BARRICADE_REMOVED", p["id"], None,
                        f"工单#{fault_id} 围挡撤除", now)
            self._event(e["id"], "REOPENED", p["id"], e["config_version"],
                        "复开确认，扶梯恢复运行", now)
            self.conn.commit()
            return {"reopened": True}

    def _esc_by_id(self, esc_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM escalators WHERE id=?", (esc_id,)).fetchone()
        if not row:
            raise BusinessError("NO_ESCALATOR", "扶梯不存在")
        return row

    # ---------- 客服绕行信息 ----------
    def publish_detour(self, esc_code: str, person_code: str, message: str,
                       floor: int | None = None) -> int:
        with self._lock:
            svc = self._require_role(person_code, ROLE_SERVICE)
            e = self._esc(esc_code)
            if e["run_state"] != ST_LOCKED:
                raise BusinessError("NOT_SEALED", "仅封控中的扶梯需要发布绕行信息")
            now = self.clock.now()
            cur = self.conn.execute(
                "INSERT INTO detours(escalator_id,floor,message,published_by,created_at)"
                " VALUES(?,?,?,?,?)",
                (e["id"], floor if floor is not None else e["floor_from"], message,
                 svc["id"], iso(now)))
            self._event(e["id"], "DETOUR_PUBLISHED", svc["id"], None,
                       f"客服发布绕行提示：{message}", now)
            self.conn.commit()
            return cur.lastrowid

    def retract_detour(self, detour_id: int, person_code: str) -> None:
        with self._lock:
            svc = self._require_role(person_code, ROLE_SERVICE)
            d = self.conn.execute("SELECT * FROM detours WHERE id=?", (detour_id,)).fetchone()
            if not d:
                raise BusinessError("NO_DETOUR", "绕行信息不存在")
            if d["status"] != "ACTIVE":
                raise BusinessError("ALREADY_RETRACTED", "绕行信息已撤回")
            now = self.clock.now()
            self.conn.execute(
                "UPDATE detours SET status='RETRACTED', retracted_by=?, retracted_at=? WHERE id=?",
                (svc["id"], iso(now), detour_id))
            self._event(d["escalator_id"], "DETOUR_RETRACTED", svc["id"], None,
                        "客服撤回绕行提示", now)
            self.conn.commit()

    def active_detours(self) -> list[sqlite3.Row]:
        return list(self.conn.execute(
            "SELECT d.*, e.code esc_code, e.name esc_name FROM detours d"
            " JOIN escalators e ON e.id=d.escalator_id WHERE d.status='ACTIVE'"
            " ORDER BY d.id DESC"))

    # ---------- 照片 ----------
    def add_photo(self, fault_id: int, relpath: str, original_name: str,
                  person_code: str) -> int:
        with self._lock:
            p = self._person(person_code)
            f = self.fault(fault_id)
            now = self.clock.now()
            cur = self.conn.execute(
                "INSERT INTO photos(fault_id,relpath,original_name,uploaded_by,created_at)"
                " VALUES(?,?,?,?,?)",
                (fault_id, relpath, original_name, p["id"], iso(now)))
            self._event(f["escalator_id"], "PHOTO_ADDED", p["id"], None,
                        f"工单#{fault_id} 上传故障照片 {original_name}", now)
            self.conn.commit()
            return cur.lastrowid

    def photos(self, fault_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM photos WHERE fault_id=? ORDER BY id",
                                      (fault_id,)))

    def _log(self, fault_id: int, person_code: str | None, action: str, note: str,
             now: datetime) -> None:
        pid = None
        if person_code:
            pid = self._person(person_code)["id"]
        self.conn.execute(
            "INSERT INTO repair_logs(fault_id,person_id,action,note,created_at)"
            " VALUES(?,?,?,?,?)", (fault_id, pid, action, note, iso(now)))

    # ---------- 封控计时 / 仪表盘 ----------
    def lockdowns(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT f.id fault_id, f.source, f.reason, f.status, f.created_at sealed_at,"
            " f.arrival_due_at, f.arrived_at, f.repair_due_at, f.repaired_at,"
            " rp.name repair_name, e.code esc_code, e.name esc_name,"
            " (SELECT status FROM barricades b WHERE b.fault_id=f.id ORDER BY b.id DESC LIMIT 1)"
            " barricade_status"
            " FROM faults f JOIN escalators e ON e.id=f.escalator_id"
            " LEFT JOIN people rp ON rp.id=f.repair_person_id"
            " WHERE f.status NOT IN (?,?) ORDER BY f.id DESC",
            (F_APPROVED, F_CLOSED)).fetchall()
        now = self.clock.now()
        out = []
        for r in rows:
            d = dict(r)
            d["sealed_minutes"] = int((now - parse_iso(r["sealed_at"])).total_seconds() // 60)
            d["arrival_overdue"] = (not r["arrived_at"]
                                    and parse_iso(r["arrival_due_at"]) < now)
            d["repair_overdue"] = (not r["repaired_at"] and r["repair_due_at"]
                                   and parse_iso(r["repair_due_at"]) < now)
            out.append(d)
        return out

    def detail(self, esc_code: str) -> dict:
        e = self._esc(esc_code)
        ins = self.get_inspection(esc_code)
        items = self.inspection_items(ins["id"]) if ins else []
        active_fault = self.conn.execute(
            "SELECT * FROM faults WHERE escalator_id=? AND status != ? ORDER BY id DESC LIMIT 1",
            (e["id"], F_CLOSED)).fetchone()
        barricade = self.conn.execute(
            "SELECT * FROM barricades WHERE escalator_id=? ORDER BY id DESC LIMIT 1",
            (e["id"],)).fetchone()
        return {"escalator": dict(e), "inspection": dict(ins) if ins else None,
                "items": [dict(x) for x in items],
                "active_fault": dict(active_fault) if active_fault else None,
                "barricade": dict(barricade) if barricade else None,
                "cabinet": self.cabinet.read(e["code"]),
                "crowd": self.crowd.read(e["code"])}
