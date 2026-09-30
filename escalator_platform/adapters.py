"""模拟适配器：控制柜读数与客流传感器。

真实项目中这里走 PLC/网关协议；演示环境全部由内存脚本驱动，
可注入故障（故障照片、控制柜跳闸）与大客流读数。
"""

import threading
from datetime import datetime, timedelta


class ControlCabinetAdapter:
    """模拟扶梯控制柜：运行电流/转速/故障码/跳闸信号。"""

    def __init__(self):
        self._lock = threading.RLock()
        # esc_id -> {"running": bool, "fault_code": str|None, "trips": int}
        self._state: dict[str, dict] = {}
        # 计划好的跳闸：esc_id -> 计划时刻
        self._trips: dict[str, datetime] = {}

    def register(self, esc_id: str):
        with self._lock:
            self._state.setdefault(
                esc_id, {"running": False, "fault_code": None, "trips": 0}
            )

    def set_running(self, esc_id: str, running: bool):
        with self._lock:
            self.register(esc_id)
            self._state[esc_id]["running"] = running
            if running:
                self._state[esc_id]["fault_code"] = None

    def inject_fault(self, esc_id: str, code: str = "E12_OVERLOAD"):
        with self._lock:
            self._state[esc_id]["fault_code"] = code

    def schedule_trip(self, esc_id: str, at: datetime):
        """安排控制柜在指定时刻跳闸（进程内任务轮询时触发）。"""
        with self._lock:
            self._trips[esc_id] = at

    def poll(self, esc_id: str, now: datetime) -> str | None:
        """返回当前应触发的信号：'trip' / None。同时给出读数。"""
        with self._lock:
            st = self._state.get(esc_id)
            if st is None:
                return None
            at = self._trips.get(esc_id)
            if at and now >= at and st["running"]:
                st["running"] = False
                st["trips"] += 1
                st["fault_code"] = st.get("fault_code") or "E07_EMERGENCY_STOP"
                del self._trips[esc_id]
                return "trip"
            return None

    def readings(self, esc_id: str) -> dict:
        with self._lock:
            st = self._state.get(esc_id, {"running": False, "fault_code": None})
            return {
                "running": st["running"],
                "current_a": 11.8 if st["running"] else 0.0,
                "speed_mps": 0.5 if st["running"] else 0.0,
                "fault_code": st["fault_code"],
            }


class CrowdSensorAdapter:
    """模拟出入口客流摄像头：人数与密度等级。"""

    LEVELS = ["low", "normal", "high", "critical"]

    def __init__(self):
        self._lock = threading.RLock()
        # esc_id -> (level, until)；默认 normal
        self._overrides: dict[str, tuple[str, datetime]] = {}

    def inject(self, esc_id: str, level: str, until: datetime):
        with self._lock:
            self._overrides[esc_id] = (level, until)

    def readings(self, esc_id: str, now: datetime) -> dict:
        with self._lock:
            override = self._overrides.get(esc_id)
            if override and now < override[1]:
                level = override[0]
            elif override:
                del self._overrides[esc_id]
                level = "normal"
            else:
                level = "normal"
        base = {"low": 25, "normal": 60, "high": 130, "critical": 210}[level]
        return {"people_per_min": base, "density": level}
