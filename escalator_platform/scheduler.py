"""进程内调度任务：

- 按启停时段计划开梯/停梯（开梯仍需当日班前检查合格，否则记事件跳过）
- 轮询模拟控制柜跳闸信号
- 客流达到 critical 自动触发客流封控
- 维修 SLA 超期告警
"""

import threading
from datetime import datetime


class Scheduler(threading.Thread):
    def __init__(self, svc, interval_sec=10):
        super().__init__(daemon=True, name="esc-scheduler")
        self.svc = svc
        self.interval = interval_sec
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        while not self._stop.wait(self.interval):
            try:
                self.tick()
            except Exception as exc:  # 调度循环不能因单次异常退出
                print(f"[scheduler] tick error: {exc}")

    def tick(self, now: datetime | None = None):
        now = now or self.svc.clock()
        hhmm = now.strftime("%H:%M")
        for esc in self.svc.list_escalators():
            eid = esc["id"]
            # 1) 模拟控制柜信号
            signal = self.svc.cabinet.poll(eid, now)
            if signal == "trip" and esc["status"] == "running":
                self.svc.handle_trip(eid, source="cabinet_poll")

            # 2) 客流临界 -> 客流封控（仅运行中设备）
            crowd = self.svc.crowd.readings(eid, now)
            if crowd["density"] == "critical" and esc["status"] == "running":
                self.svc.lock_down(eid, "crowd", "客流达到 critical，自动停梯限流")

            # 3) 计划启停
            if not esc["schedule_enabled"]:
                continue
            if hhmm == esc["on_time"] and esc["status"] == "stopped" and \
                    not self._fired_today(eid, "SCHEDULED_START", now) and \
                    not self._fired_today(eid, "SCHEDULED_START_SKIPPED", now):
                pre = self.svc.today_precheck(eid, now.date().isoformat())
                if pre and pre["completed_at"]:
                    self.svc.start("SYS_SCHED", eid, esc["direction"],
                                   esc["direction_version"])
                    self._record(eid, "SCHEDULED_START",
                                 direction=esc["direction"])
                else:
                    self._record_skip(eid, "班前检查未完成，计划开梯跳过")
            elif hhmm == esc["off_time"] and esc["status"] == "running" and \
                    not self._fired_today(eid, "SCHEDULED_STOP", now):
                self.svc.stop("SYS_SCHED", eid, reason="scheduled_off")
                self._record(eid, "SCHEDULED_STOP")

        # 4) 维修延期
        self.svc.check_repair_overdue()

    def _record(self, eid, etype, **payload):
        with self.svc._mem_lock, self.svc.conn:
            self.svc._event(self.svc.conn, eid, etype, "SYS_SCHED", **payload)

    def _fired_today(self, eid, etype, now):
        today = now.date().isoformat()
        rows = self.svc.events(eid, etype, limit=10)
        return any(r["at"].startswith(today) for r in rows)

    def _record_skip(self, eid, detail):
        with self.svc._mem_lock, self.svc.conn:
            self.svc._event(self.svc.conn, eid, "SCHEDULED_START_SKIPPED",
                            "SYS_SCHED", detail=detail)
