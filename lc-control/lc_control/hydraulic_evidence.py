"""水力模型的条件可辨识性筛查与独立数据校核。

本模块不拟合参数、不自动晋升模型、不授权控制。局部灵敏度满秩不是全局唯一
证明；数据来自配置声明，测试通过也不构成现场证书。所有计算都有输入数量和
时间预算。无法评估时返回 out_of_scope，不以缺省值制造 pass。
"""
import copy
import math
import time

from .hydraulics import HydraulicError, _linear_solve, solve_network


def _number(x, positive=False):
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x) and (x > 0 if positive else x >= 0)


def _failure(reason, status="out_of_scope"):
    return {"status": status, "reason": reason, "field_validated": False}


def _case(scene, overrides):
    result = copy.deepcopy(scene)
    nodes = {n["id"]: n for n in result["hydraulics"]["junctions"]}
    if not isinstance(overrides, dict):
        raise HydraulicError("boundary_overrides_mapping_required")
    for key, value in overrides.items():
        if key not in nodes or "pressure_pa" not in nodes[key] or not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise HydraulicError("invalid_existing_boundary_override:" + str(key))
        nodes[key]["pressure_pa"] = value
    return result


def _solve(scene, domain_id, deadline):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise HydraulicError("evidence_time_budget_exceeded")
    result = solve_network(scene, domain_id, time_budget_s=remaining)
    if result["status"] != "converged":
        raise HydraulicError("evidence_forward_failed:" + str(result["reason"]))
    return result


def _rank_columns(rows, tolerance):
    """列选主元、两次重正交的 Gram-Schmidt；返回秩和尺度代理，非 SVD。"""
    columns = [list(column) for column in zip(*rows)]
    order = list(range(len(columns)))
    diagonals = []
    for k in range(len(columns)):
        pivot = max(range(k, len(columns)), key=lambda i: sum(x * x for x in columns[i]))
        columns[k], columns[pivot] = columns[pivot], columns[k]
        order[k], order[pivot] = order[pivot], order[k]
        length = math.sqrt(sum(x * x for x in columns[k]))
        if length <= max(1e-14, (diagonals[0] if diagonals else length) * tolerance):
            break
        diagonals.append(length)
        vector = [x / length for x in columns[k]]
        for j in range(k + 1, len(columns)):
            for _ in range(2):
                projection = sum(x * y for x, y in zip(columns[j], vector))
                columns[j] = [x - projection * y for x, y in zip(columns[j], vector)]
    return len(diagonals), order, diagonals


def identifiability_precheck(scene, domain_id):
    analysis = scene.get("analysis", {})
    if not isinstance(analysis, dict):
        return _failure("analysis_mapping_required")
    spec = analysis.get("identifiability")
    if spec is None:
        return {"status": "not_required", "reason": "forward_parameters_given_not_identified",
                "global_uniqueness_proven": False, "field_validated": False}
    try:
        if not isinstance(spec, dict):
            raise HydraulicError("identifiability_mapping_required")
        parameters, experiments = spec.get("parameters"), spec.get("experiments")
        if not isinstance(parameters, list) or not 1 <= len(parameters) <= 8:
            raise HydraulicError("fit_parameter_count_must_be_1_to_8")
        if not isinstance(experiments, list) or not 1 <= len(experiments) <= 8:
            raise HydraulicError("experiment_count_must_be_1_to_8")
        for name in ("rank_rtol", "max_condition", "max_relative_interval_width"):
            if not _number(spec.get(name), True):
                raise HydraulicError("predeclared_identification_acceptance_required:" + name)
        if spec["rank_rtol"] >= 1 or spec["max_condition"] < 1:
            raise HydraulicError("invalid_rank_acceptance")
        elements = {e["id"]: e for e in scene["hydraulics"]["elements"]}
        scales, values, identities = [], [], []
        for p in parameters:
            identity = (p["element_id"], p["name"])
            if identity in identities or identity[1] not in ("resistance_pa_per_kg_s2", "linear_pa_per_kg_s"):
                raise HydraulicError("unsupported_or_duplicate_fit_parameter")
            value = elements[identity[0]]["parameters"][identity[1]]
            scale = p.get("scale", value)
            if not _number(value, True) or not _number(scale, True):
                raise HydraulicError("positive_fit_parameter_and_scale_required")
            identities.append(identity)
            scales.append(scale)
            values.append(value)
        deadline = time.monotonic() + 3.0
        rows = []
        for experiment in experiments:
            case = _case(scene, experiment.get("boundary_pressures_pa", {}))
            measurements = experiment.get("observations")
            if not isinstance(measurements, list) or not 1 <= len(measurements) <= 32:
                raise HydraulicError("measurement_count_must_be_1_to_32")
            for obs in measurements:
                if not isinstance(obs.get("element_ids"), list) or not obs["element_ids"] or len(obs["element_ids"]) != len(set(obs["element_ids"])) or not _number(obs.get("uncertainty_kg_s"), True):
                    raise HydraulicError("independent_observation_and_positive_uncertainty_required")
            # 暂不接受相关观测被当作独立样本；未来扩展完整协方差接口。
            if experiment.get("correlated_observations", False):
                raise HydraulicError("correlated_noise_whitening_not_implemented")
            derivatives = []
            for index, (element_id, name) in enumerate(identities):
                h = values[index] * 1e-3
                estimates = []
                for step in (h, h / 2):
                    forward = []
                    states = []
                    for sign in (-1, 1):
                        perturbed = copy.deepcopy(case)
                        target = next(e for e in perturbed["hydraulics"]["elements"] if e["id"] == element_id)
                        target["parameters"][name] = values[index] + sign * step
                        solved = _solve(perturbed, domain_id, deadline)
                        states.append(solved["element_states"])
                        forward.append([sum(solved["flows_kg_s"][eid] for eid in obs["element_ids"]) for obs in measurements])
                    if states[0] != states[1]:
                        raise HydraulicError("sensitivity_crosses_discrete_state_boundary")
                    estimates.append([(b - a) / (2 * step) for a, b in zip(*forward)])
                for a, b in zip(*estimates):
                    if abs(a - b) > max(1e-9, 0.02 * max(abs(a), abs(b))):
                        raise HydraulicError("sensitivity_step_not_stable")
                derivatives.append(estimates[-1])
            for i, obs in enumerate(measurements):
                rows.append([derivatives[j][i] * scales[j] / obs["uncertainty_kg_s"] for j in range(len(parameters))])
        rank, order, diagonals = _rank_columns(rows, spec["rank_rtol"])
        report = {"status": "pass", "reason": "local_screen_passed_only", "scope": "local_linearized_at_declared_parameters",
                  "parameter_count": len(parameters), "measurement_count": len(rows), "effective_rank": rank,
                  "method": "scaled_noise_weighted_pivoted_qr", "rank_rtol": spec["rank_rtol"],
                  "qr_diagonal": diagonals, "parameter_order": [list(identities[i]) for i in order],
                  "global_uniqueness_proven": False, "structural_uniqueness_status": "out_of_scope",
                  "fixed_parameter_uncertainty_propagated": False, "field_validated": False}
        if rank < len(parameters):
            report.update(status="fail", reason="rank_deficient_at_declared_conditions",
                          requested_calibration_scope="out_of_scope", practical_status="not_evaluated")
            return report
        condition = max(diagonals) / min(diagonals)
        report["condition_proxy"] = condition
        if condition > spec["max_condition"]:
            report.update(status="fail", reason="ill_conditioned_sensitivity", practical_status="fail")
            return report
        normal = [[sum(row[i] * row[j] for row in rows) for j in range(rank)] for i in range(rank)]
        relative_widths = []
        for i in range(rank):
            column = _linear_solve(normal, [1.0 if j == i else 0.0 for j in range(rank)])
            relative_widths.append(2 * 1.96 * math.sqrt(max(0, column[i])) * scales[i] / values[i])
        report["approximate_95_percent_relative_widths"] = relative_widths
        report["interval_assumption"] = "local_linearization_independent_gaussian_measurement_errors_fixed_other_parameters"
        passed = max(relative_widths) <= spec["max_relative_interval_width"]
        report["practical_status"] = "pass" if passed else "fail"
        if not passed:
            report.update(status="fail", reason="parameter_interval_too_wide")
        return report
    except (HydraulicError, KeyError, TypeError, ValueError, OverflowError, AttributeError, ZeroDivisionError) as exc:
        return _failure(str(exc))


def validate_predictions(scene, domain_id):
    """留出案例校核：含测量误差的逐点门槛和可区分支路对排序，不只报平均值。"""
    analysis = scene.get("analysis", {})
    if not isinstance(analysis, dict):
        return _failure("analysis_mapping_required")
    spec = analysis.get("validation")
    if spec is None:
        return _failure("independent_branch_validation_data_required")
    try:
        if not isinstance(spec, dict):
            raise HydraulicError("validation_mapping_required")
        if not spec.get("dataset_id") or not spec.get("calibration_dataset_id") or spec["dataset_id"] == spec["calibration_dataset_id"]:
            raise HydraulicError("distinct_calibration_and_validation_dataset_ids_required")
        if spec.get("evidence_scope") not in ("synthetic", "bench", "field"):
            raise HydraulicError("validation_evidence_scope_required")
        acceptance = spec.get("acceptance", {})
        if not _number(acceptance.get("absolute_error_kg_s")) or not _number(acceptance.get("relative_error")):
            raise HydraulicError("predeclared_validation_error_budget_required")
        if not isinstance(acceptance.get("require_ordering", False), bool):
            raise HydraulicError("validation_ordering_boolean_required")
        minimum = acceptance.get("minimum_cases_per_branch")
        if not isinstance(minimum, int) or isinstance(minimum, bool) or minimum < 1:
            raise HydraulicError("minimum_validation_coverage_required")
        cases = spec.get("cases")
        if not isinstance(cases, list) or not 1 <= len(cases) <= 32:
            raise HydraulicError("validation_case_count_must_be_1_to_32")
        domain = next(d for d in scene["control_domains"] if d["id"] == domain_id)
        branches = {b["id"]: b for b in scene["branches"] if b["id"] in domain["branch_ids"]}
        if not branches:
            raise HydraulicError("validation_target_branches_required")
        counts = {key: 0 for key in branches}
        rows, violations, sensor_insufficient, compared, unresolvable = [], [], [], 0, 0
        deadline = time.monotonic() + 3.0
        case_ids = set()
        for case in cases:
            if not case.get("id") or case["id"] in case_ids:
                raise HydraulicError("unique_validation_case_id_required")
            case_ids.add(case["id"])
            solution = _solve(_case(scene, case.get("boundary_pressures_pa", {})), domain_id, deadline)
            observed = []
            seen = set()
            observations = case.get("observations")
            if not isinstance(observations, list) or not 1 <= len(observations) <= 256:
                raise HydraulicError("bounded_validation_observations_required")
            for obs in observations:
                key = obs["branch_id"]
                if key not in branches or key in seen or not _number(obs.get("flow_kg_s")) or not _number(obs.get("uncertainty_kg_s")):
                    raise HydraulicError("invalid_validation_observation")
                seen.add(key)
                reference, uncertainty = obs["flow_kg_s"], obs["uncertainty_kg_s"]
                predicted = solution["flows_kg_s"][branches[key]["flow_element_id"]]
                budget = max(acceptance["absolute_error_kg_s"], acceptance["relative_error"] * reference)
                error = abs(predicted - reference)
                passed = error + uncertainty <= budget
                if uncertainty > budget:
                    sensor_insufficient.append(key)
                elif not passed:
                    violations.append(case["id"] + ":" + key)
                counts[key] += 1
                rows.append({"case_id": case["id"], "branch_id": key, "prediction_kg_s": predicted,
                             "reference_kg_s": reference, "absolute_error_kg_s": error,
                             "reference_uncertainty_kg_s": uncertainty, "budget_kg_s": budget,
                             "status": "pass" if passed else "fail"})
                observed.append((key, reference, uncertainty, predicted))
            if acceptance.get("require_ordering", False):
                for i, left in enumerate(observed):
                    for right in observed[i + 1:]:
                        if abs(left[1] - right[1]) <= left[2] + right[2]:
                            unresolvable += 1
                            continue
                        compared += 1
                        if (left[1] - right[1]) * (left[3] - right[3]) <= 0:
                            violations.append("ordering:" + case["id"] + ":" + left[0] + ":" + right[0])
        missing = [key for key, count in counts.items() if count < minimum]
        status = "fail" if violations else "out_of_scope" if missing or sensor_insufficient or (acceptance.get("require_ordering", False) and not compared) else "pass"
        return {"status": status, "reason": "holdout_error_or_ordering_failed" if violations else "insufficient_independent_evidence" if status == "out_of_scope" else "declared_holdout_cases_passed",
                "evidence_scope": spec["evidence_scope"], "dataset_id": spec["dataset_id"],
                "rows": rows, "violations": violations, "coverage": counts, "unvalidated_branches": missing,
                "sensor_precision_insufficient": sorted(set(sensor_insufficient)),
                "ordering_pairs_checked": compared, "ordering_pairs_unresolvable": unresolvable,
                "dataset_independence": "declared_not_independently_verified", "field_validated": False,
                "hardware_control_eligible": False}
    except (HydraulicError, KeyError, TypeError, ValueError, StopIteration, OverflowError, AttributeError, ZeroDivisionError) as exc:
        return _failure(str(exc))
