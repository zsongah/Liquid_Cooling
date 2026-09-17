"""通用配置边界的行为测试：错误拓扑不得因为能画出来而获得控制能力。"""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from lc_control.configuration import load_scene, validate_scene
from lc_control.engine import Engine
from lc_control.operations import simulate
from lc_control.registry import create_adapter, create_policy, registry_report
from lc_control.site import inspect_scene


ROOT = Path(__file__).resolve().parents[1]


class SiteTests(unittest.TestCase):
    def setUp(self):
        self.scene = load_scene(ROOT / "examples/workbench_physical.json")

    def test_legacy_is_service_map_not_physical_pipes(self):
        scene = load_scene(ROOT / "examples/thermal_liquid_to_liquid.json")
        with patch("socket.socket", side_effect=AssertionError("inspection_must_not_connect")):
            report = inspect_scene(scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["topology"]["kind"], "service_relations")
        self.assertFalse(any(e["kind"] == "fluid" for e in report["topology"]["edges"]))
        self.assertEqual(report["capabilities"][0]["policy_kind"], "flow")

    def test_physical_example_runs_aggregate_model_without_invented_branch_values(self):
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["topology"]["evidence"], "illustrative")
        self.assertEqual(len([e for e in report["topology"]["edges"] if e["kind"] == "fluid"]), 8)
        self.assertEqual(report["capabilities"][0]["supported_modes"], ["monitor", "shadow", "control"])
        self.assertTrue(report["capabilities"][0]["can_thermal_sim"])
        with tempfile.TemporaryDirectory() as directory:
            result = simulate(self.scene, directory, seconds=30, interval=5)
        self.assertEqual(result["mode_before_shutdown"], "control")
        self.assertEqual(result["source"], "synthetic_plant")
        for node in report["topology"]["nodes"]:
            if node["kind"] != "cdu":
                self.assertEqual(node["telemetry_scope"], "configuration_only")

    def test_two_independent_devices_select_different_control_quantities_with_same_engine(self):
        """同一软件包仅更换域配置，可实际运行压差和流量两种聚合控制。"""
        scene = load_scene(ROOT / "examples/workbench_independent_cdus.json")
        report = inspect_scene(scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["topology"]["kind"], "service_relations")
        self.assertEqual([c["policy_control"] for c in report["capabilities"]],
                         ["cdu.dp_sp", "cdu.sec_flow_sp"])
        with tempfile.TemporaryDirectory() as directory:
            for domain in scene["control_domains"]:
                profile = scene["devices"][domain["cdu_id"]]
                adapter = create_adapter(domain["cdu_id"], profile, domain["owner"])
                engine = Engine(scene, domain["id"], adapter, Path(directory) / domain["id"], "control")
                try:
                    for now in (5, 10, 15, 20):
                        adapter.advance(5, 75000)
                        decision = engine.tick(now)
                        self.assertNotIn("error", decision)
                    self.assertEqual(engine.service.mode, "control")
                    self.assertTrue(adapter.writes)
                    self.assertTrue(all(request.quantity == profile["policy"]["control_quantity"]
                                        and request.asset_id == domain["cdu_id"] for request in adapter.writes))
                finally:
                    engine.close()

    def test_heat_sink_type_does_not_select_dp_or_flow_control(self):
        """L2L / L2A 各自都能使用 DP 或流量 Profile，不把示例搭配当成产品限制。"""
        for filename in ("thermal_liquid_to_liquid.json", "thermal_liquid_to_air.json"):
            for device_type, sink_kind in (("liquid_to_liquid", "facility_water"), ("liquid_to_air", "room_air")):
                scene = load_scene(ROOT / "examples" / filename)
                profile = scene["devices"]["CDU_01"]
                profile["type"] = device_type
                next(a for a in scene["assets"] if a["id"] == "SINK_01")["kind"] = sink_kind
                report = inspect_scene(scene)
                self.assertTrue(report["valid"], report["errors"])
                self.assertTrue(report["capabilities"][0]["can_thermal_sim"])
                with tempfile.TemporaryDirectory() as directory:
                    adapter = create_adapter("CDU_01", profile, "lc-core")
                    engine = Engine(scene, "CDU_DOMAIN_01", adapter, directory, "control")
                    try:
                        for now in (5, 10, 15, 20):
                            adapter.advance(5, 75000)
                            self.assertNotIn("error", engine.tick(now))
                        self.assertTrue(adapter.writes)
                        self.assertTrue(all(r.quantity == profile["policy"]["control_quantity"] for r in adapter.writes))
                    finally:
                        engine.close()

    def test_plate_exchanger_fluid_cross_connection_is_rejected(self):
        self.scene["physical_topology"]["connections"][0]["to_port"] = "trunk_CDU_01_supply"
        report = inspect_scene(self.scene)
        self.assertFalse(report["valid"])
        self.assertTrue(any("primary_secondary_fluid_cross_connection" in e for e in report["errors"]))
        self.assertEqual(report["capabilities"][0]["supported_modes"], [])
        with self.assertRaisesRegex(ValueError, "primary_secondary_fluid_cross_connection"):
            validate_scene(self.scene)

    def test_missing_return_and_supply_to_return_short_circuit_are_rejected(self):
        self.scene["physical_topology"]["connections"].pop()
        self.assertTrue(any("supply_return_pair_required" in e for e in inspect_scene(self.scene)["errors"]))
        self.setUp()
        self.scene["physical_topology"]["connections"][0]["to_port"] = "primary_CDU_01_return"
        self.assertTrue(any("supply_return_short_circuit" in e for e in inspect_scene(self.scene)["errors"]))

    def test_unknown_port_parent_and_cycles_have_explicit_diagnostics(self):
        self.scene["physical_topology"]["connections"][0]["from_port"] = "UNKNOWN"
        self.assertTrue(any("unknown_connection_port" in e for e in inspect_scene(self.scene)["errors"]))
        self.setUp()
        node = next(a for a in self.scene["assets"] if a["kind"] == "node")
        node["parent_id"] = "MISSING"
        self.assertTrue(any("unknown_parent" in e for e in inspect_scene(self.scene)["errors"]))
        node["parent_id"] = node["id"]
        self.assertTrue(any("asset_parent_cycle" in e for e in inspect_scene(self.scene)["errors"]))

    def test_nonfinite_layout_and_hidden_extension_are_rejected(self):
        self.scene["physical_topology"]["layout"]["CDU_01"]["x"] = float("nan")
        report = inspect_scene(self.scene)
        self.assertFalse(report["valid"])
        json.dumps(report, allow_nan=False)
        self.setUp()
        self.scene["extensions"]["hidden_parameter"] = float("inf")
        with self.assertRaisesRegex(ValueError, "nonfinite_number"):
            validate_scene(self.scene)

    def test_unknown_adapter_and_policy_cannot_run(self):
        for field, value in (("adapter", "vendor_magic"), ("policy", {"kind": "mpc"})):
            scene = copy.deepcopy(self.scene)
            scene["devices"]["CDU_01"][field] = value
            report = inspect_scene(scene)
            self.assertFalse(report["valid"])
            self.assertEqual(report["capabilities"][0]["supported_modes"], [])
        self.assertRaises(ValueError, create_policy, {"kind": "untrusted.module:Factory"})

    def test_missing_policy_observation_downgrades_to_monitor(self):
        profile = self.scene["devices"]["CDU_01"]
        # 协议替身仍可监测缺测数据；热工对象缺少自身状态则不应声明可执行。
        profile["adapter"] = "mock"
        del profile["guards"]["cdu.sec_flow"]
        del profile["initial_observations"]["cdu.sec_flow"]
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"])
        capability = report["capabilities"][0]
        self.assertIn("cdu.sec_flow", capability["missing_observations"])
        self.assertEqual(capability["supported_modes"], ["monitor"])
        self.assertFalse(capability["can_thermal_sim"])

    def test_standard_cdu_measurement_cannot_be_relabelled_as_node_data(self):
        point = self.scene["devices"]["CDU_01"]["initial_observations"]["cdu.sec_supply_temp"]
        point["asset_id"] = "RACK_01_NODE_1"
        self.assertTrue(any("measurement_asset_mismatch" in e for e in inspect_scene(self.scene)["errors"]))
        point["asset_id"] = "CDU_01"
        point["port_id"] = "primary_CDU_01_supply"
        self.assertTrue(any("measurement_side_mismatch" in e for e in inspect_scene(self.scene)["errors"]))

    def _second_cdu(self):
        self.scene["assets"].append({"id": "CDU_02", "kind": "cdu"})
        self.scene["devices"]["CDU_02"] = copy.deepcopy(self.scene["devices"]["CDU_01"])
        for point in self.scene["devices"]["CDU_02"]["initial_observations"].values():
            point["asset_id"] = "CDU_02"
        domain = dict(self.scene["control_domains"][0], id="DOMAIN_02", cdu_id="CDU_02")
        self.scene["control_domains"].append(domain)
        return domain

    def test_same_rack_cannot_be_independently_counted_and_controlled_twice(self):
        self._second_cdu()
        report = inspect_scene(self.scene)
        self.assertFalse(report["valid"])
        self.assertTrue(any("duplicate_load_assignment_requires_coordinator" in e for e in report["errors"]))

    def test_shared_topology_can_be_described_but_not_independently_controlled(self):
        self._second_cdu()
        # 服务关系可以表达共享负荷；完整物理图还需要另补 CDU_02 的管路。
        self.scene.pop("physical_topology")
        for domain in self.scene["control_domains"]:
            domain["shared_hydraulics"] = True
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"], report["errors"])
        for capability in report["capabilities"]:
            self.assertNotIn("control", capability["supported_modes"])
            self.assertIn("shared_hydraulics_coordinator_not_implemented", capability["reasons"])

    def test_service_relationship_cannot_contradict_declared_physical_pipes(self):
        self.scene["assets"].append({"id": "UNCONNECTED_RACK", "kind": "rack"})
        self.scene["control_domains"][0]["served_racks"].append("UNCONNECTED_RACK")
        report = inspect_scene(self.scene)
        self.assertFalse(report["valid"])
        self.assertTrue(any("served_rack_not_connected_to_secondary" in e for e in report["errors"]))

    def test_same_manifold_asset_cannot_bridge_isolated_loop_ids(self):
        for port in self.scene["physical_topology"]["ports"]:
            if port["id"].startswith("branch_a_"):
                port["loop_id"] = "ISOLATED_TCS"
        report = inspect_scene(self.scene)
        self.assertFalse(report["valid"])
        self.assertTrue(any("served_rack_not_connected_to_secondary:RACK_01" in e for e in report["errors"]))

    def test_separate_supply_return_headers_and_one_way_valve_path_are_supported(self):
        scene = load_scene(ROOT / "examples/workbench_split_headers.json")
        report = inspect_scene(scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertTrue(report["capabilities"][0]["can_thermal_sim"])
        self.assertEqual(report["topology"]["solver"], "not_implemented")
        valve = next(n for n in report["topology"]["nodes"] if n["id"] == "VALVE_A")
        self.assertEqual(valve["telemetry_scope"], "configuration_only")
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(simulate(scene, directory, seconds=20)["mode_before_shutdown"], "control")
        # 拆开总管后仍必须存在返回 CDU 的完整路径，不能只凭 loop_id 相同通过。
        return_line = next(c for c in scene["physical_topology"]["connections"] if c["id"] == "trunk_return")
        return_line["from_port"], return_line["to_port"] = return_line["to_port"], return_line["from_port"]
        report = inspect_scene(scene)
        self.assertFalse(report["valid"])
        self.assertTrue(any("supply_return_pair_required" in e for e in report["errors"]))

    def test_supply_return_arrows_must_describe_opposite_physical_paths(self):
        connection = self.scene["physical_topology"]["connections"][1]
        connection["from_port"], connection["to_port"] = connection["to_port"], connection["from_port"]
        self.assertTrue(any("supply_return_flow_orientation_mismatch" in e for e in inspect_scene(self.scene)["errors"]))

    def test_liquid_to_air_physical_graph_shows_air_heat_dependency_without_liquid_pipe(self):
        self.scene["devices"]["CDU_01"]["type"] = "liquid_to_air"
        next(a for a in self.scene["assets"] if a["id"] == "SINK_01")["kind"] = "room_air"
        physical = self.scene["physical_topology"]
        physical["ports"] = [p for p in physical["ports"] if p["side"] != "primary"]
        physical["connections"] = [c for c in physical["connections"] if not c["id"].startswith("primary_")]
        report = inspect_scene(self.scene)
        self.assertTrue(report["valid"], report["errors"])
        sink_edges = [e for e in report["topology"]["edges"] if e["target"] == "SINK_01"]
        self.assertEqual(len(sink_edges), 1)
        self.assertEqual(sink_edges[0]["kind"], "heat_dependency")

    def test_physical_shared_secondary_cannot_hide_as_independent_domains(self):
        domain = self._second_cdu()
        domain["served_racks"] = []
        physical = self.scene["physical_topology"]
        for direction in ("supply", "return"):
            pid = "CDU_02_" + direction
            physical["ports"].append({"id": pid, "asset_id": "CDU_02", "side": "secondary",
                                      "direction": direction, "loop_id": "TCS_01"})
            physical["connections"].append({"id": pid + "_link", "from_port": pid,
                                             "to_port": "trunk_MANIFOLD_01_" + direction})
        report = inspect_scene(self.scene)
        self.assertTrue(any("shared_secondary_connection_requires_coordinator" in e for e in report["errors"]))

    def test_hardware_inspection_never_connects_or_claims_commissioning(self):
        scene = load_scene(ROOT / "examples/modbus_emulator.json")
        with patch("socket.socket", side_effect=AssertionError("must_not_connect")):
            report = inspect_scene(scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["capabilities"][0]["commissioning"], "declared_unverified")
        self.assertNotIn("control", report["capabilities"][0]["supported_modes"])

    def test_hardware_legacy_simulation_values_cannot_mask_unmapped_real_observation(self):
        scene = load_scene(ROOT / "examples/modbus_emulator.json")
        profile = scene["devices"]["CDU_01"]
        profile["initial_observations"] = copy.deepcopy(self.scene["devices"]["CDU_01"]["initial_observations"])
        profile["guards"].pop("cdu.sec_flow")
        profile["points"]["observations"].pop("cdu.sec_flow")
        report = inspect_scene(scene)
        self.assertTrue(report["valid"], report["errors"])
        self.assertEqual(report["capabilities"][0]["supported_modes"], ["monitor"])
        self.assertIn("cdu.sec_flow", report["capabilities"][0]["missing_observations"])

    def test_hardware_missing_status_and_wrong_owner_cannot_offer_shadow(self):
        for mutation in ("missing_alarm", "wrong_owner", "wrong_mode"):
            scene = load_scene(ROOT / "examples/modbus_emulator.json")
            status = scene["devices"]["CDU_01"]["points"]["status"]
            if mutation == "missing_alarm":
                status.pop("alarm")
            elif mutation == "wrong_owner":
                status["owner"]["enum"] = {"1": "another-controller"}
            else:
                status["mode"]["enum"] = {"1": "local_only"}
            report = inspect_scene(scene)
            self.assertTrue(report["valid"], report["errors"])
            self.assertEqual(report["capabilities"][0]["supported_modes"], ["monitor"], mutation)

    def test_optional_load_wrong_unit_and_supply_return_sensor_swap_are_not_compatible(self):
        self.scene["devices"]["CDU_01"]["initial_observations"]["cdu.liquid_load"]["unit"] = "W_e"
        report = inspect_scene(self.scene)
        self.assertEqual(report["capabilities"][0]["supported_modes"], ["monitor"])
        self.assertIn("cdu.liquid_load", report["capabilities"][0]["invalid_optional_observations"])
        self.setUp()
        point = self.scene["devices"]["CDU_01"]["initial_observations"]["cdu.sec_supply_temp"]
        point["port_id"] = "trunk_CDU_01_return"
        self.assertTrue(any("measurement_direction_mismatch" in e for e in inspect_scene(self.scene)["errors"]))

    def test_control_mode_list_is_not_a_string_and_must_match_initial_mode(self):
        self.scene["devices"]["CDU_01"]["controls"]["cdu.dp_sp"]["allowed_modes"] = "temperature_dp"
        self.assertFalse(inspect_scene(self.scene)["valid"])
        self.setUp()
        self.scene["devices"]["CDU_01"]["initial_mode"] = "local_auto"
        report = inspect_scene(self.scene)
        self.assertEqual(report["capabilities"][0]["supported_modes"], ["monitor"])

    def test_invalid_thermal_model_parameters_cannot_be_published_as_executable(self):
        for name, value in (("pump_tau_s", 0), ("density_kg_m3", -1), ("pump_efficiency", 1.1)):
            scene = copy.deepcopy(self.scene)
            scene["devices"]["CDU_01"]["simulation"][name] = value
            report = inspect_scene(scene)
            self.assertFalse(report["valid"])
            self.assertFalse(report["capabilities"][0]["can_thermal_sim"])
        profile = self.scene["devices"]["CDU_01"]
        del profile["guards"]["cdu.sec_flow"]
        del profile["initial_observations"]["cdu.sec_flow"]
        self.assertFalse(inspect_scene(self.scene)["valid"])

    def test_registry_preserves_default_and_metadata_isolation(self):
        profile = self.scene["devices"]["CDU_01"]
        policy = copy.deepcopy(profile["policy"])
        policy.pop("kind")
        self.assertEqual(create_policy(policy).gain, create_policy(profile["policy"]).gain)
        adapter = create_adapter("CDU_01", profile, "lc-core")
        self.assertEqual(adapter.describe().asset_id, "CDU_01")
        report = registry_report()
        report["adapters"].clear()
        self.assertGreater(len(registry_report()["adapters"]), 0)

    def test_incomplete_json_drafts_return_reports_instead_of_crashing(self):
        for scene in (None, [], {}, {"assets": [None]}, {"assets": [{"id": []}]},
                      {"physical_topology": {"ports": [None], "connections": [None]}}):
            report = inspect_scene(scene)
            self.assertFalse(report["valid"])
            self.assertTrue(report["errors"])
            json.dumps(report, allow_nan=False)
        for field in ("ports", "connections"):
            scene = copy.deepcopy(self.scene)
            scene["physical_topology"][field].append(None)
            self.assertFalse(inspect_scene(scene)["valid"])


if __name__ == "__main__":
    unittest.main()
