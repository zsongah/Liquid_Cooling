"""0.2工程描述和旧版隔离：只读可描述性绝不变成现场写权限。"""
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from lc_control.analysis_config import inspect_analysis_scene, migrate_legacy_scene, validate_analysis_scene
from lc_control.configuration import capability_report, load_scene, validate_scene
from lc_control.site import inspect_scene, topology_errors


ROOT = Path(__file__).resolve().parents[1]


class AnalysisConfigTests(unittest.TestCase):
    def setUp(self):
        self.scene = json.loads((ROOT / "examples/hydraulic_parallel_v02.json").read_text())

    def test_example_is_structural_and_analysis_ready_but_never_controls(self):
        with patch("socket.socket", side_effect=AssertionError("must_not_connect")):
            self.assertIs(validate_scene(self.scene), self.scene)
            report = inspect_scene(self.scene)
        self.assertTrue(report["schema_valid"], report["errors"])
        self.assertTrue(report["analysis_ready"], report["analysis"])
        self.assertFalse(report["execution_ready"])
        self.assertEqual(capability_report(self.scene)[0]["supported_modes"], [])
        self.assertEqual(topology_errors(self.scene), [])
        self.assertTrue(all(node["telemetry_scope"] != "aggregate_cdu" for node in report["topology"]["nodes"]))
        fluid_edges = [e for e in report["topology"]["edges"] if e["kind"] == "fluid"]
        self.assertEqual(len(fluid_edges), len(self.scene["hydraulics"]["elements"]))
        self.assertTrue(all(e["source"].startswith("junction:") for e in fluid_edges))

    def test_multi_cdu_members_do_not_require_fake_devices_or_domains(self):
        self.scene["assets"].append({"id": "CDU_B", "kind": "cdu", "parent_id": "ROOM"})
        self.scene["control_domains"][0]["member_asset_ids"].append("CDU_B")
        self.assertIs(validate_scene(self.scene), self.scene)
        cap = inspect_scene(self.scene)["capabilities"][0]
        self.assertIn("CDU_B", cap["member_asset_ids"])
        self.assertEqual(cap["supported_modes"], [])

    def test_missing_parameters_are_unknown_readiness_not_structural_infeasibility(self):
        del self.scene["hydraulics"]["elements"][0]["parameters"]["resistance_pa_per_kg_s2"]
        del self.scene["hydraulics"]["elements"][0]["parameters"]["linear_pa_per_kg_s"]
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"])
        self.assertFalse(report["analysis_ready"])
        self.assertTrue(any("resistance_parameters_missing" in r for r in report["analysis"]["reasons"]))
        self.assertNotIn("infeasible", json.dumps(report))

    def test_cp_and_unused_viscosity_do_not_block_resistance_only_hydraulics(self):
        del self.scene["fluid_circuits"][0]["specific_heat_j_kg_k"]
        del self.scene["fluid_circuits"][0]["dynamic_viscosity_pa_s"]
        self.assertTrue(inspect_scene(self.scene)["analysis_ready"])
        del self.scene["fluid_circuits"][0]["density_kg_m3"]
        self.assertFalse(inspect_scene(self.scene)["analysis_ready"])

    def test_explicit_null_parameter_is_unknown_even_with_another_positive_term(self):
        self.scene["hydraulics"]["elements"][0]["parameters"]["resistance_pa_per_kg_s2"] = None
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"])
        self.assertFalse(report["capabilities"][0]["analysis_ready"])
        self.assertTrue(any("element_parameter_unknown" in r for r in report["capabilities"][0]["analysis_reasons"]))

    def test_asset_tree_can_have_deep_layers_and_rejects_cycles(self):
        parent = "NODE_A1"
        for n in range(1200):
            aid = "nested_" + str(n)
            self.scene["assets"].append({"id": aid, "kind": "component", "parent_id": parent})
            parent = aid
        self.assertTrue(inspect_scene(self.scene)["valid"])
        self.scene["assets"][0]["parent_id"] = parent
        self.assertTrue(any("asset_parent_cycle" in e for e in inspect_scene(self.scene)["errors"]))

    def test_cross_circuit_mass_connection_is_invalid_even_for_heat_exchanger(self):
        fluid = dict(self.scene["fluid_circuits"][0], id="PRIMARY")
        self.scene["fluid_circuits"].append(fluid)
        self.scene["hydraulics"]["junctions"][0]["circuit_id"] = "PRIMARY"
        self.scene["hydraulics"]["elements"][0]["kind"] = "heat_exchanger"
        self.assertTrue(any("cross_circuit_mass_connection" in e for e in inspect_scene(self.scene)["errors"]))

    def test_nonfinite_boolean_and_invalid_values_are_rejected(self):
        for value in (True, -1, float("nan"), float("inf")):
            scene = copy.deepcopy(self.scene)
            scene["fluid_circuits"][0]["density_kg_m3"] = value
            report = inspect_scene(scene)
            self.assertFalse(report["valid"])
            json.dumps(report, allow_nan=False)
        for value in ([], {}, None):
            scene = copy.deepcopy(self.scene)
            scene["hydraulics"]["elements"][0]["from_junction"] = value
            self.assertFalse(inspect_scene(scene)["valid"])

    def test_pressure_boundary_cannot_reference_command_as_measurement(self):
        self.scene["hydraulics"]["junctions"][0]["pressure_source"] = "setpoint"
        with self.assertRaisesRegex(ValueError, "setpoint_is_not_pressure_boundary"):
            validate_analysis_scene(self.scene)
        self.scene["hydraulics"]["junctions"][0].pop("pressure_source")
        self.scene["hydraulics"]["junctions"][0].pop("evidence_scope")
        self.assertFalse(inspect_scene(self.scene)["valid"])

    def _add_binding(self):
        profile = json.loads((ROOT / "examples/modbus_emulator.json").read_text())["devices"]["CDU_01"]
        self.scene["devices"]["PLC_1"] = profile
        self.scene["points"] = [
            {"id": "DP_TARGET", "asset_ref": "CDU_A", "quantity": "cdu.dp_sp",
             "binding_ref": {"device_id": "PLC_1", "group": "setpoints", "key": "cdu.dp_sp"}},
            {"id": "DP_ACTUAL", "asset_ref": "CDU_A", "quantity": "cdu.sec_dp",
             "binding_ref": {"device_id": "PLC_1", "group": "observations", "key": "cdu.sec_dp"}}]
        self.scene["actuators"] = [{"id": "ACT_DP", "asset_ref": "CDU_A", "element_ref": "HEADER_SUPPLY_LOSS",
            "setpoint_ref": "DP_TARGET", "actual_feedback_ref": "DP_ACTUAL",
            "control_ref": {"device_id": "PLC_1", "quantity": "cdu.dp_sp"},
            "authority_domain_ref": "DOMAIN_SECONDARY"}]
        self.scene["control_domains"][0]["actuator_ids"] = ["ACT_DP"]

    def test_semantic_point_references_existing_single_point_table(self):
        self._add_binding()
        before = copy.deepcopy(self.scene)
        with patch("socket.socket", side_effect=AssertionError("must_not_connect")):
            self.assertTrue(inspect_scene(self.scene)["valid"], inspect_scene(self.scene)["errors"])
        self.assertEqual(self.scene, before)
        self.scene["points"][0]["address"] = 123
        self.assertTrue(any("single_wire_binding" in e for e in inspect_scene(self.scene)["errors"]))

    def test_actuator_cannot_use_target_readback_as_actual_or_duplicate_authority(self):
        self._add_binding()
        self.scene["actuators"][0]["actual_feedback_ref"] = "DP_TARGET"
        self.assertTrue(any("actual_feedback_requires_observation" in e for e in inspect_scene(self.scene)["errors"]))
        self.scene["actuators"][0]["actual_feedback_ref"] = "DP_ACTUAL"
        self.scene["actuators"].append(dict(self.scene["actuators"][0], id="ACT_DUP"))
        self.scene["control_domains"][0]["actuator_ids"].append("ACT_DUP")
        self.assertTrue(any("duplicate_actuator_control_target" in e for e in inspect_scene(self.scene)["errors"]))

    def test_duplicate_physical_write_profile_is_not_new_authority(self):
        self._add_binding()
        self.scene["devices"]["PLC_ALIAS"] = copy.deepcopy(self.scene["devices"]["PLC_1"])
        self.assertTrue(any("overlapping_physical_write_bindings" in e for e in inspect_scene(self.scene)["errors"]))

    def test_branch_tree_cannot_duplicate_or_cycle(self):
        self.scene["branches"][2]["child_branch_ids"] = ["BR_RACK_A"]
        self.assertTrue(any("branch_cycle" in e for e in inspect_scene(self.scene)["errors"]))
        self.scene["branches"][2].pop("child_branch_ids")
        self.scene["branches"].append(dict(self.scene["branches"][2], id="DUP"))
        self.assertTrue(any("duplicate_branch_flow_element" in e for e in inspect_scene(self.scene)["errors"]))

    def test_size_envelope_does_not_mark_structure_or_physics_infeasible(self):
        for n in range(65):
            self.scene["hydraulics"]["junctions"].append({"id": "EXTRA" + str(n), "circuit_id": "SECONDARY", "elevation_m": 0})
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"])
        self.assertFalse(report["analysis_ready"])
        self.assertIn("static_analysis_resource_envelope_exceeded", report["analysis"]["reasons"])

    def test_other_domain_missing_parameters_do_not_block_ready_domain(self):
        self.scene["fluid_circuits"].append({"id": "UNFINISHED", "evidence_scope": "declared"})
        self.scene["hydraulics"]["junctions"].append({"id": "INCOMPLETE", "circuit_id": "UNFINISHED"})
        self.scene["control_domains"].append({"id": "FUTURE", "member_asset_ids": [], "circuit_ids": ["UNFINISHED"], "branch_ids": [], "actuator_ids": []})
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(report["capabilities"][0]["analysis_ready"])
        self.assertFalse(report["capabilities"][1]["analysis_ready"])

    def test_valve_model_requires_measured_or_declared_position_and_curve_range(self):
        element = self.scene["hydraulics"]["elements"][0]
        element.update(kind="valve", parameters={"kv_curve": [{"position": 0, "kv_m3_h": 0}, {"position": 0.8, "kv_m3_h": 5}]}, state={})
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"])
        self.assertFalse(report["analysis_ready"])
        element["state"]["position"] = 0.5
        self.assertTrue(inspect_scene(self.scene)["analysis_ready"])
        element["state"]["position"] = 1.0
        self.assertFalse(inspect_scene(self.scene)["analysis_ready"])
        element["state"]["position"] = -0.1
        self.assertFalse(inspect_scene(self.scene)["valid"])

    def test_pressure_independent_valve_is_ready_only_with_complete_characteristic(self):
        element = self.scene["hydraulics"]["elements"][0]
        element.update(kind="pressure_independent", parameters={}, state={})
        self.assertTrue(inspect_scene(self.scene)["valid"])
        self.assertFalse(inspect_scene(self.scene)["analysis_ready"])
        element["parameters"] = {"flow_setpoint_kg_s": 2.0, "min_dp_pa": 10000, "max_dp_pa": 200000,
                                 "below_min_resistance_pa_per_kg_s2": 2500}
        self.assertTrue(inspect_scene(self.scene)["analysis_ready"])
        element["parameters"]["max_dp_pa"] = 5000
        self.assertFalse(inspect_scene(self.scene)["valid"])

    def test_thermal_coupling_links_isolated_paths_without_adding_mass_edge(self):
        self.scene["fluid_circuits"].append(dict(self.scene["fluid_circuits"][0], id="PRIMARY"))
        for name, pressure in (("PRIMARY_S", 250000), ("PRIMARY_R", 200000)):
            self.scene["hydraulics"]["junctions"].append({"id": name, "circuit_id": "PRIMARY", "elevation_m": 0,
                "pressure_pa": pressure, "evidence_scope": "synthetic", "source": "test boundary"})
        self.scene["hydraulics"]["elements"].append({"id": "HX_PRIMARY", "kind": "heat_exchanger",
            "from_junction": "PRIMARY_S", "to_junction": "PRIMARY_R", "asset_ref": "CDU_A",
            "parameters": {"resistance_pa_per_kg_s2": 10000}, "state": {}})
        self.scene["thermal_couplings"] = [{"id": "HX", "kind": "heat_exchanger", "asset_ref": "CDU_A",
            "sides": [{"circuit_id": "SECONDARY", "element_ids": ["HEADER_SUPPLY_LOSS"]},
                      {"circuit_id": "PRIMARY", "element_ids": ["HX_PRIMARY"]}]}]
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(any("not_thermally_solved" in w for w in report["warnings"]))
        self.scene["thermal_couplings"][0]["sides"][1] = self.scene["thermal_couplings"][0]["sides"][0]
        self.assertFalse(inspect_scene(self.scene)["valid"])

    def test_topology_projection_keeps_asset_and_hydraulic_ids_distinct(self):
        self.scene["assets"].append({"id": "junction:CDU_SUPPLY", "kind": "sensor"})
        report = inspect_scene(self.scene)
        node_ids = [n["id"] for n in report["topology"]["nodes"]]
        self.assertEqual(len(node_ids), len(set(node_ids)))

    def test_migration_preserves_legacy_evidence_without_inventing_pipes(self):
        legacy = load_scene(ROOT / "examples/workbench_physical.json")
        before = copy.deepcopy(legacy)
        with patch("socket.socket", side_effect=AssertionError("must_not_connect")):
            migrated = migrate_legacy_scene(legacy)
        self.assertEqual(legacy, before)
        draft = migrated["scene"]
        self.assertEqual(draft["devices"], legacy["devices"])
        self.assertEqual(draft["assets"], legacy["assets"])
        self.assertEqual(draft["hydraulics"], {"junctions": [], "elements": []})
        self.assertEqual(draft["extensions"]["legacy_source"]["physical_topology"], legacy["physical_topology"])
        self.assertTrue(migrated["migration"]["not_executable"])
        self.assertEqual(len(migrated["migration"]["source_hash"]), 64)
        report = inspect_scene(draft)
        self.assertTrue(report["valid"], report["errors"])
        self.assertFalse(report["analysis_ready"])
        self.assertFalse(report["capabilities"][0]["analysis_ready"])
        self.assertIn("RACK_01_NODE_1", draft["control_domains"][0]["member_asset_ids"])

    def test_migration_keeps_modbus_bindings_and_legacy_checks_unchanged(self):
        legacy = load_scene(ROOT / "examples/modbus_emulator.json")
        draft = migrate_legacy_scene(legacy)["scene"]
        self.assertEqual(draft["devices"]["CDU_01"]["points"], legacy["devices"]["CDU_01"]["points"])
        self.assertTrue(inspect_scene(draft)["valid"])
        del legacy["devices"]["CDU_01"]
        with self.assertRaisesRegex(ValueError, "device_asset_mismatch"):
            validate_scene(legacy)


if __name__ == "__main__":
    unittest.main()
