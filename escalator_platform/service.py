"""核心领域服务：设备状态机与全部业务规则。

设备主状态：stopped / running / locked(封控)。
故障流：  封控 → 设围挡 → 报修 → 到场 → 修复 → 复检提交 → 他人批准
          → 撤围挡 → 复开确认 → 启动
客流流：  封控 → 设围挡 → 客流回落 → 撤围挡 → 复开确认 → 启动
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import errors as E

PRECHECK_ITEMS = ["steps", "comb", "handrail", "lighting"]  # 急停装置单列
ESTOP_NEED_PASS = True
REPEAT_TRIP_LIMIT = 3          # 窗口内跳闸达到此次数 => 反复跳停，自动报修
TRIP_WINDOW = timedelta(hours=24)
REPAIR_SLA = timedelta(minutes=60)
START_LOCK_TTL = timedelta(seconds=30)
CROWD_LOCK_LEVEL = "critical"  # 达到该密度触发客流封控


def _now() -> datetime:
    return datetime.now().replace(microsecond=0)


class PlatformService:
    def __init__(self, conn: sqlite3.Connection, cabinet, crowd,
                 photo_dir: str | Path, clock=_now):
        self.conn = conn
        self.cabinet = cabinet
        self.crowd = crowd
        self.photo_dir = Path(photo_dir)
        self.photo_dir.mkdir(parents=True, exist_ok=True)
        self.clock = clock
        # 进程内互斥：DB 锁之外再保护 start_lock 的检查-置位原子性
        self._mem_lock = threading.RLock()

    # ------------------------------------------------------------------ utils
    def _ts(self) -> str:
        return self.clock().isoformat(timespec="seconds")

    def _event(self, conn, esc_id, etype, actor_id=None, **payload):
        cur = conn.execute(
            "INSERT INTO escalator_events(esc_id, at, type, actor_id, payload)"
            " VALUES (?,?,?,?,?)",
            (esc_id, self._ts(), etype, actor_id, json.dumps(payload, ensure_ascii=False)),
        )
        return cur.lastrowid

    def _staff(self, conn, staff_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM staff WHERE id=?", (staff_id,)).fetchone()
        if not row:
            raise E.NotFound(f"人员不存在: {staff_id}")
        return row

    def _esc(self, conn, esc_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM escalators WHERE id=?", (esc_id,)).fetchone()
        if not row:
            raise E.NotFound(f"扶梯不存在: {esc_id}")
        return row

    @staticmethod
    def _require_role(staff: sqlite3.Row, roles, message):
        if staff["role"] not in roles:
            raise E.DutySeparation(message)

    # ------------------------------------------------------------- queries
    def list_staff(self):
        return [dict(r) for r in self.conn.execute("SELECT * FROM staff ORDER BY id")]

    def list_escalators(self):
        rows = self.conn.execute("SELECT * FROM escalators ORDER BY floor_from, id").fetchall()
        result = []
        for r in rows:
            d = dict(r)
            d["cabinet"] = self.cabinet.readings(r["id"])
            d["crowd"] = self.crowd.readings(r["id"], self.clock())
            d.update(self._lockdown_summary(r["id"]))
            result.append(d)
        return result

    def _lockdown_summary(self, esc_id):
        ld = self.conn.execute(
            "SELECT * FROM lockdowns WHERE esc_id=? AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (esc_id,)).fetchone()
        out = {"active_lockdown": None, "active_order": None, "active_recheck": None,
               "detour": None}
        if ld:
            d = dict(ld)
            d["elapsed_sec"] = int((self.clock() - datetime.fromisoformat(
                ld["started_at"])).total_seconds())
            out["active_lockdown"] = d
            order = self.conn.execute(
                "SELECT * FROM repair_orders WHERE lockdown_id=? ORDER BY id DESC LIMIT 1",
                (ld["id"],)).fetchone()
            if order:
                o = dict(order)
                o["overdue_sec"] = max(0, int(
                    (self.clock() - datetime.fromisoformat(o["due_at"])).total_seconds()))
                out["active_order"] = o
            rc = self.conn.execute(
                "SELECT * FROM rechecks WHERE esc_id=? ORDER BY id DESC LIMIT 1",
                (esc_id,)).fetchone()
            if rc:
                out["active_recheck"] = dict(rc)
        dt = self.conn.execute(
            "SELECT * FROM detours WHERE esc_id=? AND cancelled_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (esc_id,)).fetchone()
        if dt:
            out["detour"] = dict(dt)
        return out

    def escalator_detail(self, esc_id):
        with self._mem_lock:
            conn = self.conn
            r = self._esc(conn, esc_id)
            d = dict(r)
            d["cabinet"] = self.cabinet.readings(esc_id)
            d["crowd"] = self.crowd.readings(esc_id, self.clock())
            d.update(self._lockdown_summary(esc_id))
            d["precheck_today"] = self.today_precheck(esc_id)
            d["lockdowns"] = [dict(x) for x in conn.execute(
                "SELECT * FROM lockdowns WHERE esc_id=? ORDER BY id DESC LIMIT 10",
                (esc_id,))]
            d["orders"] = [dict(x) for x in conn.execute(
                "SELECT * FROM repair_orders WHERE esc_id=? ORDER BY id DESC LIMIT 10",
                (esc_id,))]
            d["rechecks"] = [dict(x) for x in conn.execute(
                "SELECT * FROM rechecks WHERE esc_id=? ORDER BY id DESC LIMIT 10",
                (esc_id,))]
            return d

    def today_precheck(self, esc_id, date: str | None = None):
        date = date or self.clock().date().isoformat()
        row = self.conn.execute(
            "SELECT * FROM prechecks WHERE esc_id=? AND check_date=?",
            (esc_id, date)).fetchone()
        return dict(row) if row else None

    def events(self, esc_id=None, etype=None, limit=200):
        sql = ("SELECT e.*, s.name AS actor_name FROM escalator_events e"
               " LEFT JOIN staff s ON s.id=e.actor_id WHERE 1=1")
        args = []
        if esc_id:
            sql += " AND e.esc_id=?"; args.append(esc_id)
        if etype:
            sql += " AND e.type=?"; args.append(etype)
        sql += " ORDER BY e.id DESC LIMIT ?"; args.append(limit)
        rows = self.conn.execute(sql, args).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["payload"] = json.loads(d.get("payload") or "{}")
            except json.JSONDecodeError:
                d["payload"] = {}
            out.append(d)
        return out

    def pending_rechecks(self):
        rows = self.conn.execute(
            "SELECT r.*, s.name AS inspector_name, e.name AS esc_name FROM rechecks r"
            " JOIN staff s ON s.id=r.inspector_id"
            " JOIN escalators e ON e.id=r.esc_id"
            " WHERE r.result='pending' ORDER BY r.id").fetchall()
        return [dict(r) for r in rows]

    # --------------------------------------------------------- 方向与版本
    def set_direction(self, staff_id, esc_id, direction):
        """调整运行方向计划，版本号 +1；正在运行/封控中不可调整。"""
        if direction not in ("up", "down"):
            raise E.PlatformError  # pragma: no cover
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty",), "仅值班员可设置运行方向")
            esc = self._esc(conn, esc_id)
            if esc["status"] == "running":
                raise E.InvalidTransition("运行中不可切换方向，请先停梯")
            if esc["status"] == "locked":
                raise E.InvalidTransition("封控中不可调整方向")
            conn.execute(
                "UPDATE escalators SET direction=?, direction_version=direction_version+1,"
                " updated_at=? WHERE id=?",
                (direction, self._ts(), esc_id))
            self._event(conn, esc_id, "DIRECTION_SET", staff_id,
                        direction=direction,
                        version=esc["direction_version"] + 1,
                        previous=esc["direction"])
            return esc["direction_version"] + 1

    def set_schedule(self, staff_id, esc_id, on_time, off_time, enabled):
        with self._mem_lock, self.conn:
            conn = self.conn
            self._staff(conn, staff_id)
            conn.execute(
                "UPDATE escalators SET schedule_enabled=?, on_time=?, off_time=?,"
                " updated_at=? WHERE id=?",
                (1 if enabled else 0, on_time, off_time, self._ts(), esc_id))
            self._event(conn, esc_id, "SCHEDULE_SET", staff_id,
                        on=on_time, off=off_time, enabled=bool(enabled))

    # ------------------------------------------------------------- 班前检查
    def submit_precheck(self, staff_id, esc_id, results: dict,
                        check_date: str | None = None):
        """提交班前检查。results: steps/comb/handrail/lighting=pass|fail|na，
        estop=pass|fail。五项全部合格(pass)才允许启动。"""
        check_date = check_date or self.clock().date().isoformat()
        items = dict(results)
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("inspector",), "仅巡检员可提交班前检查")
            self._esc(conn, esc_id)

            missing = [k for k in PRECHECK_ITEMS + ["estop"] if k not in items]
            if missing:
                raise E.InspectionIncomplete(f"检查漏项: {', '.join(missing)}")
            for k in PRECHECK_ITEMS:
                if items[k] not in ("pass", "fail", "na"):
                    raise E.InspectionIncomplete(f"检查项取值非法: {k}")
            if items["estop"] not in ("pass", "fail"):
                raise E.EstopTestFailed("急停装置测试结果必须为通过或失败")

            # 急停测试失败独立成类
            if items["estop"] == "fail":
                conn.execute(
                    "INSERT INTO prechecks(esc_id, check_date, inspector_id,"
                    " steps_result, comb_result, handrail_result, lighting_result,"
                    " estop_result, completed_at, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(esc_id, check_date) DO UPDATE SET"
                    " inspector_id=excluded.inspector_id,"
                    " steps_result=excluded.steps_result,"
                    " comb_result=excluded.comb_result,"
                    " handrail_result=excluded.handrail_result,"
                    " lighting_result=excluded.lighting_result,"
                    " estop_result='fail', completed_at=NULL",
                    (esc_id, check_date, staff_id,
                     items["steps"], items["comb"], items["handrail"],
                     items["lighting"], "fail", None, self._ts()))
                self._event(conn, esc_id, "PRECHECK_FAIL", staff_id,
                            reason="estop", date=check_date)
                raise E.EstopTestFailed("急停装置测试失败，禁止启动，须立即报修")

            not_passed = [k for k in PRECHECK_ITEMS if items[k] != "pass"]
            completed = self._ts() if not not_passed else None
            conn.execute(
                "INSERT INTO prechecks(esc_id, check_date, inspector_id,"
                " steps_result, comb_result, handrail_result, lighting_result,"
                " estop_result, completed_at, created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(esc_id, check_date) DO UPDATE SET"
                " inspector_id=excluded.inspector_id,"
                " steps_result=excluded.steps_result,"
                " comb_result=excluded.comb_result,"
                " handrail_result=excluded.handrail_result,"
                " lighting_result=excluded.lighting_result,"
                " estop_result=excluded.estop_result,"
                " completed_at=excluded.completed_at",
                (esc_id, check_date, staff_id,
                 items["steps"], items["comb"], items["handrail"],
                 items["lighting"], "pass", completed, self._ts()))
            if not_passed:
                labels = {"steps": "梯级", "comb": "梳齿板",
                          "handrail": "扶手带", "lighting": "照明"}
                self._event(conn, esc_id, "PRECHECK_FAIL", staff_id,
                            items=not_passed, date=check_date)
                raise E.InspectionIncomplete(
                    "班前检查未全部合格："
                    + "、".join(labels[k] + ("不合格" if items[k] == "fail" else "判为不适用")
                               for k in not_passed))
            self._event(conn, esc_id, "PRECHECK_PASS", staff_id, date=check_date)
            return True

    # ------------------------------------------------------------- 启动互斥
    def prepare_start(self, staff_id, esc_id):
        """打开发梯确认：占用该扶梯的启动流程，返回当前方向与版本。

        两名值班员同时打开/提交时，仅一人能占用；占用 30 秒自动过期。
        """
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty",), "仅值班员可启动扶梯，客服请使用绕行发布")
            esc = self._esc(conn, esc_id)
            if esc["status"] == "locked":
                raise E.InvalidTransition("扶梯封控中，禁止启动（封控阻断）")
            if esc["status"] == "running":
                raise E.InvalidTransition("扶梯已在运行")
            self._acquire_start_lock(conn, esc, staff_id)
            self._event(conn, esc_id, "START_PREPARE", staff_id,
                        direction=esc["direction"],
                        version=esc["direction_version"])
            return {"direction": esc["direction"],
                    "version": esc["direction_version"]}

    def _acquire_start_lock(self, conn, esc, staff_id) -> bool:
        """尝试占有启动流程。返回 True；他人持锁则 ConflictError。"""
        owner = esc["start_lock_owner"]
        if owner and owner != staff_id:
            locked_at = datetime.fromisoformat(esc["start_lock_at"])
            if self.clock() - locked_at < START_LOCK_TTL:
                raise E.ConflictError(
                    f"值班员 {owner} 正在启动该扶梯，两名值班员不能同时启动")
        conn.execute(
            "UPDATE escalators SET start_lock_owner=?, start_lock_at=?,"
            " updated_at=? WHERE id=?",
            (staff_id, self._ts(), self._ts(), esc["id"]))
        return True

    def release_start_lock(self, staff_id, esc_id):
        with self._mem_lock, self.conn:
            conn = self.conn
            esc = self._esc(conn, esc_id)
            # 仅占用者本人或过期后可释放
            if esc["start_lock_owner"] == staff_id:
                conn.execute(
                    "UPDATE escalators SET start_lock_owner=NULL, start_lock_at=NULL,"
                    " updated_at=? WHERE id=?", (self._ts(), esc_id))
                self._event(conn, esc_id, "START_CANCEL", staff_id)

    def start(self, staff_id, esc_id, direction, version):
        """启动扶梯：职责=值班员；需当日检查全过；方向与版本必须为当前值。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty",), "仅值班员可启动扶梯，客服请使用绕行发布")
            esc = self._esc(conn, esc_id)

            if esc["status"] == "locked":
                raise E.InvalidTransition("扶梯封控中，禁止启动（封控阻断）")
            if esc["status"] == "running":
                raise E.InvalidTransition("扶梯已在运行")

            # 他人仍持有启动占用 => 互斥拒绝
            owner = esc["start_lock_owner"]
            if owner and owner != staff_id and \
                    self.clock() - datetime.fromisoformat(esc["start_lock_at"]) < START_LOCK_TTL:
                raise E.ConflictError(
                    f"值班员 {owner} 正在启动该扶梯，两名值班员不能同时启动")
            self._acquire_start_lock(conn, esc, staff_id)
            try:
                # 启动命令需按当前版本提交
                version = int(version)
                if direction != esc["direction"] or version != esc["direction_version"]:
                    self._event(conn, esc_id, "START_REJECTED", staff_id,
                                reason="direction_or_version",
                                sent=(direction, version),
                                current=(esc["direction"], esc["direction_version"]))
                    raise E.DirectionMismatch(
                        f"方向设置错误或版本过期：当前为"
                        f" {esc['direction']} v{esc['direction_version']}，"
                        f"命令为 {direction} v{version}，请刷新后重试")

                pre = self.today_precheck(esc_id)
                if not pre or not pre["completed_at"]:
                    self._event(conn, esc_id, "START_REJECTED", staff_id,
                                reason="precheck_incomplete")
                    raise E.InspectionIncomplete(
                        "今日班前检查未完成或未全部合格，禁止启动")

                conn.execute(
                    "UPDATE escalators SET status='running',"
                    " start_lock_owner=NULL, start_lock_at=NULL,"
                    " updated_at=? WHERE id=?", (self._ts(), esc_id))
                self.cabinet.set_running(esc_id, True)
                self._event(conn, esc_id, "START", staff_id,
                            direction=direction, version=version,
                            precheck_date=pre["check_date"])
                return True
            except Exception:
                # 命令被拒即释放占用，允许他人按新版本重试
                conn.execute(
                    "UPDATE escalators SET start_lock_owner=NULL, start_lock_at=NULL,"
                    " updated_at=? WHERE id=?", (self._ts(), esc_id))
                raise

    def stop(self, staff_id, esc_id, reason="manual"):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty",), "仅值班员可停梯")
            esc = self._esc(conn, esc_id)
            if esc["status"] != "running":
                raise E.InvalidTransition("仅运行中的扶梯可以停梯")
            conn.execute("UPDATE escalators SET status='stopped', updated_at=? WHERE id=?",
                         (self._ts(), esc_id))
            self.cabinet.set_running(esc_id, False)
            self._event(conn, esc_id, "STOP", staff_id, reason=reason)

    # ------------------------------------------------------------- 封控
    def lock_down(self, esc_id, reason, detail="", actor_id=None,
                  auto_order=False):
        """触发停梯封控（故障/客流）。设备转入 locked 并等待围挡。"""
        with self._mem_lock, self.conn:
            return self._do_lock_down(self.conn, esc_id, reason, detail,
                                      actor_id, auto_order)

    def _do_lock_down(self, conn, esc_id, reason, detail="", actor_id=None,
                      auto_order=False):
        esc = conn.execute("SELECT * FROM escalators WHERE id=?", (esc_id,)).fetchone()
        if not esc:
            raise E.NotFound(f"扶梯不存在: {esc_id}")
        if esc["status"] == "locked":
            return None
        if esc["status"] == "running":
            self.cabinet.set_running(esc_id, False)
        cur = conn.execute(
            "INSERT INTO lockdowns(esc_id, reason, detail, started_at)"
            " VALUES (?,?,?,?)",
            (esc_id, reason, detail, self._ts()))
        ld_id = cur.lastrowid
        conn.execute(
            "UPDATE escalators SET status='locked', barrier=0, updated_at=? WHERE id=?",
            (self._ts(), esc_id))
        self._event(conn, esc_id, "LOCKDOWN", actor_id,
                    reason=reason, detail=detail, lockdown_id=ld_id,
                    auto_order=auto_order)
        if auto_order:
            self._create_order(conn, esc_id, ld_id,
                               description=f"自动报修：{detail}",
                               reporter_id=actor_id or "SYS")
        return ld_id

    def set_barrier(self, staff_id, esc_id, in_place: bool):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty", "inspector"), "仅现场值班员/巡检员可设置围挡")
            esc = self._esc(conn, esc_id)
            ld = self._active_lockdown(conn, esc_id)
            if not ld:
                raise E.InvalidTransition("当前无封控，不需要围挡")
            if in_place:
                conn.execute("UPDATE escalators SET barrier=1, updated_at=? WHERE id=?",
                             (self._ts(), esc_id))
                conn.execute("UPDATE lockdowns SET barrier_set_at=? WHERE id=?",
                             (self._ts(), ld["id"]))
                self._event(conn, esc_id, "BARRIER_SET", staff_id, lockdown_id=ld["id"])
            else:
                # 撤围挡有前置条件，防止提前撤除
                self._assert_barrier_removable(conn, esc, ld)
                conn.execute("UPDATE escalators SET barrier=0, updated_at=? WHERE id=?",
                             (self._ts(), esc_id))
                self._event(conn, esc_id, "BARRIER_REMOVE", staff_id, lockdown_id=ld["id"])

    def _active_lockdown(self, conn, esc_id):
        return conn.execute(
            "SELECT * FROM lockdowns WHERE esc_id=? AND ended_at IS NULL"
            " ORDER BY id DESC LIMIT 1", (esc_id,)).fetchone()

    def _assert_barrier_removable(self, conn, esc, ld):
        if ld["reason"] == "fault":
            order = conn.execute(
                "SELECT * FROM repair_orders WHERE lockdown_id=? ORDER BY id DESC LIMIT 1",
                (ld["id"],)).fetchone()
            rc = conn.execute(
                "SELECT * FROM rechecks WHERE esc_id=? ORDER BY id DESC LIMIT 1",
                (esc["id"],)).fetchone()
            if not order or not order["fixed_at"]:
                raise E.EarlyReopen("维修尚未完成，不得提前撤除围挡")
            if not rc or rc["result"] != "approved":
                raise E.EarlyReopen("复检尚未经他人批准，不得提前撤除围挡")
        else:  # 客流
            crowd = self.crowd.readings(esc["id"], self.clock())
            if crowd["density"] not in ("low", "normal"):
                raise E.EarlyReopen("客流仍未回落，不得提前撤除围挡")

    # ------------------------------------------------------------- 报修维修
    def report_repair(self, staff_id, esc_id, description, photo_path=None):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty", "inspector"), "客服仅能发布绕行信息，不能报修")
            ld = self._active_lockdown(conn, esc_id)
            if not ld:
                raise E.InvalidTransition("仅封控中的扶梯可以报修")
            if ld["reason"] != "fault":
                raise E.InvalidTransition("客流封控无需报修")
            exists = conn.execute(
                "SELECT id FROM repair_orders WHERE lockdown_id=?", (ld["id"],)).fetchone()
            if exists:
                raise E.InvalidTransition("该封控单已有报修工单")
            return self._create_order(conn, esc_id, ld["id"], description,
                                      staff_id, photo_path)

    def _create_order(self, conn, esc_id, ld_id, description,
                      reporter_id, photo_path=None):
        due = self.clock() + REPAIR_SLA
        cur = conn.execute(
            "INSERT INTO repair_orders(esc_id, lockdown_id, description, photo_path,"
            " reporter_id, reported_at, due_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (esc_id, ld_id, description, photo_path, reporter_id,
             self._ts(), due.isoformat(timespec="seconds")))
        oid = cur.lastrowid
        self._event(conn, esc_id, "REPAIR_REPORT", reporter_id,
                    order_id=oid, due_at=due.isoformat(timespec="seconds"),
                    photo=photo_path, description=description)
        return oid

    def save_photo(self, filename: str, data: bytes) -> str:
        safe = Path(filename).name.replace("..", "_")
        stamp = self.clock().strftime("%Y%m%d%H%M%S")
        rel = f"{stamp}_{safe}"
        (self.photo_dir / rel).write_bytes(data)
        return rel

    def arrive(self, staff_id, esc_id):
        """维修到场：必须先有围挡（围挡缺失独立阻断）。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("service",), "仅维修人员可登记到场")
            esc = self._esc(conn, esc_id)
            ld = self._active_lockdown(conn, esc_id)
            if not ld:
                raise E.InvalidTransition("当前无封控，不能登记到场")
            if not esc["barrier"]:
                self._event(conn, esc_id, "ARRIVE_BLOCKED", staff_id,
                            reason="barrier_missing")
                raise E.BarrierMissing("围挡尚未设置，维修不得进场")
            order = conn.execute(
                "SELECT * FROM repair_orders WHERE lockdown_id=? ORDER BY id DESC LIMIT 1",
                (ld["id"],)).fetchone()
            if not order:
                raise E.InvalidTransition("尚未报修，无维修工单")
            if order["arrived_at"]:
                raise E.InvalidTransition("已登记到场")
            conn.execute("UPDATE repair_orders SET assignee_id=?, arrived_at=? WHERE id=?",
                         (staff_id, self._ts(), order["id"]))
            self._event(conn, esc_id, "REPAIR_ARRIVE", staff_id, order_id=order["id"])

    def _open_order(self, conn, esc_id):
        ld = self._active_lockdown(conn, esc_id)
        if not ld:
            raise E.InvalidTransition("当前无封控工单")
        order = conn.execute(
            "SELECT * FROM repair_orders WHERE lockdown_id=? ORDER BY id DESC LIMIT 1",
            (ld["id"],)).fetchone()
        if not order:
            raise E.InvalidTransition("尚未报修，无维修工单")
        return order

    def mark_fixed(self, staff_id, esc_id, note=""):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("service",), "仅维修人员可登记修复")
            order = self._open_order(conn, esc_id)
            if not order["arrived_at"]:
                raise E.InvalidTransition("维修尚未到场")
            conn.execute("UPDATE repair_orders SET fixed_at=?, fix_note=? WHERE id=?",
                         (self._ts(), note, order["id"]))
            self._event(conn, esc_id, "REPAIR_FIXED", staff_id,
                        order_id=order["id"], note=note)

    def rework(self, staff_id, esc_id, note=""):
        """复检退回后的返工：清除修复标记，等待再次修复与复检。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("service",), "仅维修人员可返工处理")
            order = self._open_order(conn, esc_id)
            conn.execute("UPDATE repair_orders SET fixed_at=NULL, fix_note=? WHERE id=?",
                         (f"复检退回返工：{note}", order["id"]))
            self._event(conn, esc_id, "REPAIR_REWORK", staff_id,
                        order_id=order["id"], note=note)

    # ------------------------------------------------------------- 复检
    def submit_recheck(self, staff_id, esc_id, note=""):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("inspector",), "仅巡检员可提交复检")
            order = self._open_order(conn, esc_id)
            if not order["fixed_at"]:
                raise E.InvalidTransition("维修未完成，不能提交复检")
            pending = conn.execute(
                "SELECT id FROM rechecks WHERE order_id=? AND result='pending'",
                (order["id"],)).fetchone()
            if pending:
                raise E.InvalidTransition("已有待批准的复检")
            cur = conn.execute(
                "INSERT INTO rechecks(esc_id, order_id, inspector_id, result,"
                " note, submitted_at) VALUES (?,?,?, 'pending',?,?)",
                (esc_id, order["id"], staff_id, note, self._ts()))
            self._event(conn, esc_id, "RECHECK_SUBMIT", staff_id,
                        recheck_id=cur.lastrowid, order_id=order["id"], note=note)
            return cur.lastrowid

    def decide_recheck(self, staff_id, recheck_id, approve: bool, note=""):
        """批准复检：批准人必须是巡检员，且不能是提交人本人。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("inspector",), "仅巡检员可审批复检")
            rc = conn.execute("SELECT * FROM rechecks WHERE id=?", (recheck_id,)).fetchone()
            if not rc:
                raise E.NotFound("复检单不存在")
            if rc["result"] != "pending":
                raise E.InvalidTransition("该复检已审批")
            # 职责分离：巡检员不能批准自己的复检
            if rc["inspector_id"] == staff_id:
                self._event(conn, rc["esc_id"], "RECHECK_SELF_APPROVE_BLOCKED",
                            staff_id, recheck_id=recheck_id)
                raise E.DutySeparation("巡检员不能批准自己提交的复检，请由另一名巡检员审批")
            result = "approved" if approve else "rejected"
            conn.execute(
                "UPDATE rechecks SET result=?, decided_at=?, decider_id=?,"
                " decide_note=? WHERE id=?",
                (result, self._ts(), staff_id, note, recheck_id))
            self._event(conn, rc["esc_id"],
                        "RECHECK_APPROVED" if approve else "RECHECK_REJECTED",
                        staff_id, recheck_id=recheck_id, note=note)

    # ------------------------------------------------------------- 复开
    def reopen(self, staff_id, esc_id):
        """复开确认：故障流需维修完成+复检批准+围挡撤除；客流流需客流回落+围挡撤除。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("duty",), "仅值班员可确认复开")
            esc = self._esc(conn, esc_id)
            if esc["status"] != "locked":
                raise E.InvalidTransition("仅封控中的扶梯需要复开")
            ld = self._active_lockdown(conn, esc_id)

            if esc["barrier"]:
                raise E.EarlyReopen("围挡尚未撤除，不能复开")
            if not ld["barrier_set_at"]:
                raise E.BarrierMissing("封控期间从未设置围挡，流程不合规")

            if ld["reason"] == "fault":
                order = conn.execute(
                    "SELECT * FROM repair_orders WHERE lockdown_id=? ORDER BY id DESC LIMIT 1",
                    (ld["id"],)).fetchone()
                if not order:
                    raise E.EarlyReopen("尚未报修，不能复开")
                if not order["arrived_at"]:
                    raise E.EarlyReopen("维修未到场，不能复开")
                if not order["fixed_at"]:
                    raise E.EarlyReopen("维修未完成，提前复开被禁止")
                rc = conn.execute(
                    "SELECT * FROM rechecks WHERE order_id=? ORDER BY id DESC LIMIT 1",
                    (order["id"],)).fetchone()
                if not rc or rc["result"] != "approved":
                    raise E.EarlyReopen("复检未批准，提前复开被禁止")
            else:
                crowd = self.crowd.readings(esc_id, self.clock())
                if crowd["density"] not in ("low", "normal"):
                    raise E.EarlyReopen("客流仍处高位，不能复开")

            conn.execute(
                "UPDATE lockdowns SET ended_at=?, reopen_staff_id=? WHERE id=?",
                (self._ts(), staff_id, ld["id"]))
            conn.execute(
                "UPDATE escalators SET status='stopped', updated_at=? WHERE id=?",
                (self._ts(), esc_id))
            self._event(conn, esc_id, "REOPEN", staff_id, lockdown_id=ld["id"])

    # ------------------------------------------------------------- 跳闸
    def handle_trip(self, esc_id, source="cabinet", code=None):
        """控制柜跳闸/急停触发。达到反复跳停阈值自动转故障封控并报修。"""
        with self._mem_lock, self.conn:
            conn = self.conn
            esc = self._esc(conn, esc_id)
            if esc["status"] != "running":
                return None
            self.cabinet.set_running(esc_id, False)
            # 窗口计数
            window_start = esc["trip_window_started_at"]
            count = esc["trips_today"]
            if not window_start or self.clock() - datetime.fromisoformat(window_start) >= TRIP_WINDOW:
                window_start = self._ts(); count = 0
            count += 1
            conn.execute(
                "UPDATE escalators SET status='stopped', trips_today=?,"
                " trip_window_started_at=?, updated_at=? WHERE id=?",
                (count, window_start, self._ts(), esc_id))
            self._event(conn, esc_id, "TRIP", None,
                        source=source, code=code, trip_count=count)
            if count >= REPEAT_TRIP_LIMIT and not self._active_lockdown(conn, esc_id):
                self._do_lock_down(conn, esc_id, "fault",
                                   f"{TRIP_WINDOW.total_seconds()/3600:.0f}小时内反复跳停{count}次",
                                   actor_id=None, auto_order=True)
                return "locked"
            return "stopped"

    # ------------------------------------------------------------- 绕行
    def publish_detour(self, staff_id, esc_id, message):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("cs",), "仅客服可发布客流绕行提示")
            if not message.strip():
                raise E.PlatformError("绕行提示内容不能为空")
            cur = conn.execute(
                "INSERT INTO detours(esc_id, message, published_by, published_at)"
                " VALUES (?,?,?,?)",
                (esc_id, message, staff_id, self._ts()))
            self._event(conn, esc_id, "DETOUR_PUBLISH", staff_id,
                        detour_id=cur.lastrowid, message=message)
            return cur.lastrowid

    def cancel_detour(self, staff_id, detour_id):
        with self._mem_lock, self.conn:
            conn = self.conn
            staff = self._staff(conn, staff_id)
            self._require_role(staff, ("cs",), "仅客服可撤销绕行提示")
            conn.execute("UPDATE detours SET cancelled_at=? WHERE id=? AND cancelled_at IS NULL",
                         (self._ts(), detour_id))
            self._event(conn,
                        conn.execute("SELECT esc_id FROM detours WHERE id=?",
                                     (detour_id,)).fetchone()["esc_id"],
                        "DETOUR_CANCEL", staff_id, detour_id=detour_id)

    # ------------------------------------------------------------- 封控计时/超时
    def check_repair_overdue(self):
        """调度周期调用：对超期工单发延期告警事件（节流：每整 15 分钟一条）。"""
        now = self.clock()
        fired = []
        with self._mem_lock, self.conn:
            conn = self.conn
            rows = conn.execute(
                "SELECT * FROM repair_orders WHERE fixed_at IS NULL").fetchall()
            for o in rows:
                due = datetime.fromisoformat(o["due_at"])
                if now <= due:
                    continue
                ticks = int((now - due).total_seconds() // (15 * 60)) + 1
                if ticks > o["overdue_alerts"]:
                    conn.execute(
                        "UPDATE repair_orders SET overdue_alerts=? WHERE id=?",
                        (ticks, o["id"]))
                    self._event(conn, o["esc_id"], "REPAIR_OVERDUE", None,
                                order_id=o["id"], overdue_min=int(
                                    (now - due).total_seconds() // 60))
                    fired.append(o["id"])
        return fired
