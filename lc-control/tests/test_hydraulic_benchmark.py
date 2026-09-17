"""规模基准不能漏报失败，也不能把超过未知节点上限的拒绝当作求解性能。"""
import contextlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools import benchmark_hydraulics as benchmark
from lc_control.analysis_config import validate_analysis_scene


class HydraulicBenchmarkTests(unittest.TestCase):
    def test_exact_size_gradient_and_unknown_pressure_envelope(self):
        for shape in benchmark.SHAPES:
            for size in benchmark.SIZES:
                with self.subTest(shape=shape, size=size):
                    scene = benchmark.build_scene(size, shape)
                    validate_analysis_scene(scene)
                    self.assertEqual(len(scene["hydraulics"]["elements"]), size)
                    unknown = sum(n.get("pressure_pa") is None for n in scene["hydraulics"]["junctions"])
                    self.assertEqual(unknown, 0 if shape == "independent_parallel" else size // 2 + 1)
                    self.assertEqual(scene["devices"], {})

    def test_real_main_table_and_fault_cases_report_residuals_and_rejections(self):
        with patch("lc_control.registry.create_adapter") as factory:
            report = benchmark.run_benchmark(repeats=1)
        factory.assert_not_called()
        self.assertTrue(report["all_expectations_met"], report)
        self.assertEqual(len(report["cases"]), 11)
        rejected = [case for case in report["cases"] if case["timing_kind"] == "rejection_latency"]
        self.assertEqual({case["element_count"] for case in rejected}, {128, 256})
        for case in report["cases"]:
            self.assertTrue(all(math.isfinite(value) for value in case["latency_ms"].values()))
            self.assertEqual(len(case["samples"]), 1)
            if case in rejected:
                self.assertEqual(case["errors"], ["solver_size_limit_exceeded"])
            else:
                self.assertEqual(case["status_counts"], {"converged": 1})
                self.assertLessEqual(case["samples"][0]["residuals"]["mass_kg_s"], benchmark.solver.MASS_TOL)
        self.assertFalse(report["hardware_writes"])
        json.dumps(report, allow_nan=False)

    def test_failed_samples_are_retained_and_cli_returns_nonzero(self):
        failure = {"status": "failed", "reason": "solver_time_budget_exceeded", "elapsed_ms": 2,
                   "iterations": 0, "residuals": {}, "flows_kg_s": {}}
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "benchmark.json"
            with patch.object(benchmark.solver, "solve_network", return_value=failure), contextlib.redirect_stdout(io.StringIO()):
                code = benchmark.main(["--repeats", "1", "--no-faults", "--output", str(output)])
            self.assertEqual(code, 1)
            report = json.loads(output.read_text())
            self.assertFalse(report["all_expectations_met"])
            self.assertTrue(all(case["status_counts"] == {"failed": 1} for case in report["cases"]))
            self.assertTrue(all(case["samples"][0]["reason"] == "solver_time_budget_exceeded" for case in report["cases"]))

    def test_input_budget_and_repetition_limits(self):
        for repeats, budget in ((0, 2), (101, 2), (True, 2), (1, 0), (1, 3), (1, float("nan"))):
            with self.subTest(repeats=repeats, budget=budget), self.assertRaises(ValueError):
                benchmark.run_benchmark(repeats, budget)


if __name__ == "__main__":
    unittest.main()
