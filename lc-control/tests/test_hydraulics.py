"""以独立解析答案、不可辨识反例和故障情形验证只读分析，非实现镜像测试。"""
import copy
import json
import math
from pathlib import Path
import random
import unittest
from unittest.mock import patch

from lc_control.hydraulics import analyze_scene, assess_interval, flow_enclosures, solve_network
from lc_control.hydraulic_evidence import identifiability_precheck, validate_predictions


ROOT = Path(__file__).resolve().parents[1]


def scene():
    return {"schema_version": "0.2", "scene_id": "analytic", "topology_version": "t1",
        "assets": [{"id": "CDU", "kind": "cdu"}, {"id": "R1", "kind": "rack"}, {"id": "R2", "kind": "rack"}],
        "devices": {}, "points": [], "actuators": [], "thermal_couplings": [],
        "fluid_circuits": [{"id": "C", "density_kg_m3": 1000, "dynamic_viscosity_pa_s": 0.001,
            "specific_heat_j_kg_k": 4180, "source": "analytic fixture", "evidence_scope": "synthetic"}],
        "hydraulics": {"junctions": [
            {"id": "S", "circuit_id": "C", "elevation_m": 0, "pressure_pa": 100000,
             "pressure_uncertainty_pa": 0, "source": "analytic fixture", "evidence_scope": "synthetic"},
            {"id": "T", "circuit_id": "C", "elevation_m": 0, "pressure_pa": 0,
             "pressure_uncertainty_pa": 0, "source": "analytic fixture", "evidence_scope": "synthetic"}],
            "elements": [
                {"id": "E1", "kind": "resistance", "from_junction": "S", "to_junction": "T", "asset_ref": "R1",
                 "parameters": {"resistance_pa_per_kg_s2": 100000, "relative_uncertainty": 0}},
                {"id": "E2", "kind": "resistance", "from_junction": "S", "to_junction": "T", "asset_ref": "R2",
                 "parameters": {"resistance_pa_per_kg_s2": 400000, "relative_uncertainty": 0}}]},
        "branches": [{"id": "B1", "asset_ref": "R1", "flow_element_id": "E1", "min_flow_kg_s": 0.7},
                     {"id": "B2", "asset_ref": "R2", "flow_element_id": "E2", "min_flow_kg_s": 0.7}],
        "control_domains": [{"id": "D", "member_asset_ids": ["CDU", "R1", "R2"],
                             "circuit_ids": ["C"], "branch_ids": ["B1", "B2"], "actuator_ids": []}]}


def shared_scene():
    value = scene()
    value["hydraulics"]["junctions"].append({"id": "J", "circuit_id": "C", "elevation_m": 0})
    for e in value["hydraulics"]["elements"]:
        e["from_junction"] = "J"
    value["hydraulics"]["elements"].append({"id": "H", "kind": "resistance", "from_junction": "S", "to_junction": "J",
        "parameters": {"resistance_pa_per_kg_s2": 10000, "relative_uncertainty": 0}})
    return value


def identification(total_only):
    observations = [{"element_ids": ["E1", "E2"], "uncertainty_kg_s": 0.001}] if total_only else [
        {"element_ids": ["E1"], "uncertainty_kg_s": 0.001}, {"element_ids": ["E2"], "uncertainty_kg_s": 0.001}]
    return {"parameters": [{"element_id": "E1", "name": "resistance_pa_per_kg_s2"},
                           {"element_id": "E2", "name": "resistance_pa_per_kg_s2"}],
            "experiments": [{"id": "c1", "boundary_pressures_pa": {"S": 100000}, "observations": observations},
                            {"id": "c2", "boundary_pressures_pa": {"S": 25000}, "observations": observations}],
            "rank_rtol": 1e-7, "max_condition": 10000, "max_relative_interval_width": 0.1}


class HydraulicTests(unittest.TestCase):
    def test_pressure_independent_available_pressure_and_inconsistent_curve(self):
        s = scene()
        e = s["hydraulics"]["elements"][0]
        e.update(kind="pressure_independent", parameters={"flow_setpoint_kg_s": 1.0,
            "min_dp_pa": 50000, "max_dp_pa": 200000,
            "below_min_resistance_pa_per_kg_s2": 50000})
        solved = solve_network(s)
        self.assertEqual(solved["flows_kg_s"]["E1"], 1)
        self.assertEqual(solved["element_states"]["E1"], "pressure_regulated")
        s["hydraulics"]["junctions"][0]["pressure_pa"] = 12500
        solved = solve_network(s)
        self.assertAlmostEqual(solved["flows_kg_s"]["E1"], .5)
        self.assertEqual(solved["element_states"]["E1"], "below_working_pressure")
        e["parameters"]["below_min_resistance_pa_per_kg_s2"] = 100000
        rejected = solve_network(s)
        self.assertEqual(rejected["status"], "failed")
        self.assertIn("inconsistent_pressure_independent", rejected["reason"])

    def test_pressure_driven_parallel_detects_underflow_not_forced_demand(self):
        with patch("socket.socket", side_effect=AssertionError("analysis must not connect")):
            report = analyze_scene(scene())
        self.assertEqual(report["solver"]["status"], "converged")
        self.assertAlmostEqual(report["branches"][0]["estimated_flow_kg_s"], 1)
        self.assertAlmostEqual(report["branches"][1]["estimated_flow_kg_s"], 0.5)
        self.assertEqual([b["status"] for b in report["branches"]], ["satisfied", "violated"])
        self.assertFalse(report["hardware_writes"])
        self.assertFalse(report["validation"]["field_validated"])

    def test_shared_header_matches_independent_series_parallel_formula(self):
        solved = solve_network(shared_scene())
        total = math.sqrt(100000 / (10000 + (1 / math.sqrt(100000) + 1 / math.sqrt(400000)) ** -2))
        self.assertEqual(solved["status"], "converged", solved)
        self.assertAlmostEqual(solved["flows_kg_s"]["H"], total, places=6)
        self.assertAlmostEqual(solved["flows_kg_s"]["E1"], total * 2 / 3, places=6)
        self.assertLess(solved["residuals"]["mass_kg_s"], 1e-7)

    def test_changing_one_branch_changes_other_branch_under_shared_head(self):
        s = shared_scene()
        before = solve_network(s)["flows_kg_s"]
        s["hydraulics"]["elements"][0]["parameters"]["resistance_pa_per_kg_s2"] *= 4
        after = solve_network(s)["flows_kg_s"]
        self.assertLess(after["E1"], before["E1"])
        self.assertGreater(after["E2"], before["E2"])

    def test_interval_limit_direction_and_crossing(self):
        self.assertEqual(assess_interval([9, 11], minimum=10), "unknown")
        self.assertEqual(assess_interval([7, 9], minimum=10), "violated")
        self.assertEqual(assess_interval([11, 12], minimum=10), "satisfied")
        self.assertEqual(assess_interval([95, 105], maximum=100), "unknown")
        self.assertEqual(assess_interval([105, 110], maximum=100), "violated")

    def test_interval_encloses_analytic_pressure_and_resistance_extrema(self):
        s = scene()
        s["hydraulics"]["junctions"][0]["pressure_uncertainty_pa"] = 2000
        s["hydraulics"]["elements"][0]["parameters"]["relative_uncertainty"] = 0.1
        lo, hi = flow_enclosures(s)["E1"]
        self.assertLessEqual(lo, math.sqrt(98000 / 110000))
        self.assertGreaterEqual(hi, math.sqrt(102000 / 90000))
        self.assertLess(hi - lo, 0.13)

    def test_network_bounds_cover_sampled_resistance_and_boundary_variations(self):
        # 样本覆盖是回归测试，不能被报告为全参数空间证明。
        s = shared_scene()
        for e in s["hydraulics"]["elements"]:
            e["parameters"]["relative_uncertainty"] = 0.1
        s["hydraulics"]["junctions"][0]["pressure_uncertainty_pa"] = 1000
        bounds = flow_enclosures(s)
        rng = random.Random(241)
        for _ in range(30):
            sample = copy.deepcopy(s)
            sample["hydraulics"]["junctions"][0]["pressure_pa"] += rng.uniform(-1000, 1000)
            for e in sample["hydraulics"]["elements"]:
                e["parameters"]["resistance_pa_per_kg_s2"] *= rng.uniform(0.9, 1.1)
            solved = solve_network(sample)
            self.assertEqual(solved["status"], "converged", solved)
            for key, flow in solved["flows_kg_s"].items():
                self.assertLessEqual(bounds[key][0], flow)
                self.assertGreaterEqual(bounds[key][1], flow)

    def test_missing_uncertainty_does_not_invent_exact_observations(self):
        s = scene()
        del s["hydraulics"]["elements"][0]["parameters"]["relative_uncertainty"]
        result = analyze_scene(s)
        self.assertEqual(result["solver"]["status"], "converged")
        self.assertTrue(all(b["status"] == "unknown" for b in result["branches"]))

    def test_near_closed_and_closed_valve_do_not_invent_minimum_flow(self):
        for opening in (1e-8, 0):
            s = scene()
            s["hydraulics"]["elements"][0].update(kind="valve", parameters={"kv_m3_h": 1, "characteristic": "linear", "relative_uncertainty": 0}, state={"position": opening})
            r = analyze_scene(s)
            self.assertEqual(r["solver"]["status"], "converged", r)
            self.assertLess(r["branches"][0]["estimated_flow_kg_s"], 1e-7)
            self.assertEqual(r["branches"][0]["status"], "violated")

    def test_valve_curve_without_redundant_nominal_kv(self):
        s = scene()
        s["hydraulics"]["elements"][0].update(kind="valve", parameters={"kv_curve": [{"position": 0, "kv_m3_h": 0}, {"position": 1, "kv_m3_h": 3.6}], "relative_uncertainty": 0}, state={"position": 1})
        r = analyze_scene(s)
        self.assertAlmostEqual(r["branches"][0]["estimated_flow_kg_s"], 1)

    def test_check_valve_reverse_pressure_is_zero_not_reversed_flow(self):
        s = scene()
        s["hydraulics"]["elements"][0].update(kind="check_valve", from_junction="T", to_junction="S")
        r = analyze_scene(s)
        self.assertEqual(r["branches"][0]["estimated_flow_kg_s"], 0)
        self.assertEqual(r["branches"][0]["status"], "violated")

    def test_closed_island_does_not_fabricate_pressure(self):
        s = shared_scene()
        for e in s["hydraulics"]["elements"]:
            e["state"] = {"closed": True}
        result = solve_network(s)
        self.assertEqual(result["status"], "failed")
        self.assertIn("unreferenced_pressure_component", result["reason"])
        self.assertEqual(result["pressures_pa"], {})

    def test_missing_elevation_is_not_silently_zero(self):
        s = scene()
        del s["hydraulics"]["junctions"][0]["elevation_m"]
        report = analyze_scene(s)
        self.assertEqual(report["solver"]["status"], "not_run")

    def test_unknown_pressure_and_extreme_finite_input_serialize_without_nan(self):
        for pressure in (None, 1e308):
            s = scene()
            s["hydraulics"]["junctions"][0]["pressure_pa"] = pressure
            report = analyze_scene(s)
            json.dumps(report, allow_nan=False)
            self.assertNotEqual(report["solver"]["status"], "converged")

    def test_pipe_laminar_matches_hagen_poiseuille(self):
        s = scene()
        s["hydraulics"]["junctions"][0]["pressure_pa"] = 0.1
        s["hydraulics"]["elements"][0].update(kind="pipe", parameters={"length_m": 2, "diameter_m": 0.01, "roughness_m": 0, "minor_loss_k": 0, "relative_uncertainty": 0})
        r = solve_network(s)
        expected = 0.1 * math.pi * 0.01 ** 4 * 1000 / (128 * 0.001 * 2)
        self.assertAlmostEqual(r["flows_kg_s"]["E1"], expected, places=9)

    def test_size_limit_is_not_unbounded_python_solver(self):
        s = scene()
        s["hydraulics"]["elements"] *= 129
        self.assertIn("size_limit", solve_network(s)["reason"])

    def test_identifiability_total_only_fails_despite_perfect_total_predictions(self):
        s = scene()
        s["analysis"] = {"identifiability": identification(True)}
        r = identifiability_precheck(s, "D")
        self.assertEqual(r["status"], "fail", r)
        self.assertEqual(r["effective_rank"], 1)
        self.assertEqual(r["requested_calibration_scope"], "out_of_scope")

    def test_identifiability_branch_measurements_pass_local_not_global(self):
        s = scene()
        s["analysis"] = {"identifiability": identification(False)}
        r = identifiability_precheck(s, "D")
        self.assertEqual(r["status"], "pass", r)
        self.assertEqual(r["effective_rank"], 2)
        self.assertFalse(r["global_uniqueness_proven"])

    def test_high_measurement_uncertainty_fails_practical_gate(self):
        s = scene()
        spec = identification(False)
        for e in spec["experiments"]:
            for obs in e["observations"]:
                obs["uncertainty_kg_s"] = 1
        s["analysis"] = {"identifiability": spec}
        r = identifiability_precheck(s, "D")
        self.assertEqual(r["status"], "fail")
        self.assertEqual(r["effective_rank"], 2)

    def test_holdout_correct_order_and_error_is_not_field_authorization(self):
        s = scene()
        spec = {"evidence_scope": "synthetic", "dataset_id": "holdout", "calibration_dataset_id": "fit",
            "acceptance": {"absolute_error_kg_s": 0.02, "relative_error": 0.01, "minimum_cases_per_branch": 1, "require_ordering": True},
            "cases": [{"id": "case1", "boundary_pressures_pa": {"S": 25000}, "observations": [
                {"branch_id": "B1", "flow_kg_s": 0.5, "uncertainty_kg_s": 0.001},
                {"branch_id": "B2", "flow_kg_s": 0.25, "uncertainty_kg_s": 0.001}]}]}
        s["analysis"] = {"validation": spec}
        result = validate_predictions(s, "D")
        self.assertEqual(result["status"], "pass", result)
        self.assertFalse(result["hardware_control_eligible"])
        spec["cases"][0]["observations"][1]["flow_kg_s"] = 0.6
        self.assertEqual(validate_predictions(s, "D")["status"], "fail")
        spec["dataset_id"] = "fit"
        self.assertEqual(validate_predictions(s, "D")["status"], "out_of_scope")

    def test_malformed_evidence_is_out_of_scope_not_crash(self):
        for malformed in ([], {"identifiability": []}, {"validation": []}, {"identifiability": {"parameters": [None]}}):
            s = scene()
            s["analysis"] = malformed
            json.dumps(identifiability_precheck(s, "D"), allow_nan=False)
            json.dumps(validate_predictions(s, "D"), allow_nan=False)

    def test_shipped_example_has_all_three_interval_outcomes(self):
        s = json.loads((ROOT / "examples/hydraulic_parallel_v02.json").read_text())
        report = analyze_scene(s)
        self.assertEqual(report["solver"]["status"], "converged", report)
        self.assertIn("violated", [b["status"] for b in report["branches"]])
        self.assertIn("satisfied", [b["status"] for b in report["branches"]])


if __name__ == "__main__":
    unittest.main()
