"""schema 0.2 的离线工程描述与能力门控，不创建驱动、不写设备。

资产树、液路图和协议点表是不同关系。这里允许多 CDU 共享域被正确描述，
但绝不把“配置结构合法”升级为“参数充分”或“可以现场执行”。旧版控制器仍
使用 0.1 合约；0.2 首期只给静态分析器提供经检查的输入。
"""
from copy import deepcopy
import hashlib
import json
import math

from .registry import registry_report


MAX_UNKNOWN_PRESSURES = 64
MAX_ELEMENTS = 256
EVIDENCE_SCOPES = {"synthetic", "bench", "field", "declared"}
ELEMENT_KINDS = {"resistance", "pipe", "valve", "manual_balancing",
                 "pressure_independent", "check_valve", "heat_exchanger"}
POINT_GROUPS = {"observations", "setpoints", "status", "commands"}
WIRE_FIELDS = {"address", "write_address", "read_function", "write_function", "dtype",
               "scale", "offset", "byte_order", "word_order", "connection", "register"}


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _id(value):
    return isinstance(value, str) and bool(value.strip())


def _map(value):
    return value if isinstance(value, dict) else {}


def _index(value, name, errors):
    """容忍未完成草稿并收集错误，投影层不能因错误引用直接抛 KeyError。"""
    if not isinstance(value, list):
        errors.append(name + "_list_required")
        return {}
    result = {}
    if len(value) > 10000:
        errors.append(name + "_resource_limit")
        return result
    for row in value:
        if not isinstance(row, dict) or not _id(row.get("id")):
            errors.append(name + "_id_required")
            continue
        if row["id"] in result:
            errors.append("duplicate_" + name + "_id:" + row["id"])
        result[row["id"]] = row
    return result


def _references(value, known, label, errors):
    if not isinstance(value, list) or any(not _id(v) for v in value):
        errors.append(label + "_list_required")
        return []
    if len(set(value)) != len(value):
        errors.append(label + "_duplicate")
    for ref in value:
        if ref not in known:
            errors.append(label + "_unknown:" + ref)
    return value


def _acyclic(records, children, label, errors):
    """迭代深度优先检查；资产层级深度不占 Python 调用栈。"""
    finished = set()
    for start in records:
        if start in finished:
            continue
        active = set()
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                active.discard(node)
                finished.add(node)
            elif node in active:
                errors.append(label + "_cycle:" + node)
            elif node not in finished and node in records:
                active.add(node)
                stack.append((node, True))
                stack.extend((child, False) for child in children.get(node, []))


def _json_errors(scene):
    """禁止 JSON 内隐藏非有限数；只扫描配置，不访问文件或硬件。"""
    errors, stack, seen, count = [], [(scene, "scene", 0)], set(), 0
    while stack:
        item, path, depth = stack.pop()
        count += 1
        if count > 200000 or depth > 128:
            return errors + ["configuration_structure_resource_limit"]
        if isinstance(item, float) and not math.isfinite(item):
            errors.append("nonfinite_number:" + path)
        elif isinstance(item, (dict, list)):
            # JSON 不含对象别名；防御 Python 调用方传入循环容器。
            if id(item) in seen:
                continue
            seen.add(id(item))
            entries = item.items() if isinstance(item, dict) else enumerate(item)
            stack.extend((value, path + "." + str(key), depth + 1) for key, value in entries)
    return errors


def _collect_unchecked(scene):
    errors, reasons, warnings = [], [], []
    if not isinstance(scene, dict):
        return {"errors": ["scene_mapping_required"], "reasons": [], "warnings": []}
    errors.extend(_json_errors(scene))
    if scene.get("schema_version") != "0.2":
        errors.append("unsupported_analysis_schema_version")
    if not _id(scene.get("topology_version")):
        errors.append("topology_version_required")
    assets = _index(scene.get("assets"), "asset", errors)
    circuits = _index(scene.get("fluid_circuits"), "fluid_circuit", errors)
    hydraulics = _map(scene.get("hydraulics"))
    if not isinstance(scene.get("hydraulics"), dict):
        errors.append("hydraulics_mapping_required")
    junctions = _index(hydraulics.get("junctions"), "junction", errors)
    elements = _index(hydraulics.get("elements"), "element", errors)
    branches = _index(scene.get("branches", []), "branch", errors)
    points = _index(scene.get("points", []), "point", errors)
    actuators = _index(scene.get("actuators", []), "actuator", errors)
    domains = _index(scene.get("control_domains", []), "domain", errors)
    couplings = _index(scene.get("thermal_couplings", []), "thermal_coupling", errors)
    devices = _map(scene.get("devices"))
    if not isinstance(scene.get("devices", {}), dict):
        errors.append("devices_mapping_required")
    # 协议映射只有 devices 这一份来源。跨 Profile 的同一物理写地址也不能
    # 通过不同名字逃过冲突检查；当前只支持已注册协议的工程描述。
    occupied = {}
    from .registry import validate_adapter_profile
    for did, profile in devices.items():
        if not _id(did) or not isinstance(profile, dict):
            errors.append("invalid_device_profile:" + str(did))
            continue
        if profile.get("asset_ref") is not None and profile["asset_ref"] not in assets:
            errors.append("unknown_device_asset:" + did)
        try:
            validate_adapter_profile(profile)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append("invalid_device_profile:" + did + ":" + (str(exc) if isinstance(exc, ValueError) else "malformed_binding"))
        connection = _map(profile.get("connection"))
        if profile.get("adapter") == "modbus_tcp":
            endpoint = tuple(connection.get(k) for k in ("host", "port", "unit_id"))
            mappings = _map(profile.get("points"))
            for group in ("setpoints", "commands"):
                for key, leaf in _map(mappings.get(group)).items():
                    if not isinstance(leaf, dict):
                        continue
                    address = leaf.get("write_address", leaf.get("address"))
                    width = 2 if leaf.get("dtype") in ("float32", "int32", "uint32") else 1
                    if type(address) is int:
                        for offset in range(width):
                            identity = endpoint + (address + offset,)
                            if identity in occupied:
                                errors.append("overlapping_physical_write_bindings:" + did + ":" + str(key))
                            occupied[identity] = (did, group, key)
    parents = {}
    for aid, asset in assets.items():
        if not _id(asset.get("kind")):
            errors.append("asset_kind_required:" + aid)
        parent = asset.get("parent_id")
        if parent is not None:
            if not _id(parent) or parent not in assets:
                errors.append("unknown_parent:" + aid)
            else:
                parents[aid] = [parent]
    _acyclic(assets, parents, "asset_parent", errors)

    pipe_circuits = {junctions.get(e.get("from_junction"), {}).get("circuit_id")
                     for e in elements.values() if e.get("kind") == "pipe"}
    for cid, circuit in circuits.items():
        if circuit.get("evidence_scope") not in EVIDENCE_SCOPES:
            errors.append("fluid_evidence_scope_required:" + cid)
        if not _id(circuit.get("source")):
            reasons.append("fluid_source_missing:" + cid)
        for name in ("density_kg_m3", "dynamic_viscosity_pa_s", "specific_heat_j_kg_k"):
            value = circuit.get(name)
            if value is None:
                if name == "density_kg_m3" or name == "dynamic_viscosity_pa_s" and cid in pipe_circuits:
                    reasons.append("fluid_property_missing:" + cid + ":" + name)
                else:
                    warnings.append("unused_or_thermal_fluid_property_missing:" + cid + ":" + name)
            elif not _finite(value) or value <= 0:
                errors.append("invalid_fluid_property:" + cid + ":" + name)
        for name in ("density_relative_uncertainty", "viscosity_relative_uncertainty"):
            if name in circuit and (not _finite(circuit[name]) or not 0 <= circuit[name] < 1):
                errors.append("invalid_fluid_uncertainty:" + cid + ":" + name)
    for jid, junction in junctions.items():
        if junction.get("circuit_id") not in circuits:
            errors.append("unknown_junction_circuit:" + jid)
        elevation = junction.get("elevation_m")
        if elevation is None:
            reasons.append("junction_elevation_missing:" + jid)
        elif not _finite(elevation):
            errors.append("invalid_junction_elevation:" + jid)
        pressure = junction.get("pressure_pa")
        if "pressure_pa" in junction and pressure is None:
            reasons.append("boundary_pressure_unknown:" + jid)
        if pressure is not None:
            if not _finite(pressure):
                errors.append("invalid_boundary_pressure:" + jid)
            if junction.get("evidence_scope") not in EVIDENCE_SCOPES:
                errors.append("pressure_evidence_scope_required:" + jid)
            if not _id(junction.get("source")):
                reasons.append("pressure_source_missing:" + jid)
            # 设定值不是实测过程压差；模型边界可使用明确 synthetic 的假设。
            if (junction.get("pressure_source") in ("setpoint", "command")
                    or "setpoint_ref" in junction or "command_ref" in junction):
                errors.append("setpoint_is_not_pressure_boundary:" + jid)
            pref = junction.get("point_ref")
            if pref is not None:
                point = points.get(pref, {})
                if _map(point.get("binding_ref")).get("group") != "observations":
                    errors.append("pressure_boundary_requires_observation_point:" + jid)
        uncertainty = junction.get("pressure_uncertainty_pa")
        if uncertainty is not None and (not _finite(uncertainty) or uncertainty < 0):
            errors.append("invalid_pressure_uncertainty:" + jid)

    adjacency = {jid: set() for jid in junctions}
    element_circuits = {}
    for eid, element in elements.items():
        kind = element.get("kind")
        if kind not in ELEMENT_KINDS:
            errors.append("unsupported_element_kind:" + eid)
        a, b = element.get("from_junction"), element.get("to_junction")
        if not _id(a) or not _id(b) or a not in junctions or b not in junctions:
            errors.append("unknown_element_junction:" + eid)
        elif a == b:
            errors.append("element_self_connection:" + eid)
        else:
            ca, cb = junctions[a].get("circuit_id"), junctions[b].get("circuit_id")
            if ca != cb:
                errors.append("cross_circuit_mass_connection:" + eid)
            else:
                element_circuits[eid] = ca
            adjacency[a].add(b)
            adjacency[b].add(a)
        if element.get("asset_ref") is not None and element["asset_ref"] not in assets:
            errors.append("unknown_element_asset:" + eid)
        params, state = _map(element.get("parameters")), _map(element.get("state"))
        if not isinstance(element.get("parameters", {}), dict) or not isinstance(element.get("state", {}), dict):
            errors.append("element_parameters_and_state_mapping_required:" + eid)
        for name in ("resistance_pa_per_kg_s2", "linear_pa_per_kg_s", "length_m", "diameter_m",
                     "roughness_m", "minor_loss_k", "kv_m3_h", "relative_uncertainty", "cracking_pressure_pa"):
            value = params.get(name)
            if name in params and value is None and name != "relative_uncertainty":
                reasons.append("element_parameter_unknown:" + eid + ":" + name)
            if value is not None and (not _finite(value) or value < 0 or name in ("diameter_m", "kv_m3_h") and value == 0):
                errors.append("invalid_element_parameter:" + eid + ":" + name)
        if _finite(params.get("relative_uncertainty")) and params["relative_uncertainty"] >= 1:
            errors.append("relative_uncertainty_outside_unit_interval:" + eid)
        if "closed" in state and type(state["closed"]) is not bool:
            errors.append("closed_state_must_be_boolean:" + eid)
        if "position" in state and (not _finite(state["position"]) or not 0 <= state["position"] <= 1):
            errors.append("invalid_valve_position:" + eid)
        if "position_uncertainty" in state and (not _finite(state["position_uncertainty"]) or not 0 <= state["position_uncertainty"] <= 1):
            errors.append("invalid_valve_position_uncertainty:" + eid)
        # 参数缺失是“不能估计”，不是结构错误，更不是需求不可满足。
        if kind in ("resistance", "heat_exchanger", "check_valve"):
            if not any(_finite(params.get(k)) and params[k] > 0 for k in ("resistance_pa_per_kg_s2", "linear_pa_per_kg_s")):
                reasons.append("resistance_parameters_missing:" + eid)
        elif kind == "pipe":
            for name in ("length_m", "diameter_m", "roughness_m", "minor_loss_k"):
                if params.get(name) is None:
                    reasons.append("pipe_parameter_missing:" + eid + ":" + name)
        elif kind in ("valve", "manual_balancing"):
            if params.get("kv_m3_h") is None and params.get("kv_curve") is None:
                reasons.append("valve_coefficient_missing:" + eid)
            if "position" not in state:
                reasons.append("valve_state_unknown:" + eid)
            characteristic = params.get("characteristic")
            if characteristic not in (None, "linear"):
                errors.append("unsupported_valve_characteristic:" + eid)
            if characteristic is None and params.get("kv_curve") is None:
                reasons.append("valve_characteristic_missing:" + eid)
            curve = params.get("kv_curve")
            if curve is not None:
                if not isinstance(curve, list) or len(curve) < 2 or any(
                        not isinstance(row, dict) or not _finite(row.get("position")) or not 0 <= row["position"] <= 1
                        or not _finite(row.get("kv_m3_h")) or row["kv_m3_h"] < 0 for row in curve):
                    errors.append("invalid_kv_curve:" + eid)
                elif any(a["position"] >= b["position"] or a["kv_m3_h"] > b["kv_m3_h"] for a, b in zip(curve, curve[1:])):
                    errors.append("kv_curve_must_be_monotonic:" + eid)
                elif _finite(state.get("position")) and not curve[0]["position"] <= state["position"] <= curve[-1]["position"]:
                    reasons.append("valve_position_outside_curve:" + eid)
        elif kind == "pressure_independent":
            for name in ("flow_setpoint_kg_s", "min_dp_pa", "max_dp_pa", "below_min_resistance_pa_per_kg_s2"):
                value = params.get(name)
                if value is None:
                    reasons.append("pressure_independent_parameter_missing:" + eid + ":" + name)
                elif not _finite(value) or value <= 0:
                    errors.append("invalid_pressure_independent_parameter:" + eid + ":" + name)
            if _finite(params.get("min_dp_pa")) and _finite(params.get("max_dp_pa")) and params["min_dp_pa"] >= params["max_dp_pa"]:
                errors.append("pressure_independent_range_reversed:" + eid)

    # 每个实际连通子图检查边界；circuit ID 相同也不等于已经连通。
    visited = set()
    for start in junctions:
        if start in visited:
            continue
        component, pending = set(), [start]
        while pending:
            node = pending.pop()
            if node in component:
                continue
            component.add(node)
            pending.extend(adjacency[node] - component)
        visited.update(component)
        if not any(_finite(junctions[node].get("pressure_pa")) for node in component):
            reasons.append("pressure_reference_missing:" + start)
    if not elements or not junctions:
        reasons.append("hydraulic_network_empty")
    unknowns = sum(j.get("pressure_pa") is None for j in junctions.values())
    if unknowns > MAX_UNKNOWN_PRESSURES or len(elements) > MAX_ELEMENTS:
        reasons.append("static_analysis_resource_envelope_exceeded")

    branch_children, branch_parent, flow_owners = {}, {}, {}
    for bid, branch in branches.items():
        eid = branch.get("flow_element_id")
        if eid not in elements:
            errors.append("unknown_branch_flow_element:" + bid)
        elif eid in flow_owners:
            errors.append("duplicate_branch_flow_element:" + bid)
        else:
            flow_owners[eid] = bid
        if branch.get("asset_ref") is not None and branch["asset_ref"] not in assets:
            errors.append("unknown_branch_asset:" + bid)
        for name in ("min_flow_kg_s", "max_flow_kg_s"):
            value = branch.get(name)
            if value is not None and (not _finite(value) or value < 0):
                errors.append("invalid_branch_flow_requirement:" + bid)
        lo, hi = branch.get("min_flow_kg_s"), branch.get("max_flow_kg_s")
        if _finite(lo) and _finite(hi) and lo > hi:
            errors.append("branch_flow_bounds_reversed:" + bid)
        children = _references(branch.get("child_branch_ids", []), branches, "branch_children:" + bid, errors)
        branch_children[bid] = children
        for child in children:
            if child in branch_parent:
                errors.append("branch_multiple_parents:" + child)
            branch_parent[child] = bid
            ce = branches.get(child, {}).get("flow_element_id")
            if eid in element_circuits and ce in element_circuits and element_circuits[eid] != element_circuits[ce]:
                errors.append("branch_children_cross_circuit:" + bid)
    _acyclic(branches, branch_children, "branch", errors)

    bindings = {}
    for pid, point in points.items():
        if WIRE_FIELDS.intersection(point):
            errors.append("point_must_reference_single_wire_binding:" + pid)
        if point.get("asset_ref") is not None and point["asset_ref"] not in assets:
            errors.append("unknown_point_asset:" + pid)
        if not _id(point.get("quantity")):
            errors.append("point_quantity_required:" + pid)
        binding = point.get("binding_ref")
        if binding is None:
            if point.get("status") != "unbound":
                errors.append("point_binding_or_unbound_status_required:" + pid)
            warnings.append("unbound_point:" + pid)
            continue
        binding = _map(binding)
        did, group, key = binding.get("device_id"), binding.get("group"), binding.get("key")
        if not all(_id(v) for v in (did, group, key)) or group not in POINT_GROUPS:
            errors.append("invalid_point_binding_ref:" + pid)
            continue
        leaf = _map(_map(_map(devices.get(did)).get("points")).get(group)).get(key)
        if not isinstance(leaf, dict):
            errors.append("unknown_point_binding:" + pid)
        else:
            identity = (did, group, key)
            if identity in bindings:
                errors.append("duplicate_semantic_point_binding:" + pid)
            bindings[identity] = pid
            if point.get("unit") is not None and point["unit"] != leaf.get("unit"):
                errors.append("semantic_point_unit_mismatch:" + pid)
        location = _map(point.get("location_ref"))
        for name, known in (("junction_id", junctions), ("element_id", elements), ("branch_id", branches)):
            if name in location and location[name] not in known:
                errors.append("unknown_point_location:" + pid)
        terminals = point.get("pressure_terminals")
        if terminals is not None:
            terminals = _map(terminals)
            a, b = terminals.get("positive_junction"), terminals.get("negative_junction")
            if a not in junctions or b not in junctions or a == b:
                errors.append("invalid_pressure_terminals:" + pid)
            elif junctions[a].get("circuit_id") != junctions[b].get("circuit_id"):
                errors.append("pressure_terminals_cross_circuit:" + pid)

    actuator_targets = {}
    for aid, actuator in actuators.items():
        if actuator.get("asset_ref") not in assets or actuator.get("element_ref") not in elements:
            errors.append("unknown_actuator_asset_or_element:" + aid)
        pid = actuator.get("setpoint_ref")
        if pid not in points:
            errors.append("unknown_actuator_setpoint:" + aid)
        elif _map(points[pid].get("binding_ref")).get("group") != "setpoints":
            errors.append("actuator_requires_setpoint_binding:" + aid)
        feedback = actuator.get("actual_feedback_ref")
        if feedback is not None and (feedback not in points or _map(points[feedback].get("binding_ref")).get("group") != "observations"):
            errors.append("actuator_actual_feedback_requires_observation:" + aid)
        control = _map(actuator.get("control_ref"))
        did, quantity = control.get("device_id"), control.get("quantity")
        if not _id(did) or not _id(quantity) or quantity not in _map(_map(devices.get(did)).get("controls")):
            errors.append("unknown_actuator_control_ref:" + aid)
        binding = _map(points.get(pid, {}).get("binding_ref"))
        if did != binding.get("device_id") or quantity != binding.get("key"):
            errors.append("actuator_control_binding_mismatch:" + aid)
        if _id(did) and _id(quantity):
            target = (did, quantity)
            if target in actuator_targets:
                errors.append("duplicate_actuator_control_target:" + aid)
            actuator_targets[target] = aid
        if actuator.get("authority_domain_ref") not in domains:
            errors.append("unknown_actuator_authority_domain:" + aid)

    branch_domains, actuator_domains = {}, {}
    for did, domain in domains.items():
        members = _references(domain.get("member_asset_ids"), assets, "domain_members:" + did, errors)
        cids = _references(domain.get("circuit_ids"), circuits, "domain_circuits:" + did, errors)
        bids = _references(domain.get("branch_ids"), branches, "domain_branches:" + did, errors)
        aids = _references(domain.get("actuator_ids"), actuators, "domain_actuators:" + did, errors)
        for bid in bids:
            if bid in branch_domains:
                errors.append("branch_assigned_to_multiple_domains:" + bid)
            branch_domains[bid] = did
            branch = branches.get(bid, {})
            if branch.get("asset_ref") is not None and branch["asset_ref"] not in members:
                errors.append("branch_asset_outside_domain:" + bid)
            if element_circuits.get(branch.get("flow_element_id")) not in cids:
                errors.append("branch_circuit_outside_domain:" + bid)
        for aid in aids:
            if aid in actuator_domains:
                errors.append("actuator_assigned_to_multiple_domains:" + aid)
            actuator_domains[aid] = did
            actuator = actuators.get(aid, {})
            if actuator.get("authority_domain_ref") != did:
                errors.append("actuator_authority_mismatch:" + aid)
            if actuator.get("asset_ref") not in members:
                errors.append("actuator_asset_outside_domain:" + aid)
            if element_circuits.get(actuator.get("element_ref")) not in cids:
                errors.append("actuator_circuit_outside_domain:" + aid)
    for aid in actuators:
        if aid not in actuator_domains:
            errors.append("actuator_not_declared_by_authority_domain:" + aid)

    for tid, coupling in couplings.items():
        if coupling.get("kind") not in ("heat_exchanger", "liquid_to_air"):
            errors.append("unsupported_thermal_coupling_kind:" + tid)
        if coupling.get("asset_ref") not in assets:
            errors.append("unknown_thermal_coupling_asset:" + tid)
        sides = coupling.get("sides")
        if not isinstance(sides, list) or len(sides) != 2 or any(not isinstance(side, dict) for side in sides):
            errors.append("thermal_coupling_requires_two_sides:" + tid)
            continue
        side_circuits = []
        for side in sides:
            if "air_boundary_asset_ref" in side:
                air = assets.get(side["air_boundary_asset_ref"], {})
                if coupling.get("kind") != "liquid_to_air" or air.get("kind") not in ("room_air", "air_boundary"):
                    errors.append("invalid_air_thermal_boundary:" + tid)
                side_circuits.append(None)
                continue
            refs = _references(side.get("element_ids"), elements, "thermal_side_elements:" + tid, errors)
            cid = side.get("circuit_id")
            if cid not in circuits or not refs or any(element_circuits.get(ref) != cid for ref in refs):
                errors.append("thermal_side_circuit_mismatch:" + tid)
            side_circuits.append(cid)
        if coupling.get("kind") == "heat_exchanger" and len(set(side_circuits)) != 2:
            errors.append("heat_exchanger_requires_isolated_circuits:" + tid)
        if coupling.get("kind") == "liquid_to_air" and side_circuits.count(None) != 1:
            errors.append("liquid_to_air_requires_one_fluid_and_one_air_side:" + tid)
        warnings.append("thermal_coupling_not_thermally_solved:" + tid)

    return {"errors": list(dict.fromkeys(errors)), "reasons": list(dict.fromkeys(reasons)),
            "warnings": list(dict.fromkeys(warnings)), "assets": assets, "circuits": circuits,
            "junctions": junctions, "elements": elements, "branches": branches,
            "domains": domains, "unknown_pressures": unknowns}


def _collect(scene):
    """任意草稿都返回诊断，恶意/损坏字段类型不能造成工作台 HTTP 500。"""
    try:
        return _collect_unchecked(scene)
    except (KeyError, TypeError, AttributeError, IndexError):
        return {"errors": ["malformed_analysis_scene_structure"], "reasons": [], "warnings": []}


def analysis_scene_errors(scene):
    """只返回结构错误；缺参/分析范围限制留在能力报告，不能误判为物理不可行。"""
    return _collect(scene)["errors"]


def validate_analysis_scene(scene):
    """校验 0.2 结构与引用，返回原对象；绝不自动补默认物性或放开执行。"""
    errors = analysis_scene_errors(scene)
    if errors:
        raise ValueError(";".join(errors))
    return scene


def migrate_legacy_scene(scene):
    """把合法 0.1 配置复制为不可执行的 0.2 草稿，不猜任何工程参数。

    旧 Profile、协议点表、限值与策略作为原始声明保留，但不因此获得新的执行
    能力。旧图的资产内部连通是旧版假设，不能自动翻译成新图的 junction/element。
    所以迁移后必须由工程人员补齐液路与边界；本函数不发布、不切换在运行版本。
    """
    from .configuration import validate_scene
    if not isinstance(scene, dict) or scene.get("schema_version") != "0.1":
        raise ValueError("migration_requires_schema_0_1")
    validate_scene(scene)
    source_hash = hashlib.sha256(json.dumps(scene, sort_keys=True, ensure_ascii=False,
                                            allow_nan=False).encode("utf-8")).hexdigest()
    assets = deepcopy(scene["assets"])
    children = {}
    for asset in assets:
        children.setdefault(asset.get("parent_id"), []).append(asset["id"])
    domains = []
    for old in scene["control_domains"]:
        members, pending = set(), [old["cdu_id"]] + list(old.get("served_racks", []))
        while pending:
            current = pending.pop()
            if current in members:
                continue
            members.add(current)
            pending.extend(children.get(current, []))
        domains.append({"id": old["id"], "member_asset_ids": [a["id"] for a in assets if a["id"] in members],
                        "circuit_ids": [], "branch_ids": [], "actuator_ids": []})
    retained = {"schema_version", "scene_id", "topology_version", "assets", "devices", "control_domains",
                "physical_topology", "extensions"}
    migration = {"source_schema": "0.1", "source_hash": source_hash, "not_executable": True,
        "gaps": ["explicit_fluid_circuits_required", "explicit_junctions_and_elements_required",
                 "measured_or_declared_pressure_boundaries_required", "physical_parameter_evidence_required",
                 "semantic_points_and_actuators_require_engineering_mapping"],
        "warnings": ["draft_only_not_published_or_enabled", "legacy_policy_preserved_but_not_executed",
                     "served_racks_and_legacy_ports_do_not_supply_hydraulic_parameters",
                     "legacy_commissioning_flags_do_not_grant_schema_0_2_execution"]}
    draft = {"schema_version": "0.2", "scene_id": scene.get("scene_id", "migrated_scene"),
        "topology_version": str(scene["topology_version"]) + ":v0.2-draft", "assets": assets,
        "devices": deepcopy(scene["devices"]), "fluid_circuits": [],
        "hydraulics": {"junctions": [], "elements": []}, "branches": [], "points": [],
        "actuators": [], "thermal_couplings": [], "control_domains": domains,
        "extensions": {"legacy_source": {"schema_version": "0.1", "source_hash": source_hash,
            "not_executable": True, "physical_topology": deepcopy(scene.get("physical_topology")),
            "control_domains": deepcopy(scene["control_domains"]), "extensions": deepcopy(scene.get("extensions", {})),
            "unmapped_fields": {k: deepcopy(v) for k, v in scene.items() if k not in retained}}}}
    validate_analysis_scene(draft)
    return {"scene": draft, "migration": migration}


def _domain_reasons(data, domain):
    """独立域的缺参不污染另一条已完整描述的液路；仍选整个 circuit，不能裁支路。"""
    raw = domain.get("circuit_ids", [])
    cids = {v for v in raw if _id(v)} if isinstance(raw, list) else set()
    junctions = data.get("junctions", {})
    elements = data.get("elements", {})
    chosen_nodes = {jid for jid, j in junctions.items() if j.get("circuit_id") in cids}
    chosen_elements = {eid for eid, e in elements.items() if e.get("from_junction") in chosen_nodes}
    reasons = []
    for reason in data["reasons"]:
        parts = reason.split(":")
        if parts[0] in ("static_analysis_resource_envelope_exceeded", "hydraulic_network_empty"):
            continue
        if len(parts) < 2:
            reasons.append(reason)
        elif parts[0].startswith("fluid_") and parts[1] in cids:
            reasons.append(reason)
        elif parts[0].startswith(("junction_", "pressure_reference_", "pressure_source_", "boundary_pressure_")) and parts[1] in chosen_nodes:
            reasons.append(reason)
        elif parts[1] in chosen_elements:
            reasons.append(reason)
    if not chosen_nodes or not chosen_elements:
        reasons.append("hydraulic_network_empty")
    if len(chosen_elements) > MAX_ELEMENTS or sum(junctions[j].get("pressure_pa") is None for j in chosen_nodes) > MAX_UNKNOWN_PRESSURES:
        reasons.append("static_analysis_resource_envelope_exceeded")
    return list(dict.fromkeys(reasons))


def inspect_analysis_scene(scene):
    """给工作台/CLI 的通用只读投影，所有现场运行模式始终为空。"""
    data = _collect(scene)
    errors = data["errors"]
    ready = not errors and not data["reasons"]
    nodes, edges = [], []
    used_node_ids = set(data.get("assets", {}))
    junction_node_ids = {}
    for jid in data.get("junctions", {}):
        projected = "junction:" + jid
        while projected in used_node_ids:
            projected += ":hydraulic"
        junction_node_ids[jid] = projected
        used_node_ids.add(projected)
    for aid, asset in data.get("assets", {}).items():
        # 只投影确定字段，错误草稿中的 NaN 或任意深扩展不会污染 JSON 报告。
        node = {"id": aid, "kind": asset.get("kind") if _id(asset.get("kind")) else "unknown",
                "label": asset.get("label") if _id(asset.get("label")) else aid,
                "telemetry_scope": "configuration_only"}
        if _id(asset.get("parent_id")):
            node["parent_id"] = asset["parent_id"]
            edges.append({"id": "contains:" + aid, "source": asset["parent_id"], "target": aid,
                          "kind": "contains", "label": "资产归属（不代表管路）"})
        nodes.append(node)
    for jid, junction in data.get("junctions", {}).items():
        nodes.append({"id": junction_node_ids[jid], "kind": "hydraulic_junction", "label": jid,
                      "junction_id": jid, "circuit_id": junction.get("circuit_id") if _id(junction.get("circuit_id")) else None,
                      "telemetry_scope": "analysis_only_not_measured"})
    for eid, element in data.get("elements", {}).items():
        if _id(element.get("from_junction")) and _id(element.get("to_junction")):
            edges.append({"id": "element:" + eid, "source": junction_node_ids.get(element["from_junction"], "junction:" + element["from_junction"]),
                          "target": junction_node_ids.get(element["to_junction"], "junction:" + element["to_junction"]), "kind": "fluid",
                          "element_id": eid, "element_kind": element.get("kind"),
                          "asset_ref": element.get("asset_ref"), "label": eid})
    reasons = list(data["reasons"])
    if errors:
        reasons.insert(0, "scene_validation_failed")
    capabilities = []
    def ids(value):
        return [item for item in value if _id(item)] if isinstance(value, list) else []
    for did, domain in data.get("domains", {}).items():
        domain_reasons = ["scene_validation_failed"] if errors else _domain_reasons(data, domain)
        domain_ready = not domain_reasons
        capabilities.append({"domain_id": did, "member_asset_ids": ids(domain.get("member_asset_ids")),
            "circuit_ids": ids(domain.get("circuit_ids")), "branch_ids": ids(domain.get("branch_ids")),
            "actuator_ids": ids(domain.get("actuator_ids")), "adapter": None, "policy_kind": None,
            "supported_modes": [], "supported_analysis": ["static_hydraulics"] if domain_ready else [],
            "analysis_ready": domain_ready, "execution_ready": False, "can_thermal_sim": False,
            "analysis_reasons": domain_reasons,
            "reasons": domain_reasons + ["schema_0_2_analysis_only_hardware_execution_blocked"],
            "commissioning": "not_granted_by_schema", "topology_execution": "read_only_analysis",
            "per_rack_control": "not_implemented", "energy_optimization": "not_implemented"})
    report = {"schema_version": "0.2", "valid": not errors, "schema_valid": not errors,
        "analysis_ready": ready, "execution_ready": False, "errors": errors,
        "warnings": ["static_inspection_does_not_verify_hardware_or_site_safety",
                     "analysis_outputs_are_estimates_not_branch_telemetry",
                     "pressure_boundary_is_not_a_cdu_setpoint"] + data["warnings"],
        "capabilities": capabilities, "registry": registry_report(),
        "analysis": {"ready": ready, "reasons": reasons,
                     "limits": {"max_unknown_pressures": MAX_UNKNOWN_PRESSURES, "max_elements": MAX_ELEMENTS},
                     "unknown_pressures": data.get("unknown_pressures", 0), "scope": "static_hydraulics_only"},
        "topology": {"kind": "physical", "evidence": "declared", "source": "hydraulic_graph_v0.2",
                     "nodes": nodes, "edges": edges, "layout": {}, "solver": "static_analysis_only",
                     "branch_telemetry": "not_available"}}
    # 标量异常值已被错误报告拒绝；只净化展示副本，不修改或修复用户配置。
    def safe(value):
        if isinstance(value, float) and not math.isfinite(value):
            return None
        if isinstance(value, dict):
            return {key: safe(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [safe(item) for item in value]
        return value
    return safe(report)
