"""站点的静态兼容性检查与可视化投影，不访问设备、不执行控制。

physical_topology 描述用户声明的液路。缺少这一字段的旧场景只展示服务关系，
不会把 served_racks 画成已核验的水管。图能描述的拓扑与算法可控制的拓扑
分开报告：当前仍是单 CDU 聚合策略，没有共享水路协调器或机柜支路模型。
"""
from collections import defaultdict
from copy import deepcopy
import math

from .registry import adapter_metadata, observation_bindings, policy_metadata, registry_report


def _objects(value):
    """界面允许检查未完成草稿；投影时只读取结构完整的对象，错误另行报告。"""
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _identifier(value):
    return isinstance(value, str) and bool(value.strip())


def topology_errors(scene):
    """收集新增站点字段的结构错误，供 validate_scene 和工作台共用。

    supply/return 表示管路用途，不表示端口的进/出口方向；连接两端必须同用途。
    从 from_port 到 to_port 是绘图流向，供回管应分别声明。一次、二次侧不能
    用流体连接跨越板换；板换热耦合属于 CDU 设备模型，不是跨侧水管。
    """
    if isinstance(scene, dict) and scene.get("schema_version") == "0.2":
        from .analysis_config import analysis_scene_errors
        return analysis_scene_errors(scene)
    errors = []
    if not isinstance(scene, dict):
        return ["scene_mapping_required"]

    def walk(value, path="scene"):
        if isinstance(value, float) and not math.isfinite(value):
            errors.append("nonfinite_number:" + path)
        elif isinstance(value, dict):
            for key, item in value.items():
                walk(item, path + "." + str(key))
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                walk(item, path + "[" + str(index) + "]")
    walk(scene)
    assets = _objects(scene.get("assets"))
    by_id = {a["id"]: a for a in assets if _identifier(a.get("id"))}
    for asset in assets:
        asset_id = asset.get("id")
        if not _identifier(asset_id) or not _identifier(asset.get("kind")):
            errors.append("asset_id_and_kind_required")
            continue
        parent = asset.get("parent_id")
        if parent is not None and (not _identifier(parent) or parent not in by_id):
            errors.append("unknown_parent:" + asset_id)
        if asset.get("kind") == "node" and (parent not in by_id or by_id[parent].get("kind") != "rack"):
            errors.append("node_requires_rack_parent:" + asset_id)
        seen, current = set(), asset_id
        while isinstance(current, str) and current in by_id:
            if current in seen:
                errors.append("asset_parent_cycle:" + asset_id)
                break
            seen.add(current)
            current = by_id[current].get("parent_id")
    domains = _objects(scene.get("control_domains"))
    rack_owners = defaultdict(list)
    for domain in domains:
        for rack in domain.get("served_racks", []) if isinstance(domain.get("served_racks"), list) else []:
            if isinstance(rack, str):
                rack_owners[rack].append(domain)
    for rack, owners in rack_owners.items():
        if len(owners) > 1 and any(d.get("shared_hydraulics") is not True for d in owners):
            errors.append("duplicate_load_assignment_requires_coordinator:" + rack)

    physical = scene.get("physical_topology")
    if physical is None:
        layout = _mapping(_mapping(scene.get("extensions")).get("ui_layout"))
        errors.extend(_layout_errors(layout, by_id))
        errors.extend(_measurement_errors(scene, by_id, {}))
        return errors
    if not isinstance(physical, dict):
        return errors + ["physical_topology_mapping_required"]
    if physical.get("evidence") not in ("illustrative", "declared", "verified"):
        errors.append("physical_topology_evidence_required")
    # verified 是人工声明，必须提供可追溯引用；本函数不会替现场验收证明真伪。
    if physical.get("evidence") == "verified" and not physical.get("evidence_reference"):
        errors.append("verified_topology_evidence_reference_required")
    ports = _objects(physical.get("ports"))
    if not isinstance(physical.get("ports"), list) or not ports:
        errors.append("physical_ports_required")
    elif len(ports) != len(physical["ports"]):
        errors.append("physical_port_object_required")
    if not isinstance(physical.get("connections"), list):
        errors.append("physical_connections_required")
    elif len(_objects(physical["connections"])) != len(physical["connections"]):
        errors.append("physical_connection_object_required")
    port_ids = [p.get("id") for p in ports if _identifier(p.get("id"))]
    if len(port_ids) != len(ports) or len(set(port_ids)) != len(port_ids):
        errors.append("duplicate_or_invalid_port_id")
    by_port = {p["id"]: p for p in ports if _identifier(p.get("id"))}
    for p in ports:
        pid = str(p.get("id", "?"))
        if p.get("asset_id") not in by_id:
            errors.append("unknown_port_asset:" + pid)
        elif by_id[p["asset_id"]].get("kind") == "room_air":
            errors.append("air_heat_sink_cannot_have_liquid_port:" + pid)
        if p.get("side") not in ("primary", "secondary"):
            errors.append("invalid_port_side:" + pid)
        if p.get("direction") not in ("supply", "return"):
            errors.append("invalid_port_direction:" + pid)
        if not _identifier(p.get("loop_id")):
            errors.append("port_loop_required:" + pid)
        profile = _mapping(_mapping(scene.get("devices")).get(p.get("asset_id")))
        if profile.get("type") == "liquid_to_air" and p.get("side") == "primary":
            errors.append("liquid_to_air_primary_liquid_port_not_supported:" + pid)
    connections = _objects(physical.get("connections"))
    ids = [c.get("id") for c in connections if _identifier(c.get("id"))]
    if len(ids) != len(connections) or len(ids) != len(set(ids)):
        errors.append("duplicate_or_invalid_connection_id")
    paired, attached, seen_links = defaultdict(set), set(), set()
    orientations = defaultdict(lambda: defaultdict(set))
    loop_directions = defaultdict(set)
    directed = {(side, direction): defaultdict(set) for side in ("primary", "secondary")
                for direction in ("supply", "return")}
    secondary_graph, primary_graph = defaultdict(set), defaultdict(set)
    for connection in connections:
        cid = str(connection.get("id", "?"))
        source, target = by_port.get(connection.get("from_port")), by_port.get(connection.get("to_port"))
        if not source or not target:
            errors.append("unknown_connection_port:" + cid)
            continue
        if source.get("asset_id") == target.get("asset_id"):
            errors.append("fluid_self_connection_not_supported:" + cid)
        link = tuple(sorted((source["id"], target["id"])))
        if link in seen_links:
            errors.append("duplicate_fluid_connection:" + cid)
        seen_links.add(link)
        attached.update(link)
        if source.get("side") != target.get("side"):
            errors.append("primary_secondary_fluid_cross_connection:" + cid)
        if source.get("loop_id") != target.get("loop_id"):
            errors.append("connection_loop_mismatch:" + cid)
        if source.get("direction") != target.get("direction"):
            errors.append("supply_return_short_circuit:" + cid)
        if source.get("asset_id") not in by_id or target.get("asset_id") not in by_id:
            continue
        pair = (tuple(sorted((source["asset_id"], target["asset_id"]))),
                str(source.get("loop_id")), str(source.get("side")))
        paired[pair].add(source.get("direction"))
        loop_directions[(str(source.get("loop_id")), str(source.get("side")))].add(source.get("direction"))
        orientations[pair][source.get("direction")].add((source["asset_id"], target["asset_id"]))
        # 同一资产可以具有彼此隔离的多个回路；不能通过共享资产 ID 自动混水。
        # 图节点绑定 (asset_id, loop_id)，同一回路里的分支仍按资产内部连通处理。
        source_node = (source["asset_id"], source.get("loop_id"))
        target_node = (target["asset_id"], target.get("loop_id"))
        path_graph = directed.get((source.get("side"), source.get("direction")))
        if path_graph is not None:
            path_graph[source_node].add(target_node)
        if source.get("side") == target.get("side") == "secondary":
            secondary_graph[source_node].add(target_node)
            secondary_graph[target_node].add(source_node)
        elif source.get("side") == target.get("side") == "primary":
            primary_graph[source_node].add(target_node)
            primary_graph[target_node].add(source_node)
    for pair, directions in paired.items():
        if directions == {"supply", "return"} and orientations[pair]["supply"] != {(b, a) for a, b in orientations[pair]["return"]}:
            errors.append("supply_return_flow_orientation_mismatch:" + "/".join(pair[0]))
    # 供液总管和回液总管可以是两个不同资产。配对检查必须针对完整回路路径，
    # 不能强迫每一条供液管两端之间存在一条直接回液管。
    for loop, directions in loop_directions.items():
        if directions != {"supply", "return"}:
            errors.append("supply_return_pair_required:" + loop[0])
    for port_id in sorted(set(port_ids) - attached):
        errors.append("unconnected_physical_port:" + port_id)

    # 声明独立的两个域若被二次水路连在一起，现有独立执行器不可启动。
    visited = set()
    components = {}
    for start in secondary_graph:
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current in component:
                continue
            component.add(current)
            pending.extend(secondary_graph[current] - component)
        visited.update(component)
        for asset_id in component:
            components[asset_id] = component
        cdus = {a for a, _ in component if by_id[a].get("kind") == "cdu"}
        if len(cdus) > 1 and any(d.get("cdu_id") in cdus and d.get("shared_hydraulics") is not True for d in domains):
            errors.append("shared_secondary_connection_requires_coordinator:" + ",".join(sorted(cdus)))
    for domain in domains:
        cdu = domain.get("cdu_id")
        reachable_assets = {asset for node in components if node[0] == cdu
                            for asset, _ in components[node]}
        secondary_loops = {p.get("loop_id") for p in ports
                           if p.get("asset_id") == cdu and p.get("side") == "secondary"}
        for rack in domain.get("served_racks", []) if isinstance(domain.get("served_racks"), list) else []:
            if rack not in reachable_assets:
                errors.append("served_rack_not_connected_to_secondary:" + str(rack))
            if not any((rack, loop) in _reachable(directed[("secondary", "supply")], (cdu, loop))
                       and (cdu, loop) in _reachable(directed[("secondary", "return")], (rack, loop))
                       for loop in secondary_loops):
                errors.append("supply_return_pair_required:" + str(cdu) + "/" + str(rack))
        profile = _mapping(_mapping(scene.get("devices")).get(cdu))
        if profile.get("type") == "liquid_to_liquid":
            reached, pending = set(), [node for node in primary_graph if node[0] == cdu]
            while pending:
                current = pending.pop()
                if current in reached:
                    continue
                reached.add(current)
                pending.extend(primary_graph.get(current, set()) - reached)
            if domain.get("heat_sink_id") not in {asset for asset, _ in reached}:
                errors.append("declared_heat_sink_not_connected_to_primary:" + str(cdu))
            sink = domain.get("heat_sink_id")
            primary_loops = {p.get("loop_id") for p in ports
                             if p.get("asset_id") == cdu and p.get("side") == "primary"}
            if not any((cdu, loop) in _reachable(directed[("primary", "supply")], (sink, loop))
                       and (sink, loop) in _reachable(directed[("primary", "return")], (cdu, loop))
                       for loop in primary_loops):
                errors.append("primary_supply_return_path_required:" + str(cdu))
    errors.extend(_layout_errors(physical.get("layout", {}), by_id))
    errors.extend(_measurement_errors(scene, by_id, by_port))
    return list(dict.fromkeys(errors))


def _reachable(graph, start):
    """仅判断同一回路的有向连接可达，不推算流量、压降或支路平衡。"""
    reached, pending = set(), [start]
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(graph.get(current, set()) - reached)
    return reached


def _measurement_errors(scene, by_id, by_port):
    """测点归属不依赖是否绘制物理拓扑；带 port_id 的量必须具有有效端口定义。"""
    errors = []

    # 量的所属对象不能由显示标签改变。CDU 标准量仍必须绑定该 CDU。
    for asset_id, profile in _mapping(scene.get("devices")).items():
        if not isinstance(profile, dict):
            continue
        try:
            observations = observation_bindings(profile)
        except ValueError:
            # 未注册驱动由主配置检查报错；不能猜测其观测来源。
            continue
        for quantity, point in _mapping(observations).items():
            if not isinstance(point, dict):
                continue
            owner = point.get("asset_id", asset_id)
            if owner not in by_id or (quantity.startswith("cdu.") and owner != asset_id):
                errors.append("measurement_asset_mismatch:" + str(quantity))
            if "port_id" in point:
                port = by_port.get(point["port_id"])
                if not port or port.get("asset_id") != owner:
                    errors.append("measurement_port_mismatch:" + str(quantity))
                elif quantity.startswith("cdu.sec_") and port.get("side") != "secondary":
                    errors.append("measurement_side_mismatch:" + str(quantity))
                elif quantity in ("cdu.sec_supply_temp", "cdu.sec_return_temp"):
                    expected = "supply" if quantity == "cdu.sec_supply_temp" else "return"
                    if port.get("direction") != expected:
                        errors.append("measurement_direction_mismatch:" + str(quantity))
    return errors


def _layout_errors(layout, assets):
    """画布坐标只改变展示，不参与物理模型；仍拒绝 NaN 和未知资产。"""
    if not isinstance(layout, dict):
        return ["topology_layout_mapping_required"]
    errors = []
    for asset_id, position in layout.items():
        if asset_id not in assets:
            errors.append("unknown_layout_asset:" + str(asset_id))
        if not isinstance(position, dict) or not all(
                isinstance(position.get(k), (int, float)) and not isinstance(position.get(k), bool)
                and math.isfinite(position[k]) for k in ("x", "y")):
            errors.append("invalid_layout_position:" + str(asset_id))
    return errors


def _topology(scene):
    """把声明投影成节点／边；不根据液冷管线推导未测得的支路状态。"""
    assets = _objects(scene.get("assets"))
    nodes = [{**a, "label": a.get("label", a.get("name", a.get("id", "?"))),
              "telemetry_scope": "aggregate_cdu" if a.get("kind") == "cdu" else "configuration_only"}
             for a in assets if _identifier(a.get("id"))]
    edges = [{"id": "contains:" + a["id"], "source": a["parent_id"], "target": a["id"],
              "kind": "contains", "label": "资产归属"} for a in nodes if a.get("parent_id")]
    physical = scene.get("physical_topology")
    if isinstance(physical, dict):
        ports = {p["id"]: p for p in _objects(physical.get("ports")) if _identifier(p.get("id"))}
        for c in _objects(physical.get("connections")):
            a, b = ports.get(c.get("from_port")), ports.get(c.get("to_port"))
            if not a or not b:
                continue
            direction, side = a.get("direction"), a.get("side")
            edges.append({**c, "source": a.get("asset_id"), "target": b.get("asset_id"),
                          "kind": "fluid", "direction": direction, "side": side,
                          "loop_id": a.get("loop_id"),
                          "label": ("一次侧" if side == "primary" else "二次侧")
                          + ("供液" if direction == "supply" else "回液")})
        # 液对气 CDU 的热汇是空气边界，不能在物理图中伪装成一次侧水管。
        for d in _objects(scene.get("control_domains")):
            profile = _mapping(_mapping(scene.get("devices")).get(d.get("cdu_id")))
            if profile.get("type") == "liquid_to_air":
                edges.append({"id": str(d.get("id")) + ":air_sink", "source": d.get("cdu_id"),
                              "target": d.get("heat_sink_id"), "kind": "heat_dependency",
                              "label": "空气侧排热（非液体连接）"})
        return {"kind": "physical", "evidence": physical.get("evidence", "declared"),
                "evidence_reference": physical.get("evidence_reference"), "nodes": nodes,
                "edges": edges, "layout": deepcopy(_mapping(physical.get("layout"))),
                "solver": "not_implemented", "branch_telemetry": "not_available"}
    for d in _objects(scene.get("control_domains")):
        did = str(d.get("id", "?"))
        edges.append({"id": did + ":sink", "source": d.get("cdu_id"), "target": d.get("heat_sink_id"),
                      "kind": "heat_dependency", "label": "热汇依赖（非管线）"})
        for rack in d.get("served_racks", []) if isinstance(d.get("served_racks"), list) else []:
            edges.append({"id": did + ":" + str(rack), "source": d.get("cdu_id"), "target": rack,
                          "kind": "serves", "label": "服务关系（非管线）"})
    return {"kind": "service_relations", "evidence": "declared_service_relations",
            "nodes": nodes, "edges": edges,
            "layout": deepcopy(_mapping(_mapping(scene.get("extensions")).get("ui_layout"))),
            "solver": "not_implemented", "branch_telemetry": "not_available"}


def _capabilities(scene, valid):
    """声明能力与生效模式分开。静态通过后，运行网关仍逐周期检查设备真实状态。"""
    reports = []
    for domain in _objects(scene.get("control_domains")):
        profile = _mapping(_mapping(scene.get("devices")).get(domain.get("cdu_id")))
        policy = _mapping(profile.get("policy"))
        controls = _mapping(profile.get("controls"))
        observations = {}
        reasons, missing, required, invalid_optional = [], [], {}, []
        kind = policy.get("kind", "flow") if policy else None
        try:
            metadata = adapter_metadata(profile.get("adapter"))
            observations = observation_bindings(profile)
        except (ValueError, TypeError):
            metadata = {"simulation": False}
            reasons.append("adapter_not_implemented")
        if policy:
            try:
                policy_info = policy_metadata(policy)
                required = policy_info["required_observations"]
                required.update(policy_info.get("observations_by_control", {}).get(policy.get("control_quantity"), {}))
                missing = [q for q, unit in required.items()
                           if not isinstance(observations.get(q), dict) or observations[q].get("unit") != unit]
                invalid_optional = [q for q, unit in policy_info.get("optional_observations", {}).items()
                                    if q in observations and (not isinstance(observations[q], dict)
                                                             or observations[q].get("unit") != unit)]
            except (ValueError, TypeError):
                reasons.append("policy_not_implemented")
        else:
            reasons.append("automatic_policy_not_configured")
        if missing:
            reasons.append("required_observations_missing_or_unit_mismatch")
        if invalid_optional:
            reasons.append("optional_policy_observation_unit_mismatch")
        if not controls:
            reasons.append("no_controls_declared")
        if policy and policy.get("control_quantity") not in controls:
            reasons.append("policy_control_not_advertised")
        selected_control = _mapping(controls.get(policy.get("control_quantity")))
        selected_modes = selected_control.get("allowed_modes", [])
        simulation = metadata["simulation"]
        status = _mapping(_mapping(profile.get("points")).get("status"))
        missing_status = []
        if policy and simulation and profile.get("initial_mode") not in selected_modes:
            reasons.append("initial_mode_not_allowed_for_policy_control")
        elif policy and profile.get("adapter") == "modbus_tcp":
            # shadow 同样要运行状态守护。缺少控制属主或模式观测，只能监测。
            missing_status = [q for q in ("mode", "owner", "alarm") if q not in status]
            if missing_status:
                reasons.append("control_status_points_missing")
            else:
                device_modes = _mapping(_mapping(status.get("mode")).get("enum")).values()
                device_owners = _mapping(_mapping(status.get("owner")).get("enum")).values()
                if not any(mode in device_modes for mode in selected_modes):
                    reasons.append("policy_device_mode_not_mapped")
                if domain.get("owner") not in device_owners:
                    reasons.append("domain_owner_not_mapped")
        if domain.get("shared_hydraulics"):
            reasons.append("shared_hydraulics_coordinator_not_implemented")
        if not valid:
            reasons.append("scene_validation_failed")
        modes = ["monitor"] if valid else []
        automatic = valid and bool(policy) and bool(controls) and not missing and not reasons
        if automatic:
            modes.append("shadow")
        commissioning = "simulation_only" if simulation else "declared_unverified"
        if automatic and simulation and profile.get("adapter") != "sustain_fmu":
            modes.append("control")
        elif automatic and not simulation:
            # 不把 JSON 的验收勾选自动宣传成真实设备已经验证；现场入口仍可在显式
            # control 模式通过原有 Modbus commissioned() 和运行时保护进行验收后运行。
            reasons.append("hardware_control_requires_runtime_commissioning_and_authorization")
        if profile.get("adapter") == "sustain_fmu":
            modes = []
            reasons.append("dedicated_fmu_master_required")
        reports.append({"domain_id": domain.get("id"), "cdu_id": domain.get("cdu_id"),
                        "served_racks": domain.get("served_racks", []), "adapter": profile.get("adapter"),
                        "policy_kind": kind, "advertised_controls": list(controls),
                        "policy_control": policy.get("control_quantity"), "required_observations": required,
                        "missing_observations": missing, "invalid_optional_observations": invalid_optional,
                        "missing_control_status": missing_status, "supported_modes": modes,
                        "reasons": reasons, "commissioning": commissioning,
                        "can_thermal_sim": profile.get("adapter") == "thermal_sim" and "control" in modes,
                        "per_rack_control": "not_implemented", "hydraulic_solver": "not_implemented",
                        "topology_execution": "single_cdu_aggregate_only",
                        "control_selection": "declared_writable_quantity_not_heat_sink_type",
                        "shared_heat_sink_domains": [d.get("id") for d in _objects(scene.get("control_domains"))
                                                     if d.get("id") != domain.get("id")
                                                     and d.get("heat_sink_id") == domain.get("heat_sink_id")],
                        "heat_sink_capacity_coordination": "not_implemented",
                        "energy_optimization": "bounded_flow_policy_only_no_site_savings_claim"})
    return reports


def inspect_scene(scene):
    """工作台的统一只读检查 API；错误用列表返回，未完成草稿不导致 HTTP 500。

    valid 表示当前软件的配置语义通过，不表示设备连通、物理参数准确或现场验收。
    所有错误字段均为稳定原因码，界面可配中文说明；不包含设备凭据和远端报文。
    """
    if isinstance(scene, dict) and scene.get("schema_version") == "0.2":
        from .analysis_config import inspect_analysis_scene
        return inspect_analysis_scene(scene)
    from .configuration import validate_scene
    if not isinstance(scene, dict):
        return {"valid": False, "errors": ["scene_mapping_required"], "warnings": [],
                "capabilities": [], "topology": {"kind": "service_relations", "evidence": "unknown",
                "nodes": [], "edges": [], "layout": {}}, "registry": registry_report()}
    try:
        errors = topology_errors(scene)
    except (KeyError, TypeError, AttributeError):
        errors = ["malformed_topology_structure"]
    try:
        validate_scene(scene)
    except (ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        errors.append(str(exc) if isinstance(exc, ValueError) else "malformed_scene_structure:" + type(exc).__name__)
    errors = list(dict.fromkeys(errors))
    warnings = ["static_inspection_does_not_verify_hardware_or_site_safety",
                "rack_and_node_assets_do_not_create_branch_measurements_or_models"]
    if scene.get("physical_topology") is None:
        warnings.append("service_relations_are_not_verified_physical_pipes")
    else:
        warnings.append("physical_graph_is_display_and_validation_only_no_network_solver")
    try:
        topology = _topology(scene)
        capabilities = _capabilities(scene, not errors)
    except (KeyError, TypeError, AttributeError):
        errors.append("malformed_scene_projection")
        topology = {"kind": "service_relations", "evidence": "unknown", "nodes": [], "edges": [], "layout": {}}
        capabilities = []
    result = {"valid": not errors, "errors": errors, "warnings": warnings, "capabilities": capabilities,
              "topology": topology, "registry": registry_report()}
    # 输入 NaN 已判为错误，错误报告仍应可用严格 JSON 返回。仅净化报告副本，
    # 不替用户修正草稿、也不把 None 当成合法运行配置。
    def json_safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: json_safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [json_safe(item) for item in value]
        return value
    return json_safe(result)
