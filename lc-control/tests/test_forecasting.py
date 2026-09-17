"""预测接入的物理口径、时间基准、完整覆盖及实际前馈连接验证。"""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from lc_control.engine import Engine
from lc_control.forecasting import ForecastError, descriptor, preview, resolve_domain, validate_input
from lc_control.plant import ThermalPlant

ROOT = Path(__file__).resolve().parents[1]


def packet(entries=None, **changes):
    result = {"forecast_id": "forecast-test-1", "source": {"kind": "synthetic", "name": "explicit-test"},
              "clock_basis": "simulation", "issued_at": 0, "valid_from": 0, "valid_until": 120,
              "max_age_s": 120, "unit": "W_e", "selection": "central", "entries": entries or [
                  {"asset_id": name, "liquid_fraction": 0.8,
                   "samples": [{"at": 0, "power_w": 60000}, {"at": 30, "power_w": 80000}, {"at": 120, "power_w": 60000}]}
                  for name in ("RACK_01", "RACK_02")]}
    result.update(changes)
    return result


class ForecastingTests(unittest.TestCase):
    def setUp(self):
        self.scene = json.loads((ROOT / "examples/workbench_physical.json").read_text())
        self.domain = self.scene["control_domains"][0]["id"]

    def test_rack_totals_use_simultaneous_sum_then_peak_not_sum_of_peaks(self):
        data = packet()
        for entry, values in zip(data["entries"], ((20000, 80000, 20000), (80000, 20000, 80000))):
            for point, value in zip(entry["samples"], values):
                point["power_w"] = value
        canonical = validate_input(self.scene, data, 0)
        internal, result = resolve_domain(self.scene, self.domain, canonical, 0, "simulation")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["peak_liquid_w"], 80000)
        self.assertEqual(sum(r["peak_liquid_w"] for r in result["racks"]), 128000)
        self.assertEqual(internal["source_issued_at"], 0)
        self.assertEqual(internal["materialized_at"], 0)

    def test_node_power_and_capture_fraction_aggregate_without_double_count(self):
        entries = []
        for rack in ("RACK_01", "RACK_02"):
            for number, fraction in ((1, 1), (2, 0.5)):
                entries.append({"asset_id": rack + "_NODE_" + str(number), "liquid_fraction": fraction,
                                "samples": [{"at": at, "power_w": 20000} for at in (0, 30, 120)]})
        data = validate_input(self.scene, packet(entries), 0)
        internal, result = resolve_domain(self.scene, self.domain, data, 5, "simulation")
        self.assertEqual(result["peak_liquid_w"], 60000)
        self.assertEqual(result["series"][0]["electric_w"], 80000)
        self.assertEqual(result["racks"][0]["source_assets"], ["RACK_01_NODE_1", "RACK_01_NODE_2"])
        self.assertEqual(internal["issued_at"], 5)
        self.assertEqual(internal["source_issued_at"], 0)

    def test_parent_child_duplicate_partial_nodes_and_bad_ownership_rejected(self):
        cases = []
        duplicate = packet(); duplicate["entries"].append(copy.deepcopy(duplicate["entries"][0])); cases.append(duplicate)
        mixed = packet(); child = copy.deepcopy(mixed["entries"][0]); child["asset_id"] = "RACK_01_NODE_1"; mixed["entries"].append(child); cases.append(mixed)
        partial = packet(); partial["entries"][0]["asset_id"] = "RACK_01_NODE_1"; cases.append(partial)
        unknown = packet(); unknown["entries"][0]["asset_id"] = "UNKNOWN"; cases.append(unknown)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ForecastError):
                validate_input(self.scene, case, 0)
        shared = copy.deepcopy(self.scene)
        shared["control_domains"].append(dict(shared["control_domains"][0], id="SECOND"))
        with self.assertRaisesRegex(ForecastError, "唯一"):
            validate_input(shared, packet(), 0)

    def test_partial_domain_input_does_not_silently_zero_missing_racks(self):
        data = packet(); data["entries"].pop()
        canonical = validate_input(self.scene, data, 0)
        internal, result = resolve_domain(self.scene, self.domain, canonical, 5, "simulation")
        self.assertIsNone(internal)
        self.assertEqual(result["reason"], "incomplete_domain_rack_coverage")

    def test_disjoint_domains_are_aggregated_without_duplicating_site_load(self):
        scene = copy.deepcopy(self.scene)
        scene["devices"]["CDU_02"] = copy.deepcopy(scene["devices"]["CDU_01"])
        scene["control_domains"][0]["served_racks"] = ["RACK_01"]
        scene["control_domains"].append(dict(scene["control_domains"][0], id="DOMAIN_02", cdu_id="CDU_02", served_racks=["RACK_02"]))
        result = preview(scene, validate_input(scene, packet(), 0), 5, "simulation")
        self.assertEqual(result["site"]["status"], "complete")
        self.assertEqual(result["site"]["peak_liquid_w"], 128000)
        self.assertEqual(len(result["site"]["included_domains"]), 2)
        data = packet(); data["entries"].pop()
        partial = preview(scene, validate_input(scene, data, 0), 5, "simulation")
        self.assertEqual(partial["site"]["status"], "partial")
        self.assertEqual(partial["site"]["peak_liquid_w"], 64000)

    def test_original_age_expiry_clock_and_horizon_are_checked_each_cycle(self):
        data = validate_input(self.scene, packet(max_age_s=10), 0)
        for now, clock, reason in ((11, "simulation", "forecast_too_old"), (5, "unix", "forecast_clock_mismatch")):
            internal, result = resolve_domain(self.scene, self.domain, data, now, clock)
            self.assertIsNone(internal)
            self.assertEqual(result["reason"], reason)
        data = validate_input(self.scene, packet(max_age_s=3600), 0)
        for now, reason in ((100, "forecast_does_not_cover_horizon"), (120, "forecast_expired")):
            self.assertEqual(resolve_domain(self.scene, self.domain, data, now, "simulation")[1]["reason"], reason)
        with self.assertRaises(ForecastError):
            validate_input(self.scene, packet(), 121)

    def test_uncertainty_selection_is_explicit_and_requires_complete_bounds(self):
        data = packet(selection="upper_bound")
        with self.assertRaisesRegex(ForecastError, "upper_bound_required"):
            validate_input(self.scene, data, 0)
        for entry in data["entries"]:
            for point in entry["samples"]:
                point["upper_power_w"] = point["power_w"] + 1000
        result = resolve_domain(self.scene, self.domain, validate_input(self.scene, data, 0), 0, "simulation")[1]
        self.assertEqual(result["peak_liquid_w"], 129600)
        data["entries"][0]["samples"][0]["lower_power_w"] = 999999
        with self.assertRaises(ForecastError):
            validate_input(self.scene, data, 0)

    def test_wrong_units_source_clock_and_nonfinite_or_unaligned_data_rejected(self):
        for change in ({"unit": "kW"}, {"source": {"kind": "external", "name": "wrong-clock"}}, {"max_age_s": float("inf")}, {"unexpected": 1}):
            with self.subTest(change=change), self.assertRaises(ForecastError):
                validate_input(self.scene, packet(**change), 0)
        data = packet(); data["entries"][0]["samples"][1]["at"] = 31
        with self.assertRaisesRegex(ForecastError, "unaligned"):
            validate_input(self.scene, data, 0)

    def test_descriptor_is_explicit_synthetic_input_not_an_automatic_forecaster(self):
        item = descriptor(self.scene, "config-x", 2)
        self.assertEqual(item["example"]["source"]["kind"], "synthetic")
        self.assertTrue(any(a["kind"] == "node" for a in item["assets"]))
        validate_input(self.scene, item["example"], 0)

    def test_delayed_network_read_materializes_after_acquisition_at_same_decision_clock(self):
        scene = copy.deepcopy(self.scene)
        adapter = ThermalPlant("CDU_01", scene["devices"]["CDU_01"], "lc-core")
        adapter.advance(5, 75000)
        describe = adapter.describe
        adapter.describe = lambda: replace(describe(), deployment="hardware")
        clock = {"now": 1000.0}
        original_read = adapter.read
        def delayed_read(unused_now):
            clock["now"] += 2
            return original_read(clock["now"])
        adapter.read = delayed_read
        data = packet(source={"kind": "external", "name": "external-test"}, clock_basis="unix", issued_at=1000, valid_from=1000, valid_until=1120)
        for entry in data["entries"]:
            for point in entry["samples"]:
                point["at"] += 1000
        canonical = validate_input(scene, data, 1000)
        materialized = []
        def provider(now):
            materialized.append(now)
            return resolve_domain(scene, self.domain, canonical, now, "unix")
        def now():
            clock["now"] += 0.01
            return clock["now"]
        with tempfile.TemporaryDirectory() as output:
            engine = Engine(scene, self.domain, adapter, output, "shadow")
            try:
                with patch("lc_control.runtime.time.time", side_effect=now):
                    decision = engine.tick(1000, forecast_provider=provider)
                self.assertNotIn("error", decision)
                self.assertGreater(materialized[0], 1002)
                self.assertEqual(decision["forecast_status"], "forecast_used")
                self.assertEqual(decision["forecast_input"]["status"], "used")
                self.assertEqual(decision["forecast_input"]["source_issued_at"], 1000)
                self.assertEqual(decision["request"]["issued_at"], materialized[0])
            finally:
                engine.close()


if __name__ == "__main__":
    unittest.main()
