"""产品内部的数据契约：统一物理语义，隔离厂商协议。

上层算法只认识 Reading / Snapshot / ControlRequest 等对象，不读取寄存器地址。
观测时间为秒；真实设备使用 Unix 时间，仿真使用严格递增的仿真时间。
所有对象采用 frozen dataclass，防止算法原地篡改已经采集或审计的数据。
注意：冻结只保护属性赋值，字典内容仍需调用者按只读约定使用。
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Protocol, Tuple


@dataclass(frozen=True)
class Reading:
    """单点读数。quality 非 good 不得参与控制；provenance 说明数据来源。"""
    value: float
    unit: str
    timestamp: float
    quality: str = "good"
    provenance: str = "simulated"


@dataclass(frozen=True)
class ControlCapability:
    """一个已开放控制量的工程边界；这些约束不能由在线学习自动放宽。"""
    unit: str
    minimum: float
    maximum: float
    max_step: float
    minimum_interval_s: float
    allowed_modes: Tuple[str, ...]
    evidence: str


@dataclass(frozen=True)
class DeviceCapabilities:
    """设备能力清单。controls 为空表示只读接入，不能推断存在可写控制量。"""
    asset_id: str
    device_type: str
    deployment: str
    controls: Dict[str, ControlCapability]


@dataclass(frozen=True)
class Snapshot:
    """一次采集结果。setpoints 是目标值回读，observations 是实际过程量。"""
    asset_id: str
    timestamp: float
    operating_mode: str
    owner: Optional[str]
    alarms: Tuple[str, ...]
    observations: Dict[str, Reading]
    setpoints: Dict[str, Reading]


@dataclass(frozen=True)
class ControlRequest:
    """带归属、拓扑版本、单位和截止时间的动作申请，尚不代表已执行。"""
    request_id: str
    asset_id: str
    topology_version: str
    owner: str
    quantity: str
    value: float
    unit: str
    issued_at: float
    expires_at: float


@dataclass(frozen=True)
class ControlReceipt:
    """网关处理结果。setpoint_confirmed 仅确认目标寄存器，不保证温度已达标。"""
    request_id: str
    status: str
    reasons: Tuple[str, ...]
    readback: Optional[float] = None
    process_response: str = "not_verified"
    provenance: str = "simulated"


class CDUAdapter(Protocol):
    """设备适配边界：描述、读取、写入、释放控制。

release_to_local 必须表示已验证的本地接管，不能绑定到远程停泵命令。
新增协议通过实现此契约复用控制核心；具体超时和握手由适配器负责。
"""

    def describe(self) -> DeviceCapabilities: ...
    def read(self, now: float) -> Snapshot: ...
    def write(self, request: ControlRequest) -> None: ...
    def release_to_local(self, owner: str) -> bool: ...


class ControlPolicy(Protocol):
    def propose(self, snapshot: Snapshot, context: dict) -> Optional[ControlRequest]: ...


class ThermalModel(Protocol):
    """未来可替换热模型的接口；当前 FlowPolicy 内部只实现简单一阶预测。"""
    def forecast(self, snapshot: Snapshot, disturbances: dict) -> dict: ...
    def calibration_candidate(self, history: list) -> dict: ...


@dataclass(frozen=True)
class CoolingDemand:
    """Future interface to a facility coordinator; no plant optimizer yet."""
    sink_id: str
    issued_at: float
    valid_until: float
    thermal_power_w: Tuple[float, ...]
    assumptions: Tuple[str, ...] = field(default_factory=tuple)


def describe_profile(asset_id, profile, deployment):
    """把经校验的设备配置转换为能力对象，供真实驱动和模拟器共用。

这里只构建能力元数据，不生成测量值，也不判断设备已经通过现场验收。
"""
    controls = {
        q: ControlCapability(unit=c["unit"], minimum=c["minimum"], maximum=c["maximum"],
                             max_step=c["max_step"], minimum_interval_s=c["minimum_interval_s"],
                             allowed_modes=tuple(c["allowed_modes"]), evidence=profile["evidence"])
        for q, c in profile["controls"].items()
    }
    return DeviceCapabilities(asset_id, profile["type"], deployment, controls)
