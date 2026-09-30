"""平台启动入口：python -m app.main [--host 0.0.0.0] [--port 8000] [--reset]"""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from .db import connect, init_db
from .domain import ControlCabinetAdapter, CrowdAdapter, Clock
from .scheduler import Scheduler
from .seed import seed
from .server import AppContext, serve
from .service import PlatformService

DB_PATH = "data/platform.db"
PHOTO_DIR = Path("data/photos")


def build_context(reset: bool = False, start_scheduler: bool = True) -> AppContext:
    if reset and os.path.exists(DB_PATH):
        for suffix in ("", "-wal", "-shm"):
            p = DB_PATH + suffix
            if os.path.exists(p):
                os.remove(p)
    cabinet, crowd = ControlCabinetAdapter(), CrowdAdapter()
    seed(DB_PATH, cabinet, crowd)
    conn = connect(DB_PATH)
    init_db(conn)
    svc = PlatformService(conn, cabinet, crowd, Clock(), photo_dir=str(PHOTO_DIR))
    sched = Scheduler(svc)
    if start_scheduler:
        sched.start()
    return AppContext(svc, cabinet, crowd, sched, PHOTO_DIR)


def main() -> None:
    ap = argparse.ArgumentParser(description="商场扶梯启停巡检与封控复开平台")
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reset", action="store_true", help="清空历史数据重新开始")
    args = ap.parse_args()

    ctx = build_context(reset=args.reset)
    httpd = serve(args.host, args.port, ctx)
    print(f"扶梯平台已启动：http://localhost:{args.port}")
    print("演示账号：D01 王建国(值班员) D02 李雪(值班员) I01 周敏(巡检) "
          "I02 陈工(巡检) R01 赵大力(维修) M01 钱主任(主管) S01 吴晴(客服)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.scheduler.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
