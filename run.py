"""本地启动命令。

用法：
    python3 run.py                # 重置演示库（含样例场景）并启动 Web，默认 127.0.0.1:8000
    python3 run.py serve --port 8080
    python3 run.py seed           # 仅写入演示样例（正常开梯 / 反复跳停 / 复检退回 / 客流绕行）
    python3 run.py reset          # 仅清空为初始演示数据（全部停梯待检）

样例场景：
    E12-A  正常开梯：班前五项全部合格 → 值班员按 v1 启动上行（运行中）
    E23-A  反复跳停：运行中 24h 内跳闸 3 次 → 自动封控+报修 → 围挡 → 维修到场未修复（已超 SLA）
    E34-A  复检退回：故障封控 → 围挡 → 报修 → 到场 → 修复 → 复检被另一名巡检员退回，待返工
    E23-B  客流封控：critical 客流自动停梯 → 围挡 → 客服已发布绕行提示，等待客流回落
"""

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

from escalator_platform.demo_data import build_service
from escalator_platform.server import serve

DB_PATH = "data/platform.db"
PHOTO_DIR = "data/uploads"


def _all_pass():
    return {"steps": "pass", "comb": "pass", "handrail": "pass",
            "lighting": "pass", "estop": "pass"}


def seed_scenarios(svc):
    # 用一张 1x1 红点 PNG 占位故障照片，保证本地目录有样例文件
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108020000009077"
        "53de0000000c4944415408996360f8cf000003010100c0a5c726000000004945"
        "4e44ae426082")

    # ---------- 场景 1：E12-A 正常开梯 ----------
    svc.submit_precheck("I01", "E12-A", _all_pass())
    svc.start("D01", "E12-A", "up", 1)

    # ---------- 场景 2：E23-A 反复跳停 → 封控报修 → 到场（超 SLA） ----------
    svc.submit_precheck("I02", "E23-A", _all_pass())
    svc.start("D02", "E23-A", "up", 1)
    assert svc.handle_trip("E23-A", source="cabinet", code="E07_EMERGENCY_STOP") == "stopped"
    # 跳停后设备为 stopped：前两次由值班员复位重启，第三次直接判反复跳停
    svc.start("D02", "E23-A", "up", 1)
    assert svc.handle_trip("E23-A", source="cabinet", code="E12_OVERLOAD") == "stopped"
    svc.start("D02", "E23-A", "up", 1)
    third = svc.handle_trip("E23-A", source="cabinet", code="E12_OVERLOAD")
    assert third == "locked", third
    svc.set_barrier("D01", "E23-A", True)
    order = svc.list_escalators()
    oid = next(e["active_order"]["id"] for e in order if e["id"] == "E23-A")
    svc.arrive("M01", "E23-A")
    # 回填报修时刻与 SLA 截止到 65 分钟前，制造维修延期告警
    past = (svc.clock() - timedelta(minutes=65)).isoformat(timespec="seconds")
    with svc.conn:
        svc.conn.execute(
            "UPDATE repair_orders SET reported_at=?, due_at=? WHERE id=?",
            (past, (svc.clock() - timedelta(minutes=5)).isoformat(timespec="seconds"), oid))
    svc.check_repair_overdue()

    # ---------- 场景 3：E34-A 复检退回 ----------
    photo = svc.save_photo("fault_combo.jpg", png)
    svc.lock_down("E34-A", "fault", "梳齿板卡入异物，梯级异响", "D01")
    svc.set_barrier("I01", "E34-A", True)
    svc.report_repair("I01", "E34-A", "梳齿板卡异物，已急停", photo)
    svc.arrive("M01", "E34-A")
    svc.mark_fixed("M01", "E34-A", "清理异物并复位")
    rid = svc.submit_recheck("I01", "E34-A", "梯级/梳齿/急停初检")
    svc.decide_recheck("I02", rid, False, "复检仍有异响，退回返工")

    # ---------- 场景 4：E23-B 客流封控 + 客服绕行 ----------
    svc.submit_precheck("I02", "E23-B", _all_pass())
    svc.start("D01", "E23-B", "down", 1)
    svc.crowd.inject("E23-B", "critical",
                     svc.clock() + timedelta(hours=2))
    svc.lock_down("E23-B", "crowd", "出入口客流 critical，自动限流", "SYS")
    svc.set_barrier("D02", "E23-B", True)
    svc.publish_detour("C01", "E23-B",
                       "E23-B 临时停梯限流，请前往中庭东梯或一层直梯绕行")

    print("演示样例已写入：")
    print("  E12-A 正常运行中；E23-A 反复跳停封控（维修到场、已超 SLA）；")
    print("  E34-A 复检退回待返工；E23-B 客流封控并已发布绕行。")


def main(argv=None):
    ap = argparse.ArgumentParser(description="扶梯启停巡检与封控复开平台")
    ap.add_argument("cmd", nargs="?", default="serve",
                    choices=["serve", "seed", "reset"])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--no-scheduler", action="store_true")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args(argv)

    Path("data").mkdir(exist_ok=True)
    svc = build_service(args.db, PHOTO_DIR, reset=True)
    if args.cmd in ("serve", "seed"):
        seed_scenarios(svc)
    else:
        print("已重置为初始演示数据：6 台扶梯全部停梯待检。")
    if args.cmd == "serve":
        serve(svc, args.host, args.port, with_scheduler=not args.no_scheduler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
