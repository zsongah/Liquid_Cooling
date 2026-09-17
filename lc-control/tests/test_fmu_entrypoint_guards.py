"""所有独立 FMU 执行工具在读取旧字段、载入 FMU 或创建输出前拒绝 0.2。"""
import importlib
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sustain = importlib.import_module("run_sustain_fmu")
diagnostics = importlib.import_module("run_fmu_diagnostics")
matrix = importlib.import_module("run_fmu_validation_matrix")


class FmuEntrypointGuardTests(unittest.TestCase):
    def test_workers_and_preflight_reject_before_model_or_output(self):
        scene = {"schema_version": "0.2"}  # 不给旧字段：必须返回原因码而不是 KeyError。
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-create"
            with patch.object(sustain, "SustainFmuMaster") as master, patch.object(diagnostics, "SustainFmuMaster") as diagnostic_master:
                for action in (lambda: sustain.validate_experiment(scene),
                               lambda: sustain.run_worker(Path("absent.fmu"), scene, output, "adaptive"),
                               lambda: diagnostics.worker(Path("absent.fmu"), scene, output, "hold")):
                    with self.assertRaisesRegex(ValueError, "analysis_only_schema_not_executable"):
                        action()
                master.assert_not_called()
                diagnostic_master.assert_not_called()
            self.assertFalse(output.exists())

    def test_all_suite_frontdoors_reject_before_fmu_inspection_or_subprocess(self):
        config = ROOT / "examples/hydraulic_parallel_v02.json"
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "must-not-create"
            args = SimpleNamespace(config=str(config), output=str(output), fmu="absent.fmu",
                                   matrix="absent-matrix.json", source=None)
            with patch.object(sustain, "inspect_contract") as inspect, patch.object(diagnostics, "inspect_contract") as diagnostic_inspect, \
                 patch("subprocess.run") as external, patch.object(sustain, "SustainFmuMaster") as master:
                for action in (sustain.run_suite, diagnostics.suite, matrix.suite):
                    with self.subTest(action=action.__module__), self.assertRaisesRegex(ValueError, "analysis_only_schema_not_executable"):
                        action(args)
                inspect.assert_not_called()
                diagnostic_inspect.assert_not_called()
                external.assert_not_called()
                master.assert_not_called()
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
