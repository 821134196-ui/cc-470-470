"""SQLite 持久化：设备/人员/检查/封控/维修/复检/事件。

所有检查与状态变更都写事件表 escalator_events，作为事件时序查询的唯一事实来源。
"""

import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS staff (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('duty','inspector','service','cs'))
);

CREATE TABLE IF NOT EXISTS escalators (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    floor_from INTEGER NOT NULL,
    floor_to INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'stopped'
        CHECK(status IN ('stopped','running','locked')),
    direction TEXT NOT NULL DEFAULT 'up'
        CHECK(direction IN ('up','down')),
    direction_version INTEGER NOT NULL DEFAULT 1,
    schedule_enabled INTEGER NOT NULL DEFAULT 1,
    on_time TEXT NOT NULL DEFAULT '08:00',
    off_time TEXT NOT NULL DEFAULT '22:00',
    start_lock_owner TEXT,
    start_lock_at TEXT,
    barrier INTEGER NOT NULL DEFAULT 0,
    trips_today INTEGER NOT NULL DEFAULT 0,
    trip_window_started_at TEXT,
    updated_at TEXT NOT NULL
);

-- 每日班前检查（含急停测试，estop_result 单列以便独立判定）
CREATE TABLE IF NOT EXISTS prechecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL REFERENCES escalators(id),
    check_date TEXT NOT NULL,
    inspector_id TEXT NOT NULL REFERENCES staff(id),
    steps_result TEXT CHECK(steps_result IN ('pass','fail','na')),
    comb_result TEXT CHECK(comb_result IN ('pass','fail','na')),
    handrail_result TEXT CHECK(handrail_result IN ('pass','fail','na')),
    lighting_result TEXT CHECK(lighting_result IN ('pass','fail','na')),
    estop_result TEXT CHECK(estop_result IN ('pass','fail')),
    completed_at TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(esc_id, check_date)
);

-- 封控单：故障/客流事件触发生成
CREATE TABLE IF NOT EXISTS lockdowns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL REFERENCES escalators(id),
    reason TEXT NOT NULL CHECK(reason IN ('fault','crowd')),
    detail TEXT DEFAULT '',
    started_at TEXT NOT NULL,
    barrier_set_at TEXT,
    ended_at TEXT,
    reopen_staff_id TEXT REFERENCES staff(id)
);

-- 故障报修与维修进度
CREATE TABLE IF NOT EXISTS repair_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL REFERENCES escalators(id),
    lockdown_id INTEGER REFERENCES lockdowns(id),
    description TEXT NOT NULL,
    photo_path TEXT,
    reporter_id TEXT NOT NULL REFERENCES staff(id),
    reported_at TEXT NOT NULL,
    assignee_id TEXT REFERENCES staff(id),
    due_at TEXT NOT NULL,
    arrived_at TEXT,
    fixed_at TEXT,
    fix_note TEXT DEFAULT '',
    overdue_alerts INTEGER NOT NULL DEFAULT 0
);

-- 复检：提交人(巡检员) 与 批准人必须不同
CREATE TABLE IF NOT EXISTS rechecks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL REFERENCES escalators(id),
    order_id INTEGER REFERENCES repair_orders(id),
    inspector_id TEXT NOT NULL REFERENCES staff(id),
    result TEXT NOT NULL CHECK(result IN ('pending','approved','rejected')),
    note TEXT DEFAULT '',
    submitted_at TEXT NOT NULL,
    decided_at TEXT,
    decider_id TEXT REFERENCES staff(id),
    decide_note TEXT DEFAULT ''
);

-- 客流绕行提示（仅客服可发布/撤销）
CREATE TABLE IF NOT EXISTS detours (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL REFERENCES escalators(id),
    message TEXT NOT NULL,
    published_by TEXT NOT NULL REFERENCES staff(id),
    published_at TEXT NOT NULL,
    cancelled_at TEXT
);

-- 事件流：状态机所有动作落此表
CREATE TABLE IF NOT EXISTS escalator_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    esc_id TEXT NOT NULL,
    at TEXT NOT NULL,
    type TEXT NOT NULL,
    actor_id TEXT,
    payload TEXT DEFAULT '{}',
    seq_tail TEXT DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_events_esc_time
    ON escalator_events(esc_id, id);
CREATE INDEX IF NOT EXISTS idx_precheck_date ON prechecks(check_date);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


@contextmanager
def tx(conn: sqlite3.Connection):
    """串行化关键段：SQLite 写锁 + 显式 BEGIN IMMEDIATE 保证启动互斥可测。"""
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()
