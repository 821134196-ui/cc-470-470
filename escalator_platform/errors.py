"""领域错误。七类异常情形各自独立，便于前端按类型提示与处理。"""


class PlatformError(Exception):
    """所有可向用户展示的业务错误基类。"""

    code = "platform_error"
    http_status = 400

    def to_dict(self):
        return {"error": self.code, "message": str(self)}


class DirectionMismatch(PlatformError):
    """方向设置错误：启动命令方向与当前调度版本不一致。"""

    code = "direction_mismatch"


class InspectionIncomplete(PlatformError):
    """检查漏项：开梯前五项班前检查未全部合格（含漏填/失败/仍有不适用项）。"""

    code = "inspection_incomplete"


class EstopTestFailed(PlatformError):
    """急停测试失败：独立于普通检查失败处理。"""

    code = "estop_test_failed"


class BarrierMissing(PlatformError):
    """围挡缺失：停梯封控必须先设置围挡。"""

    code = "barrier_missing"


class RepeatedTrip(PlatformError):
    """反复跳停：短时间内跳停次数达到阈值，转为故障报修流程。"""

    code = "repeated_trip"


class RepairOverdue(PlatformError):
    """维修延期：超过 SLA 仍未到场/修复，仅记录告警（不改变设备主状态）。"""

    code = "repair_overdue"


class EarlyReopen(PlatformError):
    """提前复开：复检未通过/未批准、围挡未撤除或不在封控态时禁止复开。"""

    code = "early_reopen"


class ConflictError(PlatformError):
    """启动互斥：已有值班员持有该扶梯的启动流程。"""

    code = "start_conflict"
    http_status = 409


class StaleVersion(PlatformError):
    """乐观锁：启动命令必须按当前版本提交。"""

    code = "stale_version"
    http_status = 409


class DutySeparation(PlatformError):
    """职责分离：巡检员不能批准自己的复检；客服不能操作设备。"""

    code = "duty_separation"
    http_status = 403


class InvalidTransition(PlatformError):
    """设备当前状态不允许该操作（含封控阻断：封控中禁止启停/调度切换）。"""

    code = "invalid_transition"
    http_status = 409


class NotFound(PlatformError):
    code = "not_found"
    http_status = 404
