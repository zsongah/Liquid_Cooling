import copy
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from lc_control.adapters import MockCDUAdapter
from lc_control.configuration import load_scene, validate_scene
from lc_control.contracts import ControlRequest, Reading
from lc_control.runtime import ControlService

ROOT = Path(__file__).resolve().parents[1]


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.scene = load_scene(ROOT / "examples/liquid_to_liquid.json")
        self.domain = self.scene["control_domains"][0]
        self.adapter = MockCDUAdapter("CDU_01", self.scene["devices"]["CDU_01"], "lc-core")
        self.service = ControlService(self.scene, "CDU_DOMAIN_01", self.adapter)
        self.request = ControlRequest("r1", "CDU_01", "example-1", "lc-core",
                                      "cdu.sec_supply_temp_sp", 29, "degC", 100, 130)

    def test_default_read_only_and_shadow_never_write(self):
        self.assertEqual(self.service.submit(self.request, 100).status, "rejected")
        self.service.set_mode("shadow")
        self.assertEqual(self.service.submit(replace(self.request, request_id="r2"), 100).status, "shadow")
        self.assertEqual(self.adapter.writes, [])

    def test_command_readback_is_not_process_success(self):
        self.service.set_mode("control")
        result = self.service.submit(self.request, 100)
        self.assertEqual(result.status, "setpoint_confirmed")
        self.assertEqual(result.readback, 29)
        self.assertEqual(result.process_response, "not_verified")
        self.assertEqual(self.adapter.read(100).observations["cdu.sec_supply_temp"].value, 301.15)

    def test_expired_units_bounds_owner_and_version_do_not_write(self):
        self.service.set_mode("control")
        variants = [replace(self.request, expires_at=99), replace(self.request, unit="K"),
                    replace(self.request, value=999), replace(self.request, owner="other"),
                    replace(self.request, topology_version="old"),
                    replace(self.request, value=float("nan"))]
        for index, request in enumerate(variants):
            result = self.service.submit(replace(request, request_id=str(index)), 100)
            self.assertEqual(result.status, "rejected")
        self.assertEqual(self.adapter.writes, [])

    def test_replay_does_not_repeat_write_and_conflicting_id_rejected(self):
        self.service.set_mode("control")
        original = self.service.submit(self.request, 100)
        self.assertEqual(self.service.submit(self.request, 101), original)
        self.assertIn("idempotency_conflict", self.service.submit(replace(self.request, value=28), 102).reasons)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_step_and_frequency_limits(self):
        self.service.set_mode("control")
        large = self.service.submit(replace(self.request, value=32), 100)
        self.assertIn("max_step_exceeded", large.reasons)
        self.service.submit(replace(self.request, request_id="second"), 100)
        fast = self.service.submit(replace(self.request, request_id="third", value=30), 101)
        self.assertIn("minimum_interval_not_met", fast.reasons)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_stale_telemetry_hands_back_even_without_new_request(self):
        self.service.set_mode("control")
        self.adapter.stale_by_s = 60
        result = self.service.heartbeat(100)
        self.assertEqual(result["mode"], "paused")
        self.assertTrue(result["handoff_confirmed"])
        self.assertEqual(self.adapter.operating_mode, "local_auto")
        with self.assertRaises(ValueError):
            self.service.set_mode("control")

    def test_active_alarm_and_missing_guard_pause(self):
        for missing in (True, False):
            self.setUp()
            self.service.set_mode("control")
            if missing:
                del self.adapter.profile["initial_observations"]["cdu.sec_flow"]
            else:
                self.adapter.alarms = ("leak_detected",)
            self.assertEqual(self.service.heartbeat(100)["mode"], "paused")
            self.assertEqual(self.adapter.writes, [])

    def test_uncertain_write_and_failed_handoff_are_not_reported_successful(self):
        self.service.set_mode("control")
        self.adapter.fail_write = True
        self.adapter.fail_release = True
        result = self.service.submit(self.request, 100)
        self.assertEqual(result.status, "uncertain")
        self.assertIn("handoff_unconfirmed", result.reasons)
        self.service.submit(self.request, 101)
        self.assertEqual(len(self.adapter.writes), 1)

    def test_wrong_readback_pauses_control(self):
        self.service.set_mode("control")
        self.adapter.ignore_write = True
        result = self.service.submit(self.request, 100)
        self.assertEqual(result.status, "uncertain")
        self.assertEqual(self.service.mode, "paused")

    def test_read_failure_and_owner_loss(self):
        for owner_loss in (True, False):
            self.setUp()
            self.service.set_mode("control")
            if owner_loss:
                self.adapter.owner = "operator"
            else:
                self.adapter.read_fail = True
            result = self.service.heartbeat(100)
            self.assertEqual(result["mode"], "paused")
            if owner_loss:
                self.assertFalse(result["handoff_confirmed"])
                self.assertEqual(self.adapter.owner, "operator")

    def test_shared_hydraulics_cannot_enable_independent_control(self):
        self.scene["control_domains"][0]["shared_hydraulics"] = True
        with self.assertRaisesRegex(ValueError, "coordinator"):
            self.service.set_mode("control")

    def test_dangling_rack_and_wrong_heat_sink_fail_validation(self):
        bad = copy.deepcopy(self.scene)
        bad["control_domains"][0]["served_racks"] = ["NOT_PRESENT"]
        with self.assertRaisesRegex(ValueError, "unknown_rack"):
            validate_scene(bad)
        bad = copy.deepcopy(self.scene)
        bad["assets"][0]["kind"] = "room_air"
        with self.assertRaisesRegex(ValueError, "heat_path_mismatch"):
            validate_scene(bad)

    def test_two_device_types_share_core_but_offer_different_capabilities(self):
        self.service.set_mode("control")
        flow = replace(self.request, quantity="cdu.sec_flow_sp", unit="kg/s", value=2.2)
        self.assertIn("unsupported_control", self.service.submit(flow, 100).reasons)
        scene = load_scene(ROOT / "examples/liquid_to_air.json")
        adapter = MockCDUAdapter("CDU_01", scene["devices"]["CDU_01"], "lc-core")
        service = ControlService(scene, "CDU_DOMAIN_01", adapter)
        service.set_mode("control")
        self.assertEqual(service.submit(flow, 100).status, "setpoint_confirmed")

    def test_mode_switch_prevents_wrong_control(self):
        self.service.set_mode("control")
        self.adapter.operating_mode = "manual"
        result = self.service.submit(self.request, 100)
        self.assertEqual(result.status, "rejected")
        self.assertEqual(self.adapter.writes, [])

    def test_audit_has_intent_before_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            service = ControlService(self.scene, "CDU_DOMAIN_01", self.adapter, path)
            service.set_mode("control")
            service.submit(self.request, 100)
            lines = path.read_text().splitlines()
            self.assertIn('"dispatch_intent"', lines[1])
            self.assertIn('"setpoint_confirmed"', lines[2])


if __name__ == "__main__":
    unittest.main()

