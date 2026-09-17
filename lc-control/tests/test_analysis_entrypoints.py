"""0.2 产品入口边界：真实离线分析、版本绑定以及所有旧执行入口零连接。"""
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from lc_control.__main__ import main
from lc_control.forecasting import ForecastError, descriptor, preview, resolve_domain, validate_input
from lc_control.operations import compare, run, simulate
from lc_control.site_view import site_snapshot
from lc_control.workbench import Workbench, WorkbenchError

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples/hydraulic_parallel_v02.json"


class AnalysisEntrypointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.manager = Workbench(self.root)
        self.scene = json.loads(EXAMPLE.read_text(encoding="utf-8"))

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def publish(self):
        draft = self.manager.save_draft({"name": "并联支路离线验收", "scene": self.scene})
        return self.manager.publish(draft["id"], draft["revision"])

    def test_analysis_uses_published_snapshot_and_saves_separate_model_evidence(self):
        config = self.publish()
        with patch("lc_control.registry.create_adapter") as factory, patch("lc_control.workbench.Engine") as engine:
            result = self.manager.analyze_config(config["id"], {})
        factory.assert_not_called()
        engine.assert_not_called()
        self.assertEqual(result["source"], "model_estimate")
        self.assertFalse(result["hardware_writes"])
        self.assertEqual(result["config_hash"], config["scene_hash"])
        self.assertEqual(result["config_revision"], config["revision"])
        self.assertTrue(result["branches"], "must return actual solver branch output")
        saved = self.manager.state / "analyses" / (result["analysis_id"] + ".json")
        self.assertEqual(json.loads(saved.read_text()), result)
        self.assertEqual(self.manager.list_jobs(), [])
        self.assertEqual(list((self.manager.state / "runs").iterdir()), [])
        self.assertEqual(self.manager.get_config(config["id"])["scene"], self.scene)

    def test_draft_legacy_domain_and_extra_execution_fields_are_rejected(self):
        draft = self.manager.save_draft({"name": "未发布", "scene": self.scene})
        with self.assertRaises(WorkbenchError) as caught:
            self.manager.analyze_config(draft["id"], {})
        self.assertEqual(caught.exception.code, "published_config_required")
        config = self.manager.publish(draft["id"], draft["revision"])
        for payload, code in (({"domain_id": "unrelated-domain"}, "invalid_analysis_domain"),
                              ({"mode": "control"}, "invalid_analysis_request"),
                              ({"domain_id": []}, "invalid_analysis_domain")):
            with self.subTest(payload=payload), self.assertRaises(WorkbenchError) as caught:
                self.manager.analyze_config(config["id"], payload)
            self.assertEqual(caught.exception.code, code)
        old = json.loads((ROOT / "examples/thermal_liquid_to_liquid.json").read_text())
        draft = self.manager.save_draft({"name": "旧版热工", "scene": old})
        published = self.manager.publish(draft["id"], draft["revision"])
        with self.assertRaises(WorkbenchError) as caught:
            self.manager.analyze_config(published["id"], {})
        self.assertEqual(caught.exception.code, "analysis_schema_required")

    def test_all_legacy_operations_reject_before_factory_or_output_creation(self):
        # 故意省略 cdu_id 与 devices，证明拒绝发生于旧字段访问及工厂之前。
        scene = {"schema_version": "0.2"}
        output = self.root / "must-not-exist"
        with patch("lc_control.operations.create_adapter") as factory, patch("lc_control.operations.Engine") as engine:
            for operation in (simulate, compare, run):
                with self.subTest(operation=operation.__name__), self.assertRaisesRegex(ValueError, "analysis_only_schema_not_executable"):
                    operation(scene, output)
        factory.assert_not_called()
        engine.assert_not_called()
        self.assertFalse(output.exists())

    def test_workbench_blocks_all_modes_even_with_server_hardware_flag(self):
        config = self.publish()
        self.manager.allow_hardware_control = True
        with patch("lc_control.registry.create_adapter") as factory, patch("lc_control.workbench.Engine") as engine:
            for kind in ("thermal_sim", "modbus"):
                for mode in ("monitor", "shadow", "control"):
                    with self.subTest(kind=kind, mode=mode), self.assertRaises(WorkbenchError) as caught:
                        self.manager.start_job({"config_id": config["id"], "kind": kind, "mode": mode})
                    self.assertEqual(caught.exception.code, "analysis_only_schema_not_executable")
        factory.assert_not_called()
        engine.assert_not_called()
        self.assertEqual(self.manager.list_jobs(), [])

    def test_analysis_site_never_queries_old_runs_or_creates_fake_measurements(self):
        config = self.publish()
        with patch.object(self.manager, "list_jobs", side_effect=AssertionError("must not associate old jobs")), \
             patch.object(self.manager, "get_run", side_effect=AssertionError("must not read old telemetry")):
            result = site_snapshot(self.manager, config["id"])
        self.assertEqual(result["scope"], "analysis_only")
        self.assertEqual(result["telemetry_by_asset"], {})
        self.assertEqual(result["coverage"]["observed_domains"], 0)
        self.assertTrue(all(d["latest_telemetry"] is None and not d["control_active"] for d in result["domains"]))

    def test_forecast_all_public_paths_explain_unsupported_without_aggregation(self):
        config = self.publish()
        result = self.manager.forecast_descriptor(config["id"])
        self.assertEqual(result["status"], "unsupported")
        self.assertIsNone(result["example"])
        current = self.manager.get_forecast(config["id"])
        self.assertEqual(current["status"], "unsupported")
        self.assertEqual(current["usage"], [])
        self.assertIsNone(current["reference"])
        invalid = self.manager.validate_forecast(config["id"], {})
        self.assertFalse(invalid["valid"])
        self.assertEqual(invalid["errors"][0]["code"], "analysis_schema_forecast_unsupported")
        for operation in (lambda: self.manager.submit_forecast(config["id"], {}),
                          lambda: self.manager.clear_forecast(config["id"])):
            with self.assertRaises(WorkbenchError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "analysis_schema_forecast_unsupported")
        self.assertEqual(list((self.manager.state / "forecasts").iterdir()), [])
        with self.assertRaisesRegex(ForecastError, "analysis_schema_forecast_unsupported"):
            validate_input(self.scene, {}, 0)
        internal, usage = resolve_domain(self.scene, self.scene["control_domains"][0]["id"], {}, 0, "simulation")
        self.assertIsNone(internal)
        self.assertEqual(usage["status"], "unsupported")
        self.assertEqual(preview(self.scene, {}, 0, "simulation")["site"]["status"], "unsupported")

    def test_cli_analysis_generates_json_without_adapter_and_validate_is_readable(self):
        destination = self.root / "results" / "hydraulic-analysis.json"
        stdout = io.StringIO()
        with patch("sys.argv", ["lc_control", "analyze", str(EXAMPLE), "--output", str(destination)]), \
             patch("lc_control.registry.create_adapter") as factory, contextlib.redirect_stdout(stdout):
            main()
        factory.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertEqual(json.loads(destination.read_text()), result)
        self.assertEqual(result["source"], "model_estimate")
        self.assertFalse(result["hardware_writes"])
        stdout = io.StringIO()
        with patch("sys.argv", ["lc_control", "validate", str(EXAMPLE)]), contextlib.redirect_stdout(stdout):
            main()
        self.assertTrue(json.loads(stdout.getvalue())["valid"])

    def test_cli_non_analysis_execution_actions_reject_new_schema(self):
        with patch("lc_control.registry.create_adapter") as factory, patch("lc_control.__main__.MockCDUAdapter") as mock_factory:
            for action in ("demo", "simulate", "run", "emulator"):
                with self.subTest(action=action), patch("sys.argv", ["lc_control", action, str(EXAMPLE)]), \
                     contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                    main()
                self.assertEqual(caught.exception.code, 2)
        factory.assert_not_called()
        mock_factory.assert_not_called()

    def test_cli_migration_writes_loadable_draft_and_reports_missing_physics(self):
        from lc_control.configuration import load_scene
        source = ROOT / "examples/thermal_liquid_to_liquid.json"
        original = source.read_bytes()
        destination = self.root / "migration" / "draft-v02.json"
        stdout = io.StringIO()
        with patch("sys.argv", ["lc_control", "migrate", str(source), "--output", str(destination)]), \
             patch("lc_control.registry.create_adapter") as factory, contextlib.redirect_stdout(stdout):
            main()
        factory.assert_not_called()
        draft = load_scene(destination)
        self.assertEqual(draft["schema_version"], "0.2")
        report = json.loads(stdout.getvalue())
        self.assertEqual(report["source_schema"], "0.1")
        self.assertTrue(report["gaps"], "migration must disclose missing hydraulic facts")
        self.assertEqual(source.read_bytes(), original)
        with patch("sys.argv", ["lc_control", "migrate", str(EXAMPLE)]), \
             contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
            main()
        self.assertEqual(caught.exception.code, 2)

    def test_concurrent_analysis_is_rejected_without_queue_and_slot_recovers(self):
        from lc_control.hydraulics import analyze_scene
        config, another = self.publish(), self.publish()
        entered, release, second_done = threading.Event(), threading.Event(), threading.Event()
        first_result, errors = [], []

        def blocked_solver(*args, **kwargs):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("test_solver_release_timeout")
            return analyze_scene(*args, **kwargs)

        def first():
            try:
                first_result.append(self.manager.analyze_config(config["id"]))
            except Exception as exc:
                errors.append(exc)

        def second():
            try:
                self.manager.analyze_config(another["id"])
            except WorkbenchError as exc:
                errors.append(exc)
            finally:
                second_done.set()

        with patch("lc_control.hydraulics.analyze_scene", side_effect=blocked_solver) as solve:
            one, two = threading.Thread(target=first), threading.Thread(target=second)
            one.start()
            try:
                self.assertTrue(entered.wait(2))
                two.start()
                self.assertTrue(second_done.wait(1), "second request must fail promptly rather than queue")
                self.assertEqual(solve.call_count, 1)
                self.assertEqual(errors[0].code, "analysis_busy")
                self.assertEqual(errors[0].status, 409)
                self.assertEqual(self.manager.get_config(config["id"])["status"], "published")
                self.assertEqual(list((self.manager.state / "analyses").iterdir()), [])
            finally:
                release.set()
                one.join(3)
                if two.ident is not None:
                    two.join(3)
        self.assertEqual(len(first_result), 1)
        self.assertEqual(len(errors), 1)
        # 首任务完成后新请求正常运行，忙时拒绝不污染第二配置。
        self.assertEqual(self.manager.analyze_config(another["id"])["config_id"], another["id"])

    def test_analysis_exception_and_close_release_capacity_without_early_state_takeover(self):
        config = self.publish()
        with patch("lc_control.hydraulics.analyze_scene", side_effect=RuntimeError("controlled failure")):
            with self.assertRaisesRegex(RuntimeError, "controlled failure"):
                self.manager.analyze_config(config["id"])
        self.assertFalse(self.manager._analysis_lock.locked())
        entered, release = threading.Event(), threading.Event()
        errors = []

        def slow(*args, **kwargs):
            entered.set()
            release.wait(3)
            return {"schema_version": "0.2"}

        def worker():
            try:
                self.manager.analyze_config(config["id"])
            except WorkbenchError as exc:
                errors.append(exc.code)

        with patch("lc_control.hydraulics.analyze_scene", side_effect=slow):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(2))
                self.manager.close()
                with self.assertRaises(WorkbenchError) as caught:
                    Workbench(self.root)
                self.assertEqual(caught.exception.code, "workbench_state_in_use")
            finally:
                release.set()
                thread.join(3)
        self.assertEqual(errors, ["workbench_closed"])
        self.assertEqual(list((self.manager.state / "analyses").iterdir()), [])
        self.assertIsNone(self.manager._state_lock)
        replacement = Workbench(self.root)
        replacement.close()


if __name__ == "__main__":
    unittest.main()
