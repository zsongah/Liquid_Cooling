"""FMU 适配关键契约回归；使用可控 FMI 替身，不要求本机安装 PyFMI。

真实 FMU 响应由独立 Docker 闭环实验验证。本测试关注最容易导致错误控制的
单位、时钟、失败状态、共享控制隔离，以及回读语义。
"""
import copy
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from lc_control.configuration import load_scene
from lc_control.contracts import ControlRequest
from lc_control.runtime import ControlService
from lc_control.sustain_fmu import (
    SustainFmuMaster, SustainFmuAdapter, FmuLaboratoryGate, inspect_contract,
    KPA_PER_PSI, PA_TO_PSI, block_prefix, input_prefix,
)

ROOT = Path(__file__).resolve().parents[1]


def archive(path, bad_gain=False, bad_unit=False, bad_input=False):
    root = ET.Element("fmiModelDescription", fmiVersion="2.0", modelName="test")
    ET.SubElement(root, "CoSimulation", modelIdentifier="test", canBeInstantiatedOnlyOncePerProcess="true")
    variables = ET.SubElement(root, "ModelVariables")

    def point(name, unit=None, causality=None, start=None, ref="1"):
        attrs = {"name": name, "valueReference": ref}
        if causality:
            attrs["causality"] = causality
        v = ET.SubElement(variables, "ScalarVariable", attrs)
        real = ET.SubElement(v, "Real")
        if unit:
            real.set("unit", unit)
        if start is not None:
            real.set("start", str(start))

    for group in range(1, 6):
        inp, p = input_prefix(group), block_prefix(group) + ".cdu[1]"
        point(inp + "_cdu_1_sources_dp_nom_RL", causality="local" if bad_input else "input", ref="2")
        point(p + ".sources.dp_nom_RL")
        point(p + ".controls.PID_CDUP.u_s")
        point(p + ".controls.gain.k", start=.001 if bad_gain else PA_TO_PSI)
        for key, unit in (("T_sec_s", "K"), ("T_sec_r", "K"), ("p_sec_s", "Pa"),
                          ("p_sec_r", "Pa"), ("m_flow_sec", "kg/s"), ("W_flow_CDUP", "W")):
            point(p + ".summary." + key, "kPa" if bad_unit and key == "p_sec_s" else unit)
        point(inp + "_cdu_1_sources_Tsec_supply_nom_RL", causality="input")
        for branch in range(1, 4):
            point(inp + "_cabinet_1_sources_Valve_Stpts[%s]" % branch, causality="input")
            point(inp + "_cabinet_1_sources_ComputePowerBlade%s" % branch, causality="input")
            point(block_prefix(group) + ".cabinet[1].coolingChannels_%s.medium.T" % branch, "K")
    with ZipFile(path, "w") as file:
        file.writestr("modelDescription.xml", ET.tostring(root))


class FakeFmi:
    def __init__(self):
        self.values = {}
        self.time = 0
        self.status = 0
        self.steps = []
        self.freed = False

    def setup_experiment(self, **kwargs):
        pass

    def initialize(self):
        pass

    def set(self, name, value):
        self.values[name] = value

    def get(self, name):
        if name in self.values:
            return [self.values[name]]
        for group in range(1, 6):
            if name == block_prefix(group) + ".cdu[1].controls.PID_CDUP.u_s":
                return [self.values[input_prefix(group) + "_cdu_1_sources_dp_nom_RL"]]
        endings = {"T_sec_s": 301.15, "T_sec_r": 310.15, "p_sec_s": 290000,
                   "p_sec_r": 100000, "m_flow_sec": 12.3, "W_flow_CDUP": 3400, "medium.T": 312.15}
        for suffix, value in endings.items():
            if name.endswith(suffix):
                return [value]
        return [10000.0]

    def do_step(self, current_t, step_size):
        self.steps.append((current_t, step_size))
        self.time += step_size
        return self.status

    def terminate(self):
        pass

    def free_instance(self):
        self.freed = True


class SustainFmuTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "fake.fmu"
        archive(self.path)
        self.scene = load_scene(ROOT / "examples/sustain_fmu_experiment.json")
        self.fmi = FakeFmi()
        self.master = SustainFmuMaster(self.path, self.scene["extensions"]["sustain_fmu_experiment"],
                                       loader=lambda *a, **k: self.fmi)
        self.addCleanup(self.master.close)
        self.adapter = SustainFmuAdapter(self.master, self.scene)

    def test_pressure_units_and_actual_measurement_are_distinct(self):
        self.assertAlmostEqual(inspect_contract(self.path)["kpa_per_input_unit"], 6.89475728, places=9)
        s = self.adapter.read(0)
        self.assertAlmostEqual(s.setpoints["cdu.dp_sp"].value, 27.5 * KPA_PER_PSI)
        self.assertEqual(s.observations["cdu.sec_dp"].value, 190)
        self.assertNotEqual(s.observations["cdu.sec_dp"].value, s.setpoints["cdu.dp_sp"].value)

    def test_metadata_rejects_unverified_units_or_local_actuator(self):
        for option in ("bad_gain", "bad_unit", "bad_input"):
            archive(self.path, **{option: True})
            with self.assertRaises(ValueError):
                inspect_contract(self.path)

    def test_single_master_monotonic_time_and_stale_read(self):
        self.master.advance(15, 60000)
        self.master.advance(15, 75000)
        self.assertEqual(self.fmi.steps, [(0, 15), (15, 15)])
        with self.assertRaises(ValueError):
            self.adapter.read(15)
        with self.assertRaises(ValueError):
            SustainFmuAdapter(self.master, self.scene)

    def test_non_ok_step_never_becomes_successful_sample(self):
        self.fmi.status = 2
        with self.assertRaises(RuntimeError):
            self.master.advance(15, 60000)
        self.assertEqual(self.master.time, 0)
        self.assertIsNone(self.master.last_record)
        with self.assertRaises(ValueError):
            self.master.write_dp(30 * KPA_PER_PSI)
        self.assertFalse(self.adapter.release_to_local("fmu-laboratory-master"))

    def test_default_gateway_still_blocks_shared_control(self):
        service = ControlService(self.scene, "FMU_DOMAIN_01", self.adapter)
        with self.assertRaisesRegex(ValueError, "shared_hydraulics"):
            service.set_mode("control")

    def test_laboratory_gate_reuses_bounds_and_confirms_converted_write(self):
        gate = FmuLaboratoryGate(self.scene, "FMU_DOMAIN_01", self.adapter)
        gate.set_mode("control")
        request = ControlRequest("valid", "CDU_01", self.scene["topology_version"],
                                 "fmu-laboratory-master", "cdu.dp_sp", 27.5 * KPA_PER_PSI + 2, "kPa", 0, 15)
        receipt = gate.submit(request, 0)
        self.assertEqual(receipt.status, "setpoint_confirmed")
        self.assertEqual(receipt.process_response, "not_verified")
        self.assertAlmostEqual(self.master.writes[-1]["fmu_input_psi"], request.value / KPA_PER_PSI)
        rejected = gate.submit(replace(request, request_id="bad", value=999), 0)
        self.assertEqual(rejected.status, "rejected")
        self.assertEqual(len(self.master.writes), 1)

    def test_simulation_permission_cannot_be_used_for_hardware(self):
        original = self.adapter.describe
        self.adapter.describe = lambda: replace(original(), deployment="hardware")
        gate = FmuLaboratoryGate(self.scene, "FMU_DOMAIN_01", self.adapter)
        self.assertFalse(gate._shared_control_permitted())

    def test_release_is_fixed_experiment_hold_not_oem_local_auto(self):
        self.master.write_dp(30 * KPA_PER_PSI)
        self.assertTrue(self.adapter.release_to_local("fmu-laboratory-master"))
        snapshot = self.adapter.read(0)
        self.assertEqual(snapshot.operating_mode, "experiment_fixed_hold")
        self.assertAlmostEqual(snapshot.setpoints["cdu.dp_sp"].value, 27.5 * KPA_PER_PSI)
        with self.assertRaises(ValueError):
            self.master.write_dp(30 * KPA_PER_PSI)


if __name__ == "__main__":
    unittest.main()
