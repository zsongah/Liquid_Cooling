"""标准库静态水力规模基准；使用合成输入，不连接设备、不授权运行。

每轮复制完整输入，solve_network 重新创建元件及初始压力，绝不复用收敛解。
Python 进程与模块已加载，因此这测的是“求解器冷状态”，不是 OS/解释器冷启动。
超过未知压力数量上限的案例测拒绝延迟，不能混入成功求解的性能结论。
"""
import argparse
from collections import Counter
import copy
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lc_control import hydraulics as solver
from lc_control.analysis_config import validate_analysis_scene

SIZES = (32, 64, 128, 256)
SHAPES = ("independent_parallel", "shared_headers")


def build_scene(element_count, shape):
    """生成确切元件数的压力驱动液路；最小流量不被用作求解边界。

    独立并联：所有边直接连接两个已知压力节点，未知压力数为 0。
    共享总管：两条公共总管 + 每支路两条串联边；每支路有一个未知中点，
    再加两个公共未知节点。因此 128/256 元件分别有 65/129 个未知压力。
    """
    if type(element_count) is not int or element_count not in SIZES or shape not in SHAPES:
        raise ValueError("supported_sizes_32_64_128_256_and_shape_required")
    junctions = [{"id": name, "circuit_id": "SECONDARY", "elevation_m": 0.0,
                  "pressure_pa": pressure, "pressure_uncertainty_pa": 0.0,
                  "source": "Synthetic benchmark pressure reference; not a measured or commanded CDU quantity",
                  "evidence_scope": "synthetic"}
                 for name, pressure in (("SUPPLY", 300000.0), ("RETURN", 200000.0))]
    elements, branches = [], []

    def node(identity):
        junctions.append({"id": identity, "circuit_id": "SECONDARY", "elevation_m": 0.0})

    def edge(identity, start, end, resistance):
        elements.append({"id": identity, "kind": "resistance", "from_junction": start,
                         "to_junction": end, "parameters": {"resistance_pa_per_kg_s2": resistance,
                         "linear_pa_per_kg_s": 100.0, "relative_uncertainty": 0.05}, "state": {}})

    if shape == "independent_parallel":
        for index in range(element_count):
            identity = "PATH_%03d" % index
            edge(identity, "SUPPLY", "RETURN", 100000.0 + (index % 7) * 5000.0)
            branches.append({"id": "BRANCH_%03d" % index, "flow_element_id": identity})
    else:
        node("HEADER_SUPPLY")
        node("HEADER_RETURN")
        edge("COMMON_SUPPLY", "SUPPLY", "HEADER_SUPPLY", 25.0)
        edge("COMMON_RETURN", "HEADER_RETURN", "RETURN", 25.0)
        for index in range((element_count - 2) // 2):
            middle = "MIDDLE_%03d" % index
            node(middle)
            first = "BRANCH_%03d_IN" % index
            edge(first, "HEADER_SUPPLY", middle, 50000.0 + (index % 7) * 2500.0)
            edge("BRANCH_%03d_OUT" % index, middle, "HEADER_RETURN", 50000.0)
            branches.append({"id": "BRANCH_%03d" % index, "flow_element_id": first})
    return {"schema_version": "0.2", "scene_id": "benchmark_%s_%s" % (shape, element_count),
            "topology_version": "synthetic-benchmark-1", "assets": [{"id": "CDU", "kind": "cdu"}],
            "devices": {}, "fluid_circuits": [{"id": "SECONDARY", "density_kg_m3": 997.0,
                "dynamic_viscosity_pa_s": 0.00089, "specific_heat_j_kg_k": 4180.0,
                "source": "Synthetic constant fluid properties for a numerical benchmark",
                "evidence_scope": "synthetic"}],
            "hydraulics": {"junctions": junctions, "elements": elements}, "branches": branches,
            "control_domains": [{"id": "BENCHMARK_DOMAIN", "member_asset_ids": ["CDU"],
                "circuit_ids": ["SECONDARY"], "branch_ids": [b["id"] for b in branches], "actuator_ids": []}],
            "points": [], "actuators": [], "thermal_couplings": [],
            "extensions": {"purpose": "synthetic numerical benchmark, not equipment commissioning"}}


def cases(include_faults=True):
    """主表为两个形状乘四个规模，边界案例只添加到单独类别。"""
    result = [{"id": "%s_%d" % (shape, size), "category": "scale", "shape": shape,
               "scene": build_scene(size, shape)} for shape in SHAPES for size in SIZES]
    if include_faults:
        near = build_scene(32, "independent_parallel")
        near["hydraulics"]["elements"][0].update(kind="valve", parameters={"kv_m3_h": 0.5,
            "characteristic": "linear", "relative_uncertainty": 0.05}, state={"position": 0.0001})
        reverse = build_scene(32, "independent_parallel")
        reverse["hydraulics"]["elements"][0].update(kind="check_valve", from_junction="RETURN", to_junction="SUPPLY")
        reverse["hydraulics"]["elements"][0]["parameters"]["cracking_pressure_pa"] = 5000.0
        jump = build_scene(32, "independent_parallel")
        jump["hydraulics"]["junctions"][0]["pressure_pa"] = 400000.0
        for identity, scene, note in (("near_closed", near, "Fixed near-closed Kv at position 0.0001; not a valve actuation"),
                                     ("check_reverse", reverse, "Reverse differential on one check valve; expected zero flow"),
                                     ("boundary_jump", jump, "Separate static solve after +100 kPa supply change; no transient dynamics")):
            result.append({"id": identity, "category": "boundary_case", "shape": "independent_parallel",
                           "scene": scene, "note": note, "probe_element_id": "PATH_000"})
    return result


def quantile(values, fraction):
    """线性插值经验分位数；10 个样本的 p99 不代表生产尾延迟保证。"""
    ordered = sorted(values)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * fraction
    low, high = math.floor(rank), math.ceil(rank)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def measure_case(case, repeats, budget_s):
    scene = case["scene"]
    validate_analysis_scene(scene)  # 合法超限描述仍需由求解器明确拒绝。
    unknowns = sum(n.get("pressure_pa") is None for n in scene["hydraulics"]["junctions"])
    unsupported = unknowns > solver.MAX_UNKNOWNS or len(scene["hydraulics"]["elements"]) > solver.MAX_ELEMENTS
    samples = []
    for iteration in range(repeats):
        fresh = copy.deepcopy(scene)  # 深拷贝不计入求解延迟；求解器内部编译与初始化计入。
        started = time.perf_counter()
        try:
            solved = solver.solve_network(fresh, "BENCHMARK_DOMAIN", time_budget_s=budget_s)
            elapsed = (time.perf_counter() - started) * 1000
            sample = {"repeat": iteration + 1, "elapsed_ms": elapsed,
                      "solver_elapsed_ms": solved.get("elapsed_ms"), "status": solved["status"],
                      "reason": solved.get("reason"), "iterations": solved.get("iterations"),
                      "residuals": solved.get("residuals", {}),
                      "budget_exceeded": elapsed > budget_s * 1000,
                      "flow_probe_kg_s": solved.get("flows_kg_s", {}).get(case.get("probe_element_id", ""))}
            # 一个包含 NaN 的“成功”不能写成 benchmark 合格结果。
            json.dumps(solved, allow_nan=False)
            sample["expectation_met"] = (solved["status"] == "failed" and solved.get("reason") == "solver_size_limit_exceeded"
                                         if unsupported else solved["status"] == "converged")
            sample["expectation_met"] &= not sample["budget_exceeded"]
            if not unsupported and solved["status"] == "converged":
                residuals = solved.get("residuals", {})
                sample["expectation_met"] &= (residuals.get("mass_kg_s", math.inf) <= solver.MASS_TOL
                                              and residuals.get("pressure_pa", math.inf) <= solver.PRESSURE_TOL)
            if case["id"] == "check_reverse":
                sample["expectation_met"] &= sample["flow_probe_kg_s"] == 0.0
            elif case["id"] == "near_closed":
                q = sample["flow_probe_kg_s"]
                sample["expectation_met"] &= q is not None and 0 < q < 0.001
        except Exception as exc:
            sample = {"repeat": iteration + 1, "elapsed_ms": (time.perf_counter() - started) * 1000,
                      "status": "exception", "reason": type(exc).__name__ + ":" + str(exc),
                      "residuals": {}, "expectation_met": False}
        samples.append(sample)
    durations = [s["elapsed_ms"] for s in samples]
    return {"id": case["id"], "category": case["category"], "shape": case["shape"],
            "note": case.get("note"), "element_count": len(scene["hydraulics"]["elements"]),
            "junction_count": len(scene["hydraulics"]["junctions"]), "unknown_pressure_count": unknowns,
            "expected": "unsupported_unknown_pressure_limit" if unsupported else "converged_with_residual_checks",
            "timing_kind": "rejection_latency" if unsupported else "fresh_state_solve_latency",
            "input_sha256": hashlib.sha256(json.dumps(scene, sort_keys=True).encode()).hexdigest(),
            "status_counts": dict(Counter(s["status"] for s in samples)),
            "errors": sorted({s["reason"] for s in samples if s.get("reason")}),
            "all_expectations_met": all(s["expectation_met"] for s in samples),
            "latency_ms": {"p50": quantile(durations, 0.5), "p95": quantile(durations, 0.95),
                           "p99": quantile(durations, 0.99), "max": max(durations)},
            "samples": samples}


def run_benchmark(repeats=10, budget_s=2.0, include_faults=True):
    """记录实际执行结果和环境；失败保留在报告中，不通过删样本改善分位数。"""
    if type(repeats) is not int or not 1 <= repeats <= 100:
        raise ValueError("repeats_must_be_1_to_100")
    if isinstance(budget_s, bool) or not isinstance(budget_s, (int, float)) or not math.isfinite(budget_s) or not 0 < budget_s <= 2:
        raise ValueError("budget_s_must_be_positive_at_most_2")
    started = time.perf_counter()
    report = {"schema_version": "1", "generated_at_utc": datetime.now(timezone.utc).isoformat(),
              "source": "synthetic_numerical_benchmark", "hardware_writes": False,
              "environment": {"python": sys.version, "implementation": platform.python_implementation(),
                              "platform": platform.platform(), "machine": platform.machine()},
              "settings": {"repeats": repeats, "solver_budget_s": budget_s,
                           "max_elements": solver.MAX_ELEMENTS, "max_unknown_pressures": solver.MAX_UNKNOWNS,
                           "initialization": "fresh_input_and_solver_state_each_repeat_python_process_reused",
                           "timer_scope": "solve_network_including_model_compile_excluding_deepcopy_schema_validation_and_python_startup"},
              "code_sha256": {name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
                              for name in ("lc_control/hydraulics.py", "lc_control/analysis_config.py", "tools/benchmark_hydraulics.py")},
              "cases": [measure_case(case, repeats, budget_s) for case in cases(include_faults)],
              "limitations": ["Synthetic model only; no OEM, hardware or field validation.",
                              "No hardware execution permission is derived from benchmark results.",
                              "10 repetitions give empirical quantiles, not a production p99 guarantee.",
                              "Fixed-boundary parallel edges are easier than coupled unknown-pressure networks.",
                              "Rejected oversized cases measure rejection latency, not successful solve performance.",
                              "Scope is solve_network only; uncertainty intervals and evidence screening are not timed.",
                              "Static boundary jump is not a transient, water-hammer or thermal test.",
                              "Per-solver time budget is cooperative, not an OS hard real-time guarantee."]}
    report["total_elapsed_s"] = time.perf_counter() - started
    report["all_expectations_met"] = all(case["all_expectations_met"] for case in report["cases"])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="outputs/hydraulic-analysis/benchmark.json")
    parser.add_argument("--repeats", type=int, default=10)
    parser.add_argument("--budget-s", type=float, default=2.0)
    parser.add_argument("--no-faults", action="store_true", help="只运行两个形状的规模主表")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(args.repeats, args.budget_s, not args.no_faults)
    except ValueError as exc:
        parser.error(str(exc))
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(destination.resolve()), "cases": len(report["cases"]),
                      "all_expectations_met": report["all_expectations_met"],
                      "total_elapsed_s": report["total_elapsed_s"]}, ensure_ascii=False))
    return 0 if report["all_expectations_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
