"""有界、只读的压力驱动水力分析（标准库，非设备控制器）。

未知量是压力节点的压力；支路流量来自压差和被动元件特性。负荷的最小流量
只在求解结束后比较，绝不作为强制供给量加入质量守恒。首版没有泵/冷站优化、
水锤、设备瞬态或芯片温度模型。所有结果都是配置边界下的模型估计。

Newton 仅用于寻找守恒解；近零压差使用有限差分导数，不修改真实流量。
失败时用单调节点二分恢复。关闭阀不添加虚假泄漏，孤立区域不伪造压力。
"""
import copy
import hashlib
import json
import math
import time


MAX_ELEMENTS = 256
MAX_UNKNOWNS = 64
MAX_ITERATIONS = 80
TIME_BUDGET_S = 2.0
MASS_TOL = 1e-7
PRESSURE_TOL = 0.01
GRAVITY = 9.80665
MAX_ABS_PRESSURE_PA = 1e9  # 数值支持范围，不是设备允许压力。


def _finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class HydraulicError(ValueError):
    """稳定原因码用于 CLI、工作台及验收报告；不包含设备网络操作。"""


def _linear_solve(matrix, rhs):
    """小规模稠密消元，部分选主元。硬规模上限避免宣称大型稀疏求解能力。"""
    n = len(rhs)
    a = [list(row) + [float(b)] for row, b in zip(matrix, rhs)]
    scale = max((abs(x) for row in matrix for x in row), default=1.0)
    for k in range(n):
        pivot = max(range(k, n), key=lambda i: abs(a[i][k]))
        if abs(a[pivot][k]) <= max(1e-18, scale * 1e-14):
            raise HydraulicError("singular_pressure_jacobian")
        a[k], a[pivot] = a[pivot], a[k]
        for i in range(k + 1, n):
            factor = a[i][k] / a[k][k]
            if factor:
                for j in range(k + 1, n + 1):
                    a[i][j] -= factor * a[k][j]
                a[i][k] = 0.0
    result = [0.0] * n
    for i in range(n - 1, -1, -1):
        result[i] = (a[i][n] - sum(a[i][j] * result[j] for j in range(i + 1, n))) / a[i][i]
    return result


def _quadratic_flow(dp, quadratic, linear):
    """稳定的二次阻力逆函数；避免 sqrt(b²+4ac)-b 的小数相消。"""
    if not dp:
        return 0.0
    d = abs(dp)
    q = 2 * d / (linear + math.sqrt(linear * linear + 4 * quadratic * d)) if quadratic else d / linear
    return math.copysign(q, dp)


class Element:
    """单个被动水力元件，流量单位 kg/s，压力单位 Pa。"""
    def __init__(self, spec, fluid):
        self.spec, self.id, self.kind = spec, spec["id"], spec["kind"]
        self.a, self.b = spec["from_junction"], spec["to_junction"]
        self.p, self.state = spec.get("parameters", {}), spec.get("state", {})
        if any(value is None for value in self.p.values()):
            raise HydraulicError("unknown_element_parameter:" + self.id)
        self.rho = fluid["density_kg_m3"]
        self.mu = fluid.get("dynamic_viscosity_pa_s")
        self.closed = self.state.get("closed", False)
        self.check = self.kind == "check_valve"
        self.crack = self.p.get("cracking_pressure_pa", 0.0) if self.check else 0.0
        self.r = self.p.get("resistance_pa_per_kg_s2", 0.0)
        self.linear = self.p.get("linear_pa_per_kg_s", 0.0)
        if self.kind in ("valve", "manual_balancing") and ("kv_m3_h" in self.p or "kv_curve" in self.p):
            if "position" not in self.state:
                raise HydraulicError("unknown_valve_position:" + self.id)
            opening = self.state["position"]
            curve = self.p.get("kv_curve")
            if curve:
                if opening < curve[0]["position"] or opening > curve[-1]["position"]:
                    raise HydraulicError("valve_position_outside_curve:" + self.id)
                kv = curve[-1]["kv_m3_h"]
                for left, right in zip(curve, curve[1:]):
                    if left["position"] <= opening <= right["position"]:
                        f = (opening - left["position"]) / (right["position"] - left["position"])
                        kv = left["kv_m3_h"] + f * (right["kv_m3_h"] - left["kv_m3_h"])
                        break
            elif self.p.get("characteristic") == "linear":
                kv = self.p["kv_m3_h"] * opening
            else:
                raise HydraulicError("valve_characteristic_required:" + self.id)
            if kv <= 0:
                self.closed = True
            else:
                # Kv: m³/h at 1 bar for water. Density correction is explicit.
                self.r = 1e5 * (self.rho / 1000.0) * (3600.0 / self.rho / kv) ** 2
        if self.kind not in ("pipe", "pressure_independent") and not self.closed and self.r <= 0 and self.linear <= 0:
            raise HydraulicError("positive_resistance_required:" + self.id)
        if self.kind == "pipe" and (not self.mu or not all(k in self.p for k in ("length_m", "diameter_m", "roughness_m", "minor_loss_k"))):
            raise HydraulicError("pipe_parameters_required:" + self.id)
        if self.kind == "pressure_independent" and not all(k in self.p for k in ("flow_setpoint_kg_s", "min_dp_pa", "max_dp_pa", "below_min_resistance_pa_per_kg_s2")):
            raise HydraulicError("pressure_independent_characteristic_required:" + self.id)
        if self.kind == "pressure_independent" and self.p["flow_setpoint_kg_s"] ** 2 * self.p["below_min_resistance_pa_per_kg_s2"] > self.p["min_dp_pa"] * (1 + 1e-9):
            # 给出的全开曲线在声称的最小工作压差下都达不到目标，不能标成调压成功。
            raise HydraulicError("inconsistent_pressure_independent_characteristic:" + self.id)

    def drop(self, q):
        """原物理压降；层流使用有限斜率，过渡段连续插值。"""
        if self.kind != "pipe":
            return self.r * q * abs(q) + self.linear * q
        if q == 0:
            return 0.0
        p, qabs = self.p, abs(q)
        d = p["diameter_m"]
        area = math.pi * d * d / 4
        re = qabs * d / (self.mu * area)
        laminar = 64 / re
        turbulent = 0.25 / math.log10(p["roughness_m"] / (3.7 * d) + 5.74 / max(re, 1.0) ** 0.9) ** 2
        if re <= 2000:
            friction = laminar
        elif re >= 4000:
            friction = turbulent
        else:
            weight = (re - 2000) / 2000
            friction = (1 - weight) * laminar + weight * turbulent
        pressure = (friction * p["length_m"] / d + p["minor_loss_k"]) * qabs ** 2 / (2 * self.rho * area ** 2)
        return math.copysign(pressure, q)

    def flow(self, dp, factor=1.0):
        if self.closed or (self.check and dp <= self.crack):
            return 0.0
        if self.check:
            dp -= self.crack
        if self.kind == "pressure_independent":
            if dp <= 0:
                return 0.0
            p = self.p
            # 压差不足时用明确给定的全开等效阻力，不能强制供给目标流量。
            available = math.sqrt(dp / (p["below_min_resistance_pa_per_kg_s2"] * factor))
            return min(p["flow_setpoint_kg_s"], available)
        if self.kind != "pipe":
            return _quadratic_flow(dp, self.r * factor, self.linear * factor)
        if not dp:
            return 0.0
        target = abs(dp) / factor
        lo, hi = 0.0, 1.0
        while abs(self.drop(hi)) < target and hi < 1e5:
            hi *= 2
        if hi >= 1e5:
            raise HydraulicError("flow_outside_numeric_envelope:" + self.id)
        for _ in range(48):
            mid = (lo + hi) / 2
            if self.drop(mid) < target:
                lo = mid
            else:
                hi = mid
        return math.copysign((lo + hi) / 2, dp)

    def conductance(self, dp):
        # 导数保护仅用于 Jacobian；flow() 从未添加最小泄漏流量。
        if self.closed:
            return 0.0
        h = max(0.001, abs(dp) * 1e-5)
        return max(0.0, (self.flow(dp + h) - self.flow(dp - h)) / (2 * h))

    def flow_bounds(self, dp_lo, dp_hi):
        if self.closed:
            return 0.0, 0.0
        uncertainty = self.p.get("relative_uncertainty")
        if uncertainty is None or not 0 <= uncertainty < 1:
            raise HydraulicError("resistance_uncertainty_required:" + self.id)
        values = [self.flow(dp, factor) for dp in (dp_lo, dp_hi)
                  for factor in (1 - uncertainty, 1 + uncertainty)]
        return min(values), max(values)


def _compile(scene, domain_id=None):
    domains = scene["control_domains"]
    if domain_id is None:
        if len(domains) != 1:
            raise HydraulicError("explicit_analysis_domain_required")
        domain = domains[0]
    else:
        domain = next((d for d in domains if d["id"] == domain_id), None)
        if domain is None:
            raise HydraulicError("unknown_analysis_domain")
    circuits = set(domain["circuit_ids"])
    fluids = {f["id"]: f for f in scene["fluid_circuits"]}
    nodes = {n["id"]: n for n in scene["hydraulics"]["junctions"] if n["circuit_id"] in circuits}
    # 按完整 circuit 选图，而非裁掉域成员之外的共享水阻/压力源。
    specs = [e for e in scene["hydraulics"]["elements"] if e["from_junction"] in nodes]
    if len(specs) > MAX_ELEMENTS or sum(n.get("pressure_pa") is None for n in nodes.values()) > MAX_UNKNOWNS:
        raise HydraulicError("solver_size_limit_exceeded")
    elements = []
    if not nodes or not specs:
        raise HydraulicError("hydraulic_network_required")
    for node in nodes.values():
        if "elevation_m" not in node:
            raise HydraulicError("junction_elevation_missing:" + node["id"])
        if node.get("pressure_pa") is not None and not node.get("source"):
            raise HydraulicError("pressure_source_missing:" + node["id"])
        if node.get("pressure_pa") is not None and (not _finite_number(node["pressure_pa"]) or abs(node["pressure_pa"]) > MAX_ABS_PRESSURE_PA):
            raise HydraulicError("pressure_outside_numeric_envelope:" + node["id"])
        fluid = fluids[node["circuit_id"]]
        elevation_head = fluid.get("density_kg_m3", 0) * GRAVITY * node["elevation_m"]
        if not _finite_number(elevation_head) or abs(elevation_head) > MAX_ABS_PRESSURE_PA:
            raise HydraulicError("elevation_head_outside_numeric_envelope:" + node["id"])
    for spec in specs:
        fluid = fluids[nodes[spec["from_junction"]]["circuit_id"]]
        if not fluid.get("density_kg_m3"):
            raise HydraulicError("density_required:" + fluid["id"])
        if not fluid.get("source"):
            raise HydraulicError("fluid_source_missing:" + fluid["id"])
        elements.append(Element(spec, fluid))
    return domain, nodes, elements, fluids


def solve_network(scene, domain_id=None, time_budget_s=TIME_BUDGET_S):
    """只求名义静态解。公开的分析入口另加参数/证据检查和区间计算。"""
    started = time.monotonic()
    deadline = started + min(TIME_BUDGET_S, max(0.001, time_budget_s))
    try:
        domain, nodes, elements, fluids = _compile(scene, domain_id)
        fixed = {key: n["pressure_pa"] + fluids[n["circuit_id"]]["density_kg_m3"] * GRAVITY * n.get("elevation_m", 0.0)
                 for key, n in nodes.items() if n.get("pressure_pa") is not None}
        if not fixed:
            raise HydraulicError("pressure_reference_required")
        adjacent = {key: [] for key in nodes}
        for e in elements:
            if not e.closed:
                adjacent[e.a].append((e, e.b, 1))
                adjacent[e.b].append((e, e.a, -1))
        # 显式关闭后可能形成浮置子图：流量可能已知为零，压力仍不可确定。
        reachable, queue = set(fixed), list(fixed)
        while queue:
            for _, neighbor, _ in adjacent[queue.pop()]:
                if neighbor not in reachable:
                    reachable.add(neighbor)
                    queue.append(neighbor)
        if set(nodes) - reachable:
            raise HydraulicError("unreferenced_pressure_component:" + ",".join(sorted(set(nodes) - reachable)))
        unknown = [key for key in nodes if key not in fixed]
        indices = {key: i for i, key in enumerate(unknown)}
        lo, hi = min(fixed.values()), max(fixed.values())
        pressures = {key: fixed.get(key, (lo + hi) / 2) for key in nodes}

        def evaluate(values, jacobian=False):
            flows, residual = {}, [0.0] * len(unknown)
            matrix = [[0.0] * len(unknown) for _ in unknown] if jacobian else None
            for e in elements:
                dp = values[e.a] - values[e.b]
                q = e.flow(dp)
                if not _finite_number(q):
                    raise HydraulicError("nonfinite_model_flow:" + e.id)
                flows[e.id] = q
                a, b = indices.get(e.a), indices.get(e.b)
                if a is not None:
                    residual[a] += q
                if b is not None:
                    residual[b] -= q
                if jacobian:
                    g = e.conductance(dp)
                    if not _finite_number(g):
                        raise HydraulicError("nonfinite_model_derivative:" + e.id)
                    if a is not None:
                        matrix[a][a] += g
                    if b is not None:
                        matrix[b][b] += g
                    if a is not None and b is not None:
                        matrix[a][b] -= g
                        matrix[b][a] -= g
            return flows, residual, matrix

        method, fallbacks, converged = "damped_pressure_newton", 0, False
        for iteration in range(MAX_ITERATIONS + 1):
            if time.monotonic() > deadline:
                raise HydraulicError("solver_time_budget_exceeded")
            flows, residual, matrix = evaluate(pressures, True)
            error = max(map(abs, residual), default=0.0)
            if error <= MASS_TOL:
                converged = True
                break
            if iteration == MAX_ITERATIONS:
                break
            improved = False
            try:
                delta = _linear_solve(matrix, [-x for x in residual])
                for exponent in range(16):
                    factor = 0.5 ** exponent
                    candidate = dict(pressures)
                    for key, change in zip(unknown, delta):
                        candidate[key] = min(hi, max(lo, pressures[key] + change * factor))
                    _, next_residual, _ = evaluate(candidate)
                    if max(map(abs, next_residual), default=0.0) < error:
                        pressures, improved = candidate, True
                        break
            except HydraulicError:
                pass
            if not improved:
                method = "newton_with_monotone_coordinate_fallback"
                fallbacks += 1
                for key in unknown:
                    left, right = lo, hi
                    for _ in range(36):
                        mid = (left + right) / 2
                        net = sum(sign * e.flow((mid - pressures[other]) * sign)
                                  for e, other, sign in adjacent[key])
                        if net > 0:
                            right = mid
                        else:
                            left = mid
                    pressures[key] = (left + right) / 2
        if not converged:
            raise HydraulicError("mass_balance_not_converged")
        # 满足守恒仍可能有未确定的压力（例如多个理想定流平台串联）。
        if unknown:
            try:
                # 最终唯一性以当前离散状态检查。中心差分可能跨过止回阀的
                # 开启阈值或定流阀平台，不能把另一状态的斜率当作当前可观测性。
                active_matrix = [[0.0] * len(unknown) for _ in unknown]
                for e in elements:
                    dp = pressures[e.a] - pressures[e.b]
                    inactive = e.closed or (e.check and dp <= e.crack + PRESSURE_TOL)
                    if e.kind == "pressure_independent":
                        inactive |= dp <= 0 or dp >= e.p["flow_setpoint_kg_s"] ** 2 * e.p["below_min_resistance_pa_per_kg_s2"] - PRESSURE_TOL
                    g = 0.0 if inactive else e.conductance(dp)
                    a, b = indices.get(e.a), indices.get(e.b)
                    if a is not None:
                        active_matrix[a][a] += g
                    if b is not None:
                        active_matrix[b][b] += g
                    if a is not None and b is not None:
                        active_matrix[a][b] -= g
                        active_matrix[b][a] -= g
                _linear_solve(active_matrix, [0.0] * len(unknown))
            except HydraulicError:
                raise HydraulicError("pressure_state_not_unique")
        pressure_error = 0.0
        states = {}
        for e in elements:
            dp, q = pressures[e.a] - pressures[e.b], flows[e.id]
            states[e.id] = "closed" if e.closed else "check_closed" if e.check and dp <= e.crack else "open"
            if e.kind == "pressure_independent" and not e.closed:
                if dp > e.p["max_dp_pa"]:
                    raise HydraulicError("pressure_independent_dp_above_range:" + e.id)
                states[e.id] = "pressure_regulated" if dp >= e.p["min_dp_pa"] else "below_working_pressure"
            elif not e.closed and not (e.check and dp <= e.crack):
                discrepancy = abs(dp - e.drop(q) - e.crack)
                if not _finite_number(discrepancy):
                    raise HydraulicError("nonfinite_pressure_residual:" + e.id)
                pressure_error = max(pressure_error, discrepancy)
        if pressure_error > PRESSURE_TOL:
            raise HydraulicError("original_pressure_residual_exceeded")
        return {"status": "converged", "reason": None, "method": method,
                "iterations": iteration, "fallbacks": fallbacks,
                "elapsed_ms": (time.monotonic() - started) * 1000,
                "residuals": {"mass_kg_s": error, "pressure_pa": pressure_error},
                "pressures_pa": {key: p - fluids[nodes[key]["circuit_id"]]["density_kg_m3"] * GRAVITY * nodes[key].get("elevation_m", 0.0) for key, p in pressures.items()},
                "potential_pa": pressures, "flows_kg_s": flows, "element_states": states}
    except (HydraulicError, KeyError, ZeroDivisionError, OverflowError, ValueError, TypeError) as exc:
        return {"status": "failed", "reason": str(exc), "elapsed_ms": (time.monotonic() - started) * 1000,
                "iterations": 0, "residuals": {}, "flows_kg_s": {}, "pressures_pa": {}}


def flow_enclosures(scene, domain_id=None):
    """被动网络的保守模型区间，不是 Monte Carlo 样本极值。

    使用压力最大值原理初始化范围，逐节点以邻居范围和阻力上下界收紧。
    最终无需收紧到一点：宽区间合法并应产生 unknown。当前仅支持固定物性、
    固定阀位下明确声明的阻力乘性误差与压力边界误差。不支持的误差来源返回
    unknown，绝不悄悄丢弃。区间保证仅针对声明模型，不是现场安全认证。
    """
    domain, nodes, elements, fluids = _compile(scene, domain_id)
    for fluid in fluids.values():
        if fluid["id"] in domain["circuit_ids"] and any(fluid.get(k, 0) for k in ("density_relative_uncertainty", "viscosity_relative_uncertainty")):
            raise HydraulicError("fluid_uncertainty_requires_equivalent_resistance_envelope")
    bounds, adjacent = {}, {key: [] for key in nodes}
    for key, node in nodes.items():
        if node.get("pressure_pa") is not None:
            if "pressure_uncertainty_pa" not in node:
                raise HydraulicError("boundary_uncertainty_required:" + key)
            potential = node["pressure_pa"] + fluids[node["circuit_id"]]["density_kg_m3"] * GRAVITY * node.get("elevation_m", 0.0)
            bounds[key] = [potential - node["pressure_uncertainty_pa"], potential + node["pressure_uncertainty_pa"]]
    low = min(b[0] for b in bounds.values())
    high = max(b[1] for b in bounds.values())
    fixed = set(bounds)
    for key in nodes:
        bounds.setdefault(key, [low, high])
    for e in elements:
        if e.state.get("position_uncertainty", 0):
            raise HydraulicError("position_uncertainty_requires_resistance_envelope:" + e.id)
        if e.kind == "pressure_independent":
            raise HydraulicError("pressure_independent_uncertainty_not_supported")
        e.flow_bounds(0, 1)  # 缺少误差声明时，不以零误差替代。
        if not e.closed:
            adjacent[e.a].append((e, e.b, 1))
            adjacent[e.b].append((e, e.a, -1))

    def residual_bound(key, pressure, upper):
        total = 0.0
        for e, other, sign in adjacent[key]:
            b = bounds[other]
            interval = e.flow_bounds(pressure - b[1], pressure - b[0]) if sign == 1 else e.flow_bounds(b[0] - pressure, b[1] - pressure)
            total += interval[1 if upper else 0] if sign == 1 else -interval[0 if upper else 1]
        return total

    deadline = time.monotonic() + 1.0
    for _ in range(32):
        largest = 0.0
        for key in nodes:
            if key in fixed:
                continue
            if time.monotonic() > deadline:
                break  # 当前包含区间仍有效，停止收紧不等于假定已收敛。
            old_lo, old_hi = bounds[key]
            updated = []
            for upper_residual in (True, False):
                lo, hi = old_lo, old_hi
                for _ in range(32):
                    mid = (lo + hi) / 2
                    if residual_bound(key, mid, upper_residual) > 0:
                        hi = mid
                    else:
                        lo = mid
                # 使用保留根的两侧端点；宽压力范围下固定次数二分的中点±
                # 常数容差不足以覆盖根，可能把临界最小流量误判为缺流。
                updated.append(lo if upper_residual else hi)
            new_lo = max(old_lo, updated[0] - PRESSURE_TOL)
            new_hi = min(old_hi, updated[1] + PRESSURE_TOL)
            if new_lo > new_hi:
                raise HydraulicError("inconsistent_interval_bounds")
            largest = max(largest, new_lo - old_lo, old_hi - new_hi)
            bounds[key] = [new_lo, new_hi]
        if largest < PRESSURE_TOL or time.monotonic() > deadline:
            break
    flows = {}
    for e in elements:
        a, b = bounds[e.a], bounds[e.b]
        lo, hi = e.flow_bounds(a[0] - b[1], a[1] - b[0])
        if not _finite_number(lo) or not _finite_number(hi):
            raise HydraulicError("nonfinite_flow_bounds:" + e.id)
        flows[e.id] = [lo - MASS_TOL, hi + MASS_TOL] if not e.closed else [0.0, 0.0]
    # 独立压力区间会丢失相关性。利用内部节点质量守恒进一步收紧流量区间，
    # 而不是按 assets/branch 父子关系猜测求和；定压边界允许有外部流入，跳过。
    for _ in range(16):
        for key, connections in adjacent.items():
            if key in fixed:
                continue
            for target, _, direction in connections:
                net_lo = net_hi = 0.0
                for other, _, sign in connections:
                    if other.id == target.id:
                        continue
                    a, b = flows[other.id]
                    net_lo += a if sign == 1 else -b
                    net_hi += b if sign == 1 else -a
                candidate = [-net_hi, -net_lo] if direction == 1 else [net_lo, net_hi]
                a, b = flows[target.id]
                narrowed = [max(a, candidate[0] - MASS_TOL), min(b, candidate[1] + MASS_TOL)]
                if narrowed[0] > narrowed[1]:
                    raise HydraulicError("inconsistent_mass_interval")
                flows[target.id] = narrowed
    return flows


def assess_interval(interval, minimum=None, maximum=None):
    """同一约束的区间跨限=unknown；最小流量须上界仍不足才确定违反。"""
    if interval is None or (minimum is None and maximum is None):
        return "unknown"
    lo, hi = interval
    if minimum is not None and hi < minimum:
        return "violated"
    if maximum is not None and lo > maximum:
        return "violated"
    if (minimum is None or lo >= minimum) and (maximum is None or hi <= maximum):
        return "satisfied"
    return "unknown"


def analyze_scene(scene, domain_id=None):
    """稳定 JSON 契约；仅配置输入，不创建任何 adapter/socket/控制任务。"""
    from .analysis_config import validate_analysis_scene, inspect_analysis_scene
    validate_analysis_scene(scene)
    result = {"schema_version": "0.2", "domain_id": domain_id,
              "source": "model_estimate", "hardware_writes": False,
              "scene_hash": hashlib.sha256(json.dumps(scene, sort_keys=True, allow_nan=False).encode()).hexdigest(),
              "readiness": {"status": "ready", "reasons": []},
              "feasibility": {"status": "unknown", "reason": "not_evaluated"},
              "branches": [], "junctions": [], "elements": [],
              "capabilities": {"static_hydraulics": True, "hardware_control": False,
                               "thermal_simulation": False, "derating_analysis": False},
              "limitations": ["配置边界下的静态估计，不是实测。", "没有泵曲线/能力包络，不提供降额或N+1证明。",
                              "区间仅覆盖明确声明的误差，不代替现场校核或独立保护。"]}
    try:
        domain, nodes, elements, _ = _compile(scene, domain_id)
        result["domain_id"] = domain["id"]
        capability = next(c for c in inspect_analysis_scene(scene)["capabilities"] if c["domain_id"] == domain["id"])
        if not capability["analysis_ready"]:
            raise HydraulicError(";".join(capability["analysis_reasons"]))
    except (HydraulicError, KeyError, TypeError, ValueError, OverflowError) as exc:
        result["readiness"] = {"status": "out_of_scope", "reasons": [str(exc)]}
        result["solver"] = {"status": "not_run", "reason": str(exc), "iterations": 0, "residuals": {}}
        result["capabilities"]["static_hydraulics"] = False
        result["feasibility"] = {"status": "unknown", "reason": "forward_model_not_ready"}
        result["identifiability"] = {"status": "out_of_scope", "reason": "forward_model_not_ready"}
        result["validation"] = {"status": "out_of_scope", "reason": "forward_model_not_ready", "field_validated": False}
        return result
    solution = solve_network(scene, domain["id"])
    result["solver"] = {k: v for k, v in solution.items() if k not in ("flows_kg_s", "pressures_pa", "potential_pa", "element_states")}
    intervals, interval_reason = {}, None
    if solution["status"] == "converged":
        try:
            intervals = flow_enclosures(scene, domain["id"])
        except (HydraulicError, ValueError, KeyError, ZeroDivisionError, TypeError, OverflowError) as exc:
            interval_reason = str(exc)
    else:
        interval_reason = "no_current_solution"
    for b in scene["branches"]:
        if b["id"] not in domain["branch_ids"]:
            continue
        flow = solution["flows_kg_s"].get(b["flow_element_id"])
        interval = intervals.get(b["flow_element_id"])
        status = assess_interval(interval, b.get("min_flow_kg_s"), b.get("max_flow_kg_s"))
        result["branches"].append({"id": b["id"], "asset_ref": b.get("asset_ref"),
            "estimated_flow_kg_s": flow, "flow_interval_kg_s": interval,
            "min_flow_kg_s": b.get("min_flow_kg_s"), "max_flow_kg_s": b.get("max_flow_kg_s"),
            "status": status, "reason": interval_reason or ("within_declared_model_bounds" if status == "satisfied" else "bounds_cross_limit" if status == "unknown" else "limit_violated_for_declared_bounds"),
            "interval_kind": "conservative_model_bounds" if interval is not None else None,
            "uncertainty_method": "passive_network_interval_contraction" if interval is not None else None,
            "field_safety_verified": False})
    result["junctions"] = [{"id": key, "estimated_pressure_pa": value, "pressure_basis": "configured_reference"}
                           for key, value in solution["pressures_pa"].items()]
    result["elements"] = [{"id": e.id, "estimated_flow_kg_s": solution["flows_kg_s"].get(e.id),
                           "state": solution.get("element_states", {}).get(e.id, "unknown")} for e in elements]
    statuses = [branch["status"] for branch in result["branches"]]
    result["feasibility"] = {
        "status": "infeasible" if "violated" in statuses else "satisfied" if statuses and all(s == "satisfied" for s in statuses) else "unknown",
        "scope": "declared_branch_flow_limits_and_uncertainty_only",
        "reason": "current_solution_unavailable" if solution["status"] != "converged" else "conditional_model_assessment",
        "field_safety_verified": False}
    from .hydraulic_evidence import identifiability_precheck, validate_predictions
    result["identifiability"] = identifiability_precheck(scene, domain["id"])
    result["validation"] = validate_predictions(scene, domain["id"])
    return result
