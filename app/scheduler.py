"""进程内任务：计划启停、围挡超时检查、维修超时检查、运行中控制柜/客流轮询。"""
from __future__ import annotations

import threading
import time
from datetime import datetime

from .domain import Clock, ST_RUNNING


class Scheduler(threading.Thread):
    """守护线程，默认每 15 秒一个节拍。

    - 每分钟按 HH:MM 触发计划开/关梯（每天每台只触发一次）；
    - 每拍检查围挡宽限期与维修 SLA；
    - 每拍轮询运行中扶梯的控制柜与客流读数。
    """

    def __init__(self, service, clock: Clock | None = None, interval: float = 15.0):
        super().__init__(daemon=True, name="escalator-scheduler")
        self.service = service
        self.clock = clock or Clock()
        self.interval = interval
        self._stop = threading.Event()
        # (esc_code, date, kind) 去重，保证计划动作每个时段只触发一次
        self._fired: set[tuple] = set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 调度异常不能杀死线程
                print(f"[scheduler] 节拍异常：{exc}")

    def tick(self, now: datetime | None = None) -> dict:
        now = now or self.clock.now()
        fired = []
        hm = now.strftime("%H:%M")
        date = now.strftime("%Y-%m-%d")
        for e in self.service.list_escalators():
            if hm == e["schedule_start"] and (e["code"], date, "START") not in self._fired:
                self.service.scheduled_start(e["code"])
                self._fired.add((e["code"], date, "START"))
                fired.append((e["code"], "START"))
            if hm == e["schedule_end"] and (e["code"], date, "STOP") not in self._fired:
                self.service.scheduled_stop(e["code"])
                self._fired.add((e["code"], date, "STOP"))
                fired.append((e["code"], "STOP"))
            if e["run_state"] == ST_RUNNING:
                self.service.poll_running(e["code"])
        missing = self.service.check_barricade_sla()
        overdue = self.service.check_repair_sla()
        return {"fired": fired, "barricade_missing": missing, "repair_overdue": overdue}
