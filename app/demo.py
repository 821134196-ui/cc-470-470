"""本地演示命令：正常开梯、跳停封控复开、复检退回三个样例。

用法：
    python -m app.demo              # 依次演示三个样例
    python -m app.demo normal       # 仅正常开梯（含两名值班员启动互斥）
    python -m app.demo trip         # 仅跳停 -> 封控 -> 维修复检 -> 复开
    python -m app.demo reject       # 仅复检退回（含自批拦截、提前复开拦截）
"""
from __future__ import annotations

import sys

from .db import connect, init_db
from .domain import (ControlCabinetAdapter, CrowdAdapter, Clock, PASS, FAIL,
                     CHECK_ITEMS)
from .service import PlatformService, BusinessError
from .seed import seed

DEMO_DB = "data/demo.db"
PHOTO_DIR = "data/photos"


def fresh_service() -> PlatformService:
    import os
    if os.path.exists(DEMO_DB):
        for suffix in ("", "-wal", "-shm"):
            p = DEMO_DB + suffix
            if os.path.exists(p):
                os.remove(p)
    cabinet, crowd = ControlCabinetAdapter(), CrowdAdapter()
    seed(DEMO_DB, cabinet, crowd, verbose=False)
    conn = connect(DEMO_DB)
    init_db(conn)
    return PlatformService(conn, cabinet, crowd, Clock(), photo_dir=PHOTO_DIR)


def title(t: str) -> None:
    print("\n" + "=" * 68)
    print(f"  {t}")
    print("=" * 68)


def fail_points(svc: PlatformService, esc: str, inspector: str,
                results: dict | None = None) -> int:
    """完成当日班前检查，默认五项全合格。"""
    ins_id = svc.open_or_get_inspection(esc, inspector)
    for key, label, _ in CHECK_ITEMS:
        r = (results or {}).get(key, PASS)
        svc.record_check_item(ins_id, key, r, inspector)
    return ins_id


def timeline(svc: PlatformService, esc: str) -> None:
    for ev in reversed(svc.events(esc, limit=40)):
        print(f"  [{ev['created_at'][11:]}] {ev['type']:<22} "
              f"{(ev['actor_name'] or '系统'):<6} {ev['detail'] or ''}")


# ---------------- 样例 1：正常开梯（含启动互斥、版本校验） ----------------
def scenario_normal(svc: PlatformService) -> None:
    title("样例 1：正常开梯 —— 班前五项全合格 + 急停测试通过 + 启动互斥")
    esc = "ESC-101"
    v = svc._esc(esc)["config_version"]
    print(f"当前 {esc} 配置版本 v{v}")

    fail_points(svc, esc, "I01")
    print("巡检员周敏完成班前检查：梯级/梳齿板/扶手带/照明/急停装置 全部合格")
    svc.submit_emergency_test(esc, "I01", PASS, response_ms=320)
    print("急停装置测试通过（320ms）")

    # 两名值班员不能同时启动同一扶梯
    svc.acquire_start(esc, "D01", v)
    print("值班员王建国占用启动权 …")
    try:
        svc.acquire_start(esc, "D02", v)
    except BusinessError as e:
        print(f"值班员李雪同时启动被拒：[{e.code}] {e.message}")

    # 过期版本命令被拒
    try:
        svc.start(esc, "D01", v - 1)
    except BusinessError as e:
        print(f"旧版本启动命令被拒：[{e.code}] {e.message}")

    r = svc.start(esc, "D01", v)
    print(f"按当前版本 v{v} 下发启动：running={r['running']}")

    # 运行中改方向被拒
    try:
        svc.set_direction(esc, "DOWN", "D01")
    except BusinessError as e:
        print(f"运行中改方向被拒：[{e.code}] {e.message}")

    timeline(svc, esc)


# ---------------- 样例 2：跳停 -> 封控 -> 维修 -> 复检 -> 复开 ----------------
def scenario_trip(svc: PlatformService) -> None:
    title("样例 2：启动后跳停 —— 停梯封控、围挡、维修到场、复检复开全链路")
    esc = "ESC-201"
    v = svc._esc(esc)["config_version"]
    fail_points(svc, esc, "I01")
    svc.submit_emergency_test(esc, "I01", PASS, 410)

    svc.cabinet.arm_trip(esc)
    print("控制柜注入：下一次启动将触发驱动链保护跳停")
    r = svc.start(esc, "D01", v)
    print(f"启动结果：tripped={r['tripped']}，自动进入封控")

    fault = svc.detail(esc)["active_fault"]
    fid = fault["id"]
    print(f"自动生成报修工单 #{fid}：{fault['reason']}")

    # 围挡缺失独立告警（演示：先让调度检查一次，此时尚未设围挡但宽限 10 分钟内，无告警）
    svc.check_barricade_sla()
    svc.place_barricade(fid, "D01")
    print("值班员王建国设置围挡（斜纹边界），封控完成")

    svc.dispatch(fid, "R01", "M01")
    svc.enroute(fid, "R01")
    svc.arrive(fid, "R01")
    print("主管派单 -> 维修员赵大力出发 -> 到场（30 分钟 SLA 内）")
    svc.begin_repair(fid, "R01", note="调整驱动链张紧装置并复位保护开关")
    svc.complete_repair(fid, "R01", note="试运转正常")
    print("维修完成，工单等待复检（围挡保持）")

    rid = svc.submit_reinspection(fid, "I02", PASS, note="梯级运行平稳，保护装置复位有效")
    print("巡检员陈工提交复检：合格，提交主管审批")
    try:
        # 巡检员不能批准自己的复检
        svc.approve_reinspection(rid, "I02", True)
    except BusinessError as e:
        print(f"陈工尝试自批被拒：[{e.code}] {e.message}")
    svc.approve_reinspection(rid, "M01", True)
    print("主管钱主任审批通过")

    try:
        svc.remove_barricade_and_reopen(fid, "D01", confirmed=False)
    except BusinessError as e:
        print(f"未二次确认的复开被拒：[{e.code}] {e.message}")
    r = svc.remove_barricade_and_reopen(fid, "D01", confirmed=True)
    print(f"撤除围挡并复开确认：reopened={r['reopened']}")
    timeline(svc, esc)


# ---------------- 样例 3：复检退回 + 提前复开阻断 ----------------
def scenario_reject(svc: PlatformService) -> None:
    title("样例 3：客流事件封控 —— 复检退回、提前复开阻断、客服绕行")
    esc = "ESC-301"
    v = svc._esc(esc)["config_version"]
    fail_points(svc, esc, "I02")
    svc.submit_emergency_test(esc, "I02", PASS, 280)
    svc.start(esc, "D02", v)
    print("李雪完成开梯，扶梯正常运行")

    # 客服在未封控时不能发绕行
    try:
        svc.publish_detour(esc, "S01", "请绕行中庭扶梯")
    except BusinessError as e:
        print(f"封控前客服发绕行被拒：[{e.code}] {e.message}")

    fid = svc.seal(esc, "D02", "INCIDENT", "3F 餐饮散场客流倒灌，临时封控")
    print(f"客流事件触发停梯封控，工单 #{fid}")
    svc.place_barricade(fid, "D02")
    did = svc.publish_detour(esc, "S01", "3F 上行临时停用，请改乘中庭直达梯或 2F 绕行")
    print(f"客服吴晴发布绕行提示（#{did}），其他岗位无此权限")

    svc.dispatch(fid, "R02", "M01")
    svc.enroute(fid, "R02")
    svc.arrive(fid, "R02")
    svc.begin_repair(fid, "R02", note="检查客流关联的急停回路与梳齿板")
    svc.complete_repair(fid, "R02", note="复位完成")

    # 维修未复检就想复开 -> 提前复开阻断
    try:
        svc.remove_barricade_and_reopen(fid, "D02", confirmed=True)
    except BusinessError as e:
        print(f"复检完成前复开被拒：[{e.code}] {e.message}")

    rid = svc.submit_reinspection(fid, "I01", PASS, note="外观正常")
    svc.approve_reinspection(rid, "M01", False, reason="梳齿板仍有卡阻感，需拆检清洁")
    print("主管钱主任退回复检：梳齿板卡阻，退回重新维修")

    # 退回后仍不能复开
    try:
        svc.remove_barricade_and_reopen(fid, "D02", confirmed=True)
    except BusinessError as e:
        print(f"复检退回后复开被拒：[{e.code}] {e.message}")

    svc.begin_repair(fid, "R02", note="拆检梳齿板，清除异物")
    svc.complete_repair(fid, "R02", note="梳齿板啮合正常")
    rid2 = svc.submit_reinspection(fid, "I01", PASS, note="梳齿板拆检合格，连续运行无异常")
    svc.approve_reinspection(rid2, "M01", True)
    print("重新维修后复检合格，主管审批通过")
    r = svc.remove_barricade_and_reopen(fid, "D02", confirmed=True)
    print(f"围挡撤除、复开确认：reopened={r['reopened']}")
    svc.retract_detour(did, "S01")
    print("客服撤回绕行提示")
    timeline(svc, esc)


def main() -> None:
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    if which in ("all", "normal"):
        scenario_normal(fresh_service())
    if which in ("all", "trip"):
        scenario_trip(fresh_service())
    if which in ("all", "reject"):
        scenario_reject(fresh_service())
    if which not in ("all", "normal", "trip", "reject"):
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
