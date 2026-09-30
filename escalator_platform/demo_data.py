"""演示数据：4 层商场 6 台扶梯、值班员/巡检员/维修工/客服各角色。"""

from datetime import datetime, timedelta

from .db import init_db, connect
from .adapters import ControlCabinetAdapter, CrowdSensorAdapter
from .service import PlatformService, REPAIR_SLA

STAFF = [
    ("D01", "王磊(值班员)", "duty"),
    ("D02", "赵敏(值班员)", "duty"),
    ("I01", "陈晨(巡检员)", "inspector"),
    ("I02", "孙宁(巡检员)", "inspector"),
    ("M01", "周工(维修)", "service"),
    ("C01", "林客服", "cs"),
    ("SYS", "系统", "duty"),
    ("SYS_SCHED", "计划任务", "duty"),
]

# id, 名称, 起始层, 到达层, 方向, 开/关梯时段
ESCALATORS = [
    ("E12-A", "中庭东梯 A", 1, 2, "up", "08:00", "22:00"),
    ("E12-B", "中庭西梯 B", 1, 2, "down", "08:00", "22:00"),
    ("E23-A", "中庭东梯 A", 2, 3, "up", "08:30", "21:30"),
    ("E23-B", "中庭西梯 B", 2, 3, "down", "08:30", "21:30"),
    ("E34-A", "餐饮街东梯", 3, 4, "up", "10:00", "22:30"),
    ("E34-B", "餐饮街西梯", 3, 4, "down", "10:00", "22:30"),
]


def build_service(db_path: str = "data/platform.db",
                  photo_dir: str = "data/uploads",
                  reset: bool = True, clock=None):
    conn = connect(db_path)
    if reset:
        conn.executescript("""
            DROP TABLE IF EXISTS escalator_events;
            DROP TABLE IF EXISTS detours;
            DROP TABLE IF EXISTS rechecks;
            DROP TABLE IF EXISTS repair_orders;
            DROP TABLE IF EXISTS lockdowns;
            DROP TABLE IF EXISTS prechecks;
            DROP TABLE IF EXISTS escalators;
            DROP TABLE IF EXISTS staff;
        """)
        conn.commit()
    init_db(conn)
    cabinet = ControlCabinetAdapter()
    crowd = CrowdSensorAdapter()
    svc = PlatformService(conn, cabinet, crowd, photo_dir, clock=clock or _real_now)
    seed(svc, conn)
    return svc


def _real_now():
    return datetime.now().replace(microsecond=0)


def seed(svc: PlatformService, conn):
    now = svc.clock()
    ts = now.isoformat(timespec="seconds")
    conn.executemany(
        "INSERT INTO staff(id,name,role) VALUES (?,?,?)", STAFF)
    conn.executemany(
        "INSERT INTO escalators(id,name,floor_from,floor_to,status,direction,"
        " direction_version,on_time,off_time,updated_at)"
        " VALUES (?,?,?,?,'stopped',?,1,?,?,?)",
        [(eid, name, f, t, d, on, off, ts)
         for eid, name, f, t, d, on, off in ESCALATORS])
    conn.commit()
    for eid, *_ in ESCALATORS:
        svc.cabinet.register(eid)
    return svc
