"""设备配置与最小拓扑校验。

本文件只检查资产引用、热汇类型、控制能力和工程字段，不求解管网。
配置必须先通过 load_scene，再创建设备驱动；未知厂商字段可放 extensions，
但扩展字段不会自动影响控制。数字禁止 NaN/Inf 和把布尔值当作 0/1。"""
import json
import math
from pathlib import Path


CONTROL_UNITS = {
    "cdu.sec_supply_temp_sp": "degC",
    "cdu.dp_sp": "kPa",
    "cdu.sec_flow_sp": "kg/s",
    "cdu.pump_speed_rel": "1",
}


def finite(value):
    """判断是否为有限数值。明确排除 bool，防止配置把 true 当作工程数值 1。"""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_scene(scene):
    """校验一个已解析场景字典并返回原对象。
    配置错误抛出 ValueError；不访问网络、不写设备。允许 Modbus 只读配置 controls 为空。"""
    def require(condition, reason):
        if not condition:
            raise ValueError(reason)

    require(scene.get("schema_version") == "0.1", "unsupported_schema_version")
    require(bool(scene.get("topology_version")), "topology_version_required")
    assets = scene.get("assets", [])
    ids = [a["id"] for a in assets]
    require(len(ids) == len(set(ids)), "duplicate_asset_id")
    by_id = {a["id"]: a for a in assets}
    cdu_ids = {a["id"] for a in assets if a["kind"] == "cdu"}
    require(bool(cdu_ids), "cdu_required")
    require(set(scene.get("devices", {})) == cdu_ids, "device_asset_mismatch")
    domains = scene.get("control_domains", [])
    require(len({d["id"] for d in domains}) == len(domains), "duplicate_domain_id")
    assigned = [d["cdu_id"] for d in domains]
    require(len(assigned) == len(set(assigned)) and set(assigned) == cdu_ids,
            "one_domain_per_cdu_required")
    for domain in domains:
        require(bool(domain.get("owner")), "owner_required")
        require(type(domain.get("shared_hydraulics")) is bool, "shared_hydraulics_unknown")
        require(domain.get("heat_sink_id") in by_id, "unknown_heat_sink")
        require(by_id[domain["heat_sink_id"]]["kind"] in ("facility_water", "room_air"),
                "invalid_heat_sink_type")
        device = scene["devices"][domain["cdu_id"]]
        expected_sink = {"liquid_to_liquid": "facility_water", "liquid_to_air": "room_air"}
        require(device.get("type") in expected_sink, "unsupported_device_type")
        require(by_id[domain["heat_sink_id"]]["kind"] == expected_sink[device["type"]],
                "heat_path_mismatch")
        racks = domain.get("served_racks", [])
        require(len(racks) == len(set(racks)), "duplicate_served_rack")
        require(all(r in by_id and by_id[r]["kind"] == "rack" for r in racks), "unknown_rack")
        # Shared hydraulic control needs a coordinator beyond this single-domain skeleton.
    for asset_id, device in scene["devices"].items():
        require(device.get("adapter") in ("mock", "thermal_sim", "modbus_tcp", "sustain_fmu"), "adapter_not_implemented")
        simulated = device["adapter"] in ("mock", "thermal_sim", "sustain_fmu")
        if simulated:
            require(device.get("evidence") == "illustrative_not_oem", "mock_evidence_required")
        else:
            from .modbus import validate_profile
            validate_profile(device)
        require(isinstance(device.get("controls"), dict), "controls_mapping_required")
        if simulated:
            require(bool(device["controls"]), "controls_required")
        require(finite(device.get("max_data_age_s")) and device["max_data_age_s"] > 0,
                "invalid_data_age")
        for quantity, capability in device["controls"].items():
            require(CONTROL_UNITS.get(quantity) == capability.get("unit"), "control_unit_mismatch")
            lo, hi = capability["minimum"], capability["maximum"]
            require(finite(lo) and finite(hi) and lo < hi, "invalid_control_bounds")
            require(finite(capability["max_step"]) and capability["max_step"] > 0, "invalid_max_step")
            interval = capability["minimum_interval_s"]
            require(finite(interval) and interval > 0, "invalid_control_interval")
            require(bool(capability["allowed_modes"]), "control_modes_required")
            if simulated:
                initial = device["initial_setpoints"].get(quantity)
                require(finite(initial) and lo <= initial <= hi, "invalid_initial_setpoint")
        if simulated:
            require(set(device["initial_setpoints"]) == set(device["controls"]), "setpoint_keys_mismatch")
        require(bool(device.get("guards")), "guards_required")
        for point, guard in device["guards"].items():
            sample = (device["initial_observations"] if simulated else device["points"]["observations"]).get(point)
            require(sample is not None and sample["unit"] == guard["unit"], "guard_point_or_unit_missing")
            if simulated:
                require(finite(sample["value"]), "nonfinite_observation")
            require(finite(guard["minimum"]) and finite(guard["maximum"])
                    and guard["minimum"] < guard["maximum"], "invalid_guard_bounds")
        if "policy" in device:
            from .policy import FlowPolicy
            FlowPolicy(device["policy"])
            require(device["policy"]["control_quantity"] in device["controls"], "policy_control_not_advertised")
    return scene


def load_scene(path):
    """读取 UTF-8 JSON 并执行语义校验；路径错误、JSON 错误不自动使用默认配置。"""
    return validate_scene(json.loads(Path(path).read_text(encoding="utf-8")))


def capability_report(scene):
    """生成静态能力概览。能力声明不代表现场验收，更不代表机柜安全已得到保障。"""
    reports = []
    for domain in scene["control_domains"]:
        device = scene["devices"][domain["cdu_id"]]
        reports.append({
            "domain_id": domain["id"],
            "cdu_id": domain["cdu_id"],
            "served_racks": domain["served_racks"],
            "control": "monitor_only" if not device["controls"] else "unavailable" if domain["shared_hydraulics"] else (
                "requires_commissioning" if device["adapter"] == "modbus_tcp" else "simulation_only"),
            "control_reason": "no_controls_declared" if not device["controls"] else "coordinator_required" if domain["shared_hydraulics"] else device["adapter"],
            "advertised_controls": list(device["controls"]),
            "rack_thermal_assurance": "unavailable: per-rack observations/model not implemented",
            "energy_optimization": ("bounded flow/dp policy available; site savings unverified"
                                    if device.get("policy") and not domain["shared_hydraulics"]
                                    else "unavailable: no policy or shared-domain coordinator required"),
            "hydraulic_solver": "not_implemented",
            "coolant_health_prediction": "not_implemented",
            "topology_level": "service_and_dependency_relations",
        })
    return reports
