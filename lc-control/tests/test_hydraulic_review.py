"""独立解析反例的回归：不能把数值收敛、空证据或窄区间当成安全证明。

这里的边界与阻力只是合成数学夹具。测试直接使用串联系统解析解，
没有把求解器自身的输出再次当作期望值，也不访问设备或写运行记录。
"""
import json
import unittest

from lc_control.hydraulics import analyze_scene


def review_scene():
    """两条独立并联阻力，所有物性和压力来源均明确标为合成数据。"""
    return {
        "schema_version": "0.2",
        "scene_id": "hydraulic_review_counterexamples",
        "topology_version": "review-1",
        "assets": [{"id": "CDU", "kind": "cdu"},
                   {"id": "R1", "kind": "rack"},
                   {"id": "R2", "kind": "rack"}],
        "devices": {}, "points": [], "actuators": [], "thermal_couplings": [],
        "fluid_circuits": [{
            "id": "C", "density_kg_m3": 1000.0,
            "dynamic_viscosity_pa_s": 0.001, "specific_heat_j_kg_k": 4180.0,
            "source": "Analytic review fixture", "evidence_scope": "synthetic",
        }],
        "hydraulics": {
            "junctions": [
                {"id": "S", "circuit_id": "C", "elevation_m": 0,
                 "pressure_pa": 100000.0, "pressure_uncertainty_pa": 0,
                 "source": "Analytic review fixture", "evidence_scope": "synthetic"},
                {"id": "T", "circuit_id": "C", "elevation_m": 0,
                 "pressure_pa": 0.0, "pressure_uncertainty_pa": 0,
                 "source": "Analytic review fixture", "evidence_scope": "synthetic"},
            ],
            "elements": [
                {"id": "E1", "kind": "resistance", "from_junction": "S",
                 "to_junction": "T", "asset_ref": "R1",
                 "parameters": {"resistance_pa_per_kg_s2": 100000.0,
                                "relative_uncertainty": 0}},
                {"id": "E2", "kind": "resistance", "from_junction": "S",
                 "to_junction": "T", "asset_ref": "R2",
                 "parameters": {"resistance_pa_per_kg_s2": 400000.0,
                                "relative_uncertainty": 0}},
            ],
        },
        "branches": [
            {"id": "B1", "asset_ref": "R1", "flow_element_id": "E1", "min_flow_kg_s": 0.7},
            {"id": "B2", "asset_ref": "R2", "flow_element_id": "E2", "min_flow_kg_s": 0.7},
        ],
        "control_domains": [{
            "id": "D", "member_asset_ids": ["CDU", "R1", "R2"],
            "circuit_ids": ["C"], "branch_ids": ["B1", "B2"], "actuator_ids": [],
        }],
    }


def empty_validation():
    return {
        "dataset_id": "declared_holdout", "calibration_dataset_id": "declared_fit",
        "evidence_scope": "synthetic",
        "acceptance": {"absolute_error_kg_s": 0.01, "relative_error": 0.01,
                       "minimum_cases_per_branch": 1},
        "cases": [{"id": "empty_case", "observations": []}],
    }


class HydraulicReviewRegressionTests(unittest.TestCase):
    def test_check_valve_at_cracking_threshold_does_not_invent_unique_dead_end_pressure(self):
        value = review_scene()
        value["hydraulics"]["junctions"][1]["pressure_pa"] = 99800.0
        value["hydraulics"]["junctions"].append(
            {"id": "J", "circuit_id": "C", "elevation_m": 0})
        valve = value["hydraulics"]["elements"][0]
        valve.update(kind="check_valve", to_junction="J")
        valve["parameters"]["cracking_pressure_pa"] = 100.0

        # J 是只有入口止回阀的盲端；任意 J >= 99900 Pa 都满足 q=0。
        # 初始化中点刚好等于 99900 Pa。跨开闭状态的中心差分会给出正斜率，
        # 但这个数值导数不能证明唯一性，更不能制造一个确定的盲端压力。
        result = analyze_scene(value)
        self.assertEqual(result["solver"]["status"], "failed", result)
        self.assertEqual(result["solver"]["reason"], "pressure_state_not_unique")
        self.assertFalse(any(j["id"] == "J" and j.get("estimated_pressure_pa") is not None
                             for j in result["junctions"]))
        self.assertTrue(all(branch["status"] == "unknown" for branch in result["branches"]))
        self.assertFalse(result["hardware_writes"])

    def test_no_target_branches_cannot_pass_holdout_validation(self):
        value = review_scene()
        value["control_domains"][0]["branch_ids"] = []
        value["analysis"] = {"validation": empty_validation()}
        result = analyze_scene(value)
        self.assertEqual(result["solver"]["status"], "converged", result)
        self.assertEqual(result["validation"]["status"], "out_of_scope")
        self.assertEqual(result["validation"]["reason"], "validation_target_branches_required")
        self.assertFalse(result["validation"]["field_validated"])

    def test_empty_observations_cannot_pass_holdout_validation(self):
        value = review_scene()
        value["analysis"] = {"validation": empty_validation()}
        result = analyze_scene(value)
        self.assertEqual(result["solver"]["status"], "converged", result)
        self.assertEqual(result["validation"]["status"], "out_of_scope")
        self.assertEqual(result["validation"]["reason"], "bounded_validation_observations_required")
        self.assertFalse(result["validation"]["field_validated"])

    def test_large_pressure_span_enclosure_contains_independent_series_solution(self):
        for pressure, second_resistance in ((1e8, 30000.0), (1e9, 20000.0)):
            with self.subTest(pressure_pa=pressure, resistance=second_resistance):
                value = review_scene()
                value["hydraulics"]["junctions"][0]["pressure_pa"] = pressure
                value["hydraulics"]["junctions"].append(
                    {"id": "J", "circuit_id": "C", "elevation_m": 0})
                first, second = value["hydraulics"]["elements"]
                first["to_junction"], second["from_junction"] = "J", "J"
                first["parameters"] = {"linear_pa_per_kg_s": 10000.0, "relative_uncertainty": 0}
                second["parameters"] = {"linear_pa_per_kg_s": second_resistance,
                                        "relative_uncertainty": 0}
                # 串联线性阻力解析解 q = Δp / (R1 + R2)。需求刚好等于真值。
                # 32 次二分后取中点 ±0.01 Pa 曾排除该根，并误报确定缺流。
                exact_flow = pressure / (10000.0 + second_resistance)
                value["branches"][0]["min_flow_kg_s"] = exact_flow
                result = analyze_scene(value)
                self.assertEqual(result["solver"]["status"], "converged", result)
                for branch in result["branches"]:
                    interval = branch["flow_interval_kg_s"]
                    self.assertIsNotNone(interval, branch)
                    self.assertLessEqual(interval[0], exact_flow, branch)
                    self.assertGreaterEqual(interval[1], exact_flow, branch)
                    self.assertAlmostEqual(branch["estimated_flow_kg_s"], exact_flow, delta=1e-7)
                self.assertNotEqual(result["branches"][0]["status"], "violated")

    def test_finite_elevation_with_invalid_head_returns_serializable_failure(self):
        for elevation in (1e308, 1e9):
            with self.subTest(elevation_m=elevation):
                value = review_scene()
                # 孤立的定压点仍会出现在报告中；rho*g*z 不能产生 inf 后通过
                # inf-inf 抵消成 NaN，也不能突破声明的数值支持范围。
                value["hydraulics"]["junctions"].append({
                    "id": "ISOLATED_FIXED", "circuit_id": "C", "elevation_m": elevation,
                    "pressure_pa": 0, "pressure_uncertainty_pa": 0,
                    "source": "Finite synthetic extreme", "evidence_scope": "synthetic",
                })
                result = analyze_scene(value)
                self.assertEqual(result["solver"]["status"], "not_run", result)
                self.assertEqual(result["solver"]["reason"],
                                 "elevation_head_outside_numeric_envelope:ISOLATED_FIXED")
                self.assertEqual(result["readiness"]["status"], "out_of_scope")
                json.dumps(result, allow_nan=False)
                self.assertFalse(result["hardware_writes"])


if __name__ == "__main__":
    unittest.main()
