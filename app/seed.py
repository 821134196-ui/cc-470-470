"""演示数据：人员、楼层扶梯。"""
from __future__ import annotations

from .db import connect, init_db
from .domain import ControlCabinetAdapter, CrowdAdapter, UP, DOWN

PEOPLE = [
    # code, name, role
    ("D01", "王建国", "DUTY"),
    ("D02", "李雪", "DUTY"),
    ("I01", "周敏", "INSPECTOR"),
    ("I02", "陈工", "INSPECTOR"),
    ("R01", "赵大力", "REPAIR"),
    ("R02", "孙磊", "REPAIR"),
    ("M01", "钱主任", "SUPERVISOR"),
    ("S01", "吴晴", "SERVICE"),
]

# code, name, floor_from, floor_to, zone, direction, start, end
ESCALATORS = [
    ("ESC-101", "中庭东扶梯", 1, 2, "A区中庭", UP, "09:00", "22:00"),
    ("ESC-102", "中庭西扶梯", 2, 1, "A区中庭", DOWN, "09:00", "22:00"),
    ("ESC-201", "北区上行扶梯", 2, 3, "B区北街", UP, "09:30", "21:30"),
    ("ESC-202", "北区下行扶梯", 3, 2, "B区北街", DOWN, "09:30", "21:30"),
    ("ESC-301", "餐饮区上行扶梯", 3, 4, "C区餐饮", UP, "10:00", "22:30"),
    ("ESC-302", "餐饮区下行扶梯", 4, 3, "C区餐饮", DOWN, "10:00", "22:30"),
]


def seed(db_path: str, cabinet: ControlCabinetAdapter, crowd: CrowdAdapter,
         verbose: bool = True) -> None:
    conn = connect(db_path)
    init_db(conn)
    if conn.execute("SELECT COUNT(*) c FROM people").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO people(code,name,role) VALUES(?,?,?)", PEOPLE)
    if conn.execute("SELECT COUNT(*) c FROM escalators").fetchone()["c"] == 0:
        conn.executemany(
            "INSERT INTO escalators(code,name,floor_from,floor_to,zone,desired_direction,"
            " schedule_start,schedule_end) VALUES(?,?,?,?,?,?,?,?)", ESCALATORS)
        for row in ESCALATORS:
            cabinet.register(row[0])
            crowd.set_load(row[0], 0)
    else:
        for code, *_ in conn.execute("SELECT code FROM escalators"):
            cabinet.register(code)
    conn.commit()
    conn.close()
    if verbose:
        print(f"演示数据就绪：{len(PEOPLE)} 名人员，{len(ESCALATORS)} 部扶梯")
