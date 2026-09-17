"""受信任代码的驱动、策略注册表；配置只能选择已注册的名字。

注册表不从 JSON 加载 Python 模块或执行表达式。部署时由受审查的 Python
代码注册扩展，配置检查和工作台读取同一份元数据，避免 UI 宣称尚未实现的能力。
工厂按需导入，读取注册信息不会创建网络连接或加载 FMU。
"""
from copy import deepcopy


_ADAPTERS = {}
_POLICIES = {}


def register_adapter(name, factory, *, version, simulation, description, validator=None):
    """注册一个已实现的驱动，禁止静默覆盖；factory 接收 asset_id/profile/owner。

    驱动应实现现有 Adapter 合约。validator 只检查 Profile，不得访问硬件。
    此扩展点不绕过运行网关、控制权或逐型号验收。
    """
    if not isinstance(name, str) or not name or name in _ADAPTERS:
        raise ValueError("duplicate_or_invalid_adapter_registration")
    if not callable(factory) or not isinstance(version, str) or not version:
        raise ValueError("adapter_factory_and_version_required")
    _ADAPTERS[name] = {"factory": factory, "validator": validator, "metadata": {
        "id": name, "version": version, "simulation": bool(simulation),
        "description": description, "status": "implemented"}}


def register_policy(name, factory, *, version, description, required_observations,
                    optional_observations=None, observations_by_control=None,
                    supported_controls=None):
    """注册符合现有 Engine 策略生命周期的实现；目前内置策略只有 flow。

    策略需实现 observe/propose，并暴露 gain/gain_bounds/version/last_decision；
    这是当前版本的聚合流量策略接口，不表示已提供任意优化器的通用状态持久化。
    """
    if not isinstance(name, str) or not name or name in _POLICIES:
        raise ValueError("duplicate_or_invalid_policy_registration")
    if not callable(factory) or not isinstance(version, str) or not version:
        raise ValueError("policy_factory_and_version_required")
    _POLICIES[name] = {"factory": factory, "metadata": {
        "id": name, "version": version, "description": description,
        "status": "implemented", "required_observations": dict(required_observations),
        "optional_observations": dict(optional_observations or {}),
        "observations_by_control": deepcopy(observations_by_control or {}),
        "supported_controls": list(supported_controls or []),
        "interface": "aggregate_flow_policy_v1"}}


def adapter_metadata(name):
    """返回副本，防止界面意外改写运行注册表。"""
    if not isinstance(name, str) or name not in _ADAPTERS:
        raise ValueError("adapter_not_implemented:" + str(name))
    return deepcopy(_ADAPTERS[name]["metadata"])


def policy_metadata(config):
    """旧 schema 0.1 未写 kind 时，仍使用原来的 FlowPolicy。"""
    if not isinstance(config, dict):
        raise ValueError("policy_mapping_required")
    name = config.get("kind", "flow")
    if not isinstance(name, str) or name not in _POLICIES:
        raise ValueError("policy_not_implemented:" + str(name))
    return deepcopy(_POLICIES[name]["metadata"])


def create_adapter(asset_id, profile, owner, **context):
    """只在显式运行入口调用；inspect_scene 不调用此函数。"""
    adapter_metadata(profile.get("adapter"))
    return _ADAPTERS[profile["adapter"]]["factory"](asset_id, profile, owner, **context)


def observation_bindings(profile):
    """按真实驱动的数据来源读取声明，禁止硬件遗留的仿真字段补齐缺测。

    initial_observations 只有仿真驱动使用。Modbus 实际从 points.observations
    读取，能力检查和测点归属检查必须查看同一组字段，否则会产生假可用能力。
    """
    simulated = adapter_metadata(profile.get("adapter"))["simulation"]
    points = profile.get("points", {})
    data = profile.get("initial_observations", {}) if simulated else (
        points.get("observations", {}) if isinstance(points, dict) else {})
    return data if isinstance(data, dict) else {}


def validate_adapter_profile(profile):
    """静态校验扩展驱动的字段，既不实例化驱动，也不做设备发现。"""
    adapter_metadata(profile.get("adapter"))
    validator = _ADAPTERS[profile["adapter"]]["validator"]
    if validator:
        validator(profile)


def create_policy(config):
    """仅创建本地算法对象；策略参数的工程校验由实现负责。"""
    metadata = policy_metadata(config)
    return _POLICIES[metadata["id"]]["factory"](config)


def registry_report():
    """可序列化的能力目录。models 是已有模型说明，尚不是任意模型执行器。"""
    return {"version": "1", "adapters": [deepcopy(v["metadata"]) for v in _ADAPTERS.values()],
            "policies": [deepcopy(v["metadata"]) for v in _POLICIES.values()],
            "models": [
                {"id": "aggregate_hydraulic_power_law", "version": "1", "status": "implemented",
                 "scope": "single_cdu_aggregate", "description": "流量≈增益×压差^指数；不是管网求解器"},
                {"id": "first_order_return_temperature", "version": "1", "status": "implemented",
                 "scope": "cdu_return_fluid", "description": "回液温度一阶预测；不是节点结温模型"},
                {"id": "synthetic_thermal_plant", "version": "1", "status": "implemented",
                 "scope": "single_cdu_aggregate", "description": "合成泵、供回温动态；无机柜支路状态"},
                {"id": "sustain_fmu", "version": "1", "status": "special_experiment_only",
                 "scope": "fixed_fmu_topology", "description": "须使用统一 FMU Master 的专用实验入口"}]}


def _mock(asset_id, profile, owner, **context):
    from .adapters import MockCDUAdapter
    return MockCDUAdapter(asset_id, profile, owner)


def _thermal(asset_id, profile, owner, **context):
    from .plant import ThermalPlant
    return ThermalPlant(asset_id, profile, owner)


def _thermal_validate(profile):
    """合成对象也要验证可执行条件，不能发布零时间常数等必然失败的模型。"""
    from .configuration import finite
    model = profile.get("simulation")
    if not isinstance(model, dict):
        raise ValueError("thermal_sim_parameters_required")
    positive = ("true_flow_per_sqrt_kpa", "cp_j_kg_k", "density_kg_m3", "pump_tau_s",
                "supply_tau_s", "return_tau_s", "local_fallback_flow_kg_s")
    for name in positive:
        if not finite(model.get(name)) or model[name] <= 0:
            raise ValueError("invalid_thermal_sim_parameter:" + name)
    if not finite(model.get("pump_efficiency")) or not 0 < model["pump_efficiency"] <= 1:
        raise ValueError("invalid_thermal_sim_parameter:pump_efficiency")
    if not finite(model.get("pump_idle_w")) or model["pump_idle_w"] < 0:
        raise ValueError("invalid_thermal_sim_parameter:pump_idle_w")
    setpoints = profile.get("initial_setpoints", {})
    if "cdu.sec_supply_temp_sp" not in setpoints:
        raise ValueError("thermal_sim_requires_supply_temperature_setpoint")
    observations = profile.get("initial_observations", {})
    for name, unit in (("cdu.sec_flow", "kg/s"), ("cdu.sec_supply_temp", "K"), ("cdu.sec_return_temp", "K")):
        point = observations.get(name, {})
        if not isinstance(point, dict) or point.get("unit") != unit or not finite(point.get("value")):
            raise ValueError("thermal_sim_initial_state_required:" + name)
    if observations["cdu.sec_flow"]["value"] <= 0:
        raise ValueError("thermal_sim_initial_flow_must_be_positive")


def _modbus(asset_id, profile, owner, **context):
    from .modbus import ModbusCDUAdapter
    return ModbusCDUAdapter(asset_id, profile, owner)


def _modbus_validate(profile):
    from .modbus import validate_profile
    validate_profile(profile)


def _fmu(asset_id, profile, owner, **context):
    # 同一 FMU 只能由一个 Master 推进。普通运行入口不能自行复制实例冒充共享物理系统。
    if "master" not in context or "scene" not in context:
        raise ValueError("sustain_fmu_requires_experiment_master_and_scene")
    from .sustain_fmu import SustainFmuAdapter
    return SustainFmuAdapter(context["master"], context["scene"])


def _flow(config):
    from .policy import FlowPolicy
    return FlowPolicy(config)


register_adapter("mock", _mock, version="1", simulation=True, description="内存协议替身，无热动态")
register_adapter("thermal_sim", _thermal, version="1", simulation=True,
                 description="单 CDU 聚合合成热工仿真", validator=_thermal_validate)
register_adapter("modbus_tcp", _modbus, version="1", simulation=False,
                 description="真实 Modbus TCP 驱动；点表与控制权限须现场验收", validator=_modbus_validate)
register_adapter("sustain_fmu", _fmu, version="1", simulation=True,
                 description="固定拓扑 Sustain FMU，仅专用实验入口")
register_policy("flow", _flow, version="1", description="有界流量需求／压差控制与聚合增益校准",
                required_observations={"cdu.sec_supply_temp": "K", "cdu.sec_return_temp": "K",
                                       "cdu.sec_flow": "kg/s"},
                optional_observations={"cdu.liquid_load": "W_th"},
                observations_by_control={"cdu.dp_sp": {"cdu.sec_dp": "kPa"}},
                supported_controls=["cdu.dp_sp", "cdu.sec_flow_sp"])
