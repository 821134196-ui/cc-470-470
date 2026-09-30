"""领域常量与模拟适配器（控制柜 / 客流读数）。"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta

# ---- 角色 ----
ROLE_DUTY = "DUTY"            # 值班员：开梯/停梯、确认围挡撤除与复开
ROLE_INSPECTOR = "INSPECTOR"  # 巡检员：班前检查、复检提交（不能批准自己的复检）
ROLE_REPAIR = "REPAIR"        # 维修员：接单、到场、修复
ROLE_SUPERVISOR = "SUPERVISOR"  # 主管：复检审批
ROLE_SERVICE = "SERVICE"      # 客服：仅能发布/撤回绕行信息

ROLE_NAMES = {
    ROLE_DUTY: "值班员",
    ROLE_INSPECTOR: "巡检员",
    ROLE_REPAIR: "维修员",
    ROLE_SUPERVISOR: "主管",
    ROLE_SERVICE: "客服",
}

UP = "UP"
DOWN = "DOWN"

# 扶梯运行状态
ST_STOPPED = "STOPPED"
ST_STARTING = "STARTING"
ST_RUNNING = "RUNNING"
ST_STOPPING = "STOPPING"
ST_LOCKED = "LOCKED"  # 封控停梯

# 班前检查五项，critical=1 为必须合格
CHECK_ITEMS = [
    ("steps", "梯级", 1),
    ("comb", "梳齿板", 1),
    ("handrail", "扶手带", 1),
    ("lighting", "照明", 1),
    ("estop", "急停装置", 1),
]
CHECK_ITEM_LABELS = {k: label for k, label, _ in CHECK_ITEMS}

PASS, FAIL, NA = "PASS", "FAIL", "NA"

# 封控触发源
SRC_FAULT = "FAULT"
SRC_INCIDENT = "INCIDENT"
SRC_ESTOP = "ESTOP"
SRC_REPEAT = "REPEAT"

# 故障工单状态
F_REPORTED = "REPORTED"
F_ASSIGNED = "ASSIGNED"
F_ENROUTE = "ENROUTE"
F_ARRIVED = "ARRIVED"
F_REPAIRING = "REPAIRING"
F_REPAIRED = "REPAIRED"
F_REJECTED = "REINSPECT_REJECTED"
F_APPROVED = "APPROVED"
F_CLOSED = "CLOSED"

ARRIVAL_SLA_MIN = 30      # 维修到场时限
REPAIR_SLA_MIN = 120      # 修复时限
TRIP_LIMIT = 3            # 24h 内跳停次数上限 -> 锁定大修
BARRICADE_GRACE_MIN = 10  # 封控后围挡必须就位的宽限期
REOPEN_SEPARATION = 0.5   # 复开按钮与普通操作的视觉间距（rem），仅用于文档说明


class Clock:
    """可替换的时钟，便于测试跳停窗口与超时。"""

    def now(self) -> datetime:
        return datetime.now()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ControlCabinetAdapter:
    """模拟扶梯控制柜：下发方向/启停，回读实际运行方向与故障码。

    真实系统这里是 PLC/物联网关；演示用内存状态 + 可注入故障。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # code -> dict(running, direction, fault_code)
        self._units: dict[str, dict] = {}
        self._trip_armed: set[str] = set()  # 下次启动后触发一次跳停

    def register(self, code: str) -> None:
        with self._lock:
            self._units.setdefault(code, {"running": False, "direction": None, "fault_code": None})

    def arm_trip(self, code: str) -> None:
        with self._lock:
            self._trip_armed.add(code)

    def inject_running_fault(self, code: str, fault_code: str = "E-44 主机超速保护") -> None:
        """模拟运行中控制柜上报故障码（下一次轮询即触发停梯封控）。"""
        with self._lock:
            self._units[code]["fault_code"] = fault_code

    def clear_fault(self, code: str) -> None:
        with self._lock:
            self._units[code]["fault_code"] = None

    def start(self, code: str, direction: str) -> dict:
        with self._lock:
            u = self._units[code]
            tripped = code in self._trip_armed
            self._trip_armed.discard(code)
            if tripped:
                # 控制柜拒绝保持运行：模拟启动后立刻跳停
                u["running"] = False
                u["direction"] = None
                u["fault_code"] = "E-31 驱动链保护动作"
                return {"running": False, "direction": None, "tripped": True,
                        "fault_code": u["fault_code"]}
            u["running"] = True
            u["direction"] = direction
            u["fault_code"] = None
            return {"running": True, "direction": direction, "tripped": False}

    def stop(self, code: str) -> dict:
        with self._lock:
            u = self._units[code]
            u["running"] = False
            u["direction"] = None
            return {"running": False, "direction": None}

    def read(self, code: str) -> dict:
        with self._lock:
            u = self._units[code]
            return dict(u)


class CrowdAdapter:
    """模拟客流摄像头读数（人/5分钟），并可注入客流拥堵事件。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._load: dict[str, int] = {}
        self._incident_armed: set[str] = set()

    def set_load(self, code: str, people: int) -> None:
        with self._lock:
            self._load[code] = people

    def arm_incident(self, code: str) -> None:
        with self._lock:
            self._incident_armed.add(code)

    def read(self, code: str) -> dict:
        """只读窥视，不消费已注入事件（页面刷新调用，不能吃掉调度器要处理的事件）。"""
        with self._lock:
            return {"people_per_5min": self._load.get(code, 0),
                    "incident": code in self._incident_armed}

    def consume_incident(self, code: str) -> bool:
        """取出并清除一次性客流事件标志（仅调度轮询调用）。"""
        with self._lock:
            had = code in self._incident_armed
            self._incident_armed.discard(code)
            return had


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")


def today(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d")


def minutes_between(a: str, b: str) -> float:
    return (parse_iso(b) - parse_iso(a)).total_seconds() / 60.0


def due_after(now: datetime, minutes: float) -> str:
    return iso(now + timedelta(minutes=minutes))
