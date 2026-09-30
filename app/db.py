"""SQLite 持久化：检查记录与状态事件。

所有表只追加状态事件（run_events），当前状态冗余在 escalators 表上，
每次状态迁移由 PlatformService 在事务内更新并写一条事件。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS people(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  role TEXT NOT NULL                      -- DUTY 值班员 / INSPECTOR 巡检员 / REPAIR 维修员 / SUPERVISOR 主管 / SERVICE 客服
);

CREATE TABLE IF NOT EXISTS escalators(
  id INTEGER PRIMARY KEY,
  code TEXT UNIQUE NOT NULL,
  name TEXT NOT NULL,
  floor_from INTEGER NOT NULL,
  floor_to INTEGER NOT NULL,
  zone TEXT NOT NULL,
  desired_direction TEXT NOT NULL,       -- UP / DOWN
  schedule_start TEXT NOT NULL,          -- HH:MM
  schedule_end TEXT NOT NULL,
  config_version INTEGER NOT NULL DEFAULT 1,
  run_state TEXT NOT NULL DEFAULT 'STOPPED',  -- STOPPED/STARTING/RUNNING/STOPPING/LOCKED
  actual_direction TEXT,
  lock_reason TEXT,
  trip_count INTEGER NOT NULL DEFAULT 0,      -- 24h 窗口内跳停次数
  trip_window_start TEXT,
  lock_holder TEXT,                          -- 启动互斥持有者（人员 code）
  lock_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS inspections(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  date TEXT NOT NULL,                    -- YYYY-MM-DD
  inspector_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'OPEN',   -- OPEN/PASS/FAIL
  created_at TEXT NOT NULL,
  completed_at TEXT,
  UNIQUE(escalator_id, date)
);

CREATE TABLE IF NOT EXISTS inspection_items(
  id INTEGER PRIMARY KEY,
  inspection_id INTEGER NOT NULL,
  item_key TEXT NOT NULL,               -- steps/comb/handrail/lighting/estop
  result TEXT,                          -- PASS/FAIL/NA
  remark TEXT,
  critical INTEGER NOT NULL DEFAULT 1,
  updated_at TEXT,
  UNIQUE(inspection_id, item_key)
);

CREATE TABLE IF NOT EXISTS emergency_tests(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  date TEXT NOT NULL,
  tester_id INTEGER NOT NULL,
  result TEXT NOT NULL,                 -- PASS/FAIL
  response_ms INTEGER,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS faults(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  reporter_id INTEGER,
  source TEXT NOT NULL,                 -- FAULT 设备故障 / INCIDENT 客流事件 / ESTOP 急停测试失败 / REPEAT 反复跳停
  reason TEXT NOT NULL,
  status TEXT NOT NULL,                 -- REPORTED/ASSIGNED/ENROUTE/ARRIVED/REPAIRING/REPAIRED/
                                        -- REINSPECT_REJECTED/APPROVED/CLOSED
  repair_person_id INTEGER,
  reject_count INTEGER NOT NULL DEFAULT 0,
  arrival_due_at TEXT,
  arrived_at TEXT,
  repair_due_at TEXT,
  repaired_at TEXT,
  sla_notified INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE IF NOT EXISTS repair_logs(
  id INTEGER PRIMARY KEY,
  fault_id INTEGER NOT NULL,
  person_id INTEGER,
  action TEXT NOT NULL,
  note TEXT,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS barricades(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  fault_id INTEGER,
  placed_by INTEGER,
  placed_at TEXT NOT NULL,
  expected_removal_at TEXT,
  removed_by INTEGER,
  removed_at TEXT,
  status TEXT NOT NULL DEFAULT 'UP'     -- UP / DOWN
);

CREATE TABLE IF NOT EXISTS reinspections(
  id INTEGER PRIMARY KEY,
  fault_id INTEGER NOT NULL,
  escalator_id INTEGER NOT NULL,
  inspector_id INTEGER NOT NULL,
  result TEXT NOT NULL,                 -- PASS / FAIL
  note TEXT,
  submitted_at TEXT NOT NULL,
  approver_id INTEGER,
  approval TEXT,                        -- PENDING/APPROVED/REJECTED
  reject_reason TEXT,
  decided_at TEXT
);

CREATE TABLE IF NOT EXISTS detours(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  floor INTEGER NOT NULL,
  message TEXT NOT NULL,
  published_by INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  retracted_by INTEGER,
  retracted_at TEXT,
  status TEXT NOT NULL DEFAULT 'ACTIVE' -- ACTIVE/RETRACTED
);

CREATE TABLE IF NOT EXISTS photos(
  id INTEGER PRIMARY KEY,
  fault_id INTEGER,
  relpath TEXT NOT NULL,
  original_name TEXT,
  uploaded_by INTEGER,
  created_at TEXT NOT NULL
);

-- 只追加的状态事件流
CREATE TABLE IF NOT EXISTS run_events(
  id INTEGER PRIMARY KEY,
  escalator_id INTEGER NOT NULL,
  type TEXT NOT NULL,
  actor_id INTEGER,
  from_version INTEGER,
  detail TEXT,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_esc ON run_events(escalator_id, id);
CREATE INDEX IF NOT EXISTS idx_faults_status ON faults(status);
"""


def connect(db_path: str | Path) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()
