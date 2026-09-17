"""工作台真实生命周期验收：配置版本、实际仿真、只读历史和执行权限。"""
import copy
import hashlib
import json
import shutil
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lc_control.storage import Store
from lc_control.workbench import Workbench, WorkbenchError, MASK

ROOT = Path(__file__).resolve().parents[1]


class WorkbenchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        shutil.copytree(ROOT / "examples", self.root / "examples")
        self.workbench = Workbench(self.root)
        self.scene = json.loads((ROOT / "examples/thermal_liquid_to_liquid.json").read_text())

    def tearDown(self):
        self.workbench.close()
        self.temp.cleanup()

    def publish(self, scene=None):
        draft = self.workbench.save_draft({"name": "测试配置", "scene": scene or self.scene})
        return self.workbench.publish(draft["id"], draft["revision"])

    def finish(self, job, timeout=4):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            current = next(j for j in self.workbench.list_jobs() if j["id"] == job["id"])
            if not current["active"]:
                return current
            time.sleep(0.01)
        self.fail("后台任务未按时结束")

    def test_draft_revision_publish_immutable_and_restart(self):
        draft = self.workbench.save_draft({"name": "第一版", "scene": self.scene})
        changed = copy.deepcopy(self.scene)
        changed["scene_id"] = "second-scene"
        updated = self.workbench.save_draft({"id": draft["id"], "expected_revision": 1,
                                            "name": "第二版", "scene": changed})
        self.assertEqual(updated["revision"], 2)
        with self.assertRaisesRegex(WorkbenchError, "配置已变化"):
            self.workbench.save_draft({"id": draft["id"], "expected_revision": 1,
                                       "name": "旧页面", "scene": self.scene})
        published = self.workbench.publish(updated["id"], 2)
        self.assertEqual(published["status"], "published")
        with self.assertRaises(WorkbenchError):
            self.workbench.save_draft({"id": published["id"], "expected_revision": 3,
                                       "name": "不允许覆盖", "scene": changed})
        self.workbench.close()
        self.workbench = Workbench(self.root)
        persisted = self.workbench.get_config(published["id"])
        self.assertEqual(persisted["scene_id"], "second-scene")
        self.assertEqual(persisted["revision"], 3)
        self.assertEqual(persisted["scene_hash"], published["scene_hash"])

    def test_masked_credentials_roundtrip_and_no_source_rejected(self):
        scene = copy.deepcopy(self.scene)
        scene["extensions"]["connection"] = {"password": "never-display-this", "secret_ref": "env:SITE_SECRET"}
        draft = self.workbench.save_draft({"name": "有凭据", "scene": scene})
        self.assertEqual(draft["scene"]["extensions"]["connection"]["password"], MASK)
        self.assertEqual(draft["scene"]["extensions"]["connection"]["secret_ref"], "env:SITE_SECRET")
        again = self.workbench.save_draft({"id": draft["id"], "expected_revision": 1,
                                           "name": "保留凭据", "scene": draft["scene"]})
        self.assertEqual(self.workbench._configs[draft["id"]]["scene"]["extensions"]["connection"]["password"], "never-display-this")
        derived = self.workbench.save_draft({"name": "复制", "source_config_id": again["id"], "scene": again["scene"]})
        self.assertEqual(self.workbench._configs[derived["id"]]["scene"]["extensions"]["connection"]["password"], "never-display-this")
        with self.assertRaisesRegex(WorkbenchError, "遮蔽凭据"):
            self.workbench.save_draft({"name": "无原始来源", "scene": draft["scene"]})
        self.assertNotIn("never-display-this", json.dumps(self.workbench.catalog()))

    def test_invalid_unbound_template_can_be_saved_but_not_published(self):
        unbound = json.loads((ROOT / "examples/field_template.UNBOUND.json").read_text())
        draft = self.workbench.save_draft({"name": "待点表核验", "scene": unbound})
        self.assertFalse(draft["validation"]["valid"])
        with self.assertRaisesRegex(WorkbenchError, "配置检查未通过"):
            self.workbench.publish(draft["id"], 1)

    def test_real_thermal_job_produces_dynamic_observations_and_gateway_receipts(self):
        config = self.publish()
        job = self.workbench.start_job({"config_id": config["id"], "kind": "thermal_sim",
                                        "mode": "control", "seconds": 40, "interval_s": 5, "speed": 1000})
        final = self.finish(job)
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["cycles"], 8)
        self.assertTrue(final["handoff_confirmed"])
        run = self.workbench.get_run(job["run_id"])
        self.assertEqual(run["source"], "synthetic_thermal")
        self.assertEqual(run["job"]["config_revision"], config["revision"])
        telemetry = self.workbench.events(job["run_id"], "telemetry")["items"]
        values = [e["payload"]["observations"]["cdu.sec_flow"]["value"] for e in telemetry]
        self.assertGreater(max(values) - min(values), 0.1)
        decisions = self.workbench.events(job["run_id"], "decision")["items"]
        self.assertTrue(any(e["payload"].get("receipt", {}).get("status") == "setpoint_confirmed" for e in decisions))
        self.assertTrue((self.workbench.state / "runs" / job["id"] / "commands.jsonl").exists())

    def test_stop_is_request_then_confirmed_handoff_not_pump_stop(self):
        config = self.publish()
        job = self.workbench.start_job({"config_id": config["id"], "seconds": 3600, "speed": 1})
        self.workbench.stop_job(job["id"])
        final = self.finish(job)
        self.assertEqual(final["status"], "stopped")
        self.assertTrue(final["handoff_confirmed"])
        self.assertIn("不是停泵", final["stop_meaning"])
        self.assertLess(final["elapsed_s"], 3600)

    def test_shadow_logs_without_dispatch_intents(self):
        config = self.publish()
        job = self.workbench.start_job({"config_id": config["id"], "mode": "shadow", "seconds": 10, "speed": 1000})
        self.assertEqual(self.finish(job)["status"], "completed")
        path = self.workbench.state / "runs" / job["id"] / "commands.jsonl"
        rows = [json.loads(line) for line in path.read_text().splitlines()]
        self.assertFalse(any(r["event"] == "dispatch_intent" for r in rows))
        self.assertIsNone(self.workbench.list_jobs()[0]["handoff_confirmed"])

    def test_failed_handoff_remains_unconfirmed_after_thread_finishes(self):
        config = self.publish()
        with patch("lc_control.adapters.MockCDUAdapter.release_to_local", return_value=False):
            job = self.workbench.start_job({"config_id": config["id"], "seconds": 5, "speed": 1000})
            final = self.finish(job)
        self.assertFalse(final["handoff_confirmed"])
        self.assertEqual(final["required_action"], "local_operator_required")
        self.assertFalse(self.workbench.stop_job(job["id"])["handoff_confirmed"])

    def test_missing_algorithm_observation_blocks_control_before_runner(self):
        scene = copy.deepcopy(self.scene)
        del scene["devices"]["CDU_01"]["initial_observations"]["cdu.sec_dp"]
        del scene["devices"]["CDU_01"]["guards"]["cdu.sec_dp"]
        config = self.publish(scene)
        self.assertTrue(config["validation"]["valid"])
        for mode in ("shadow", "control"):
            with self.assertRaisesRegex(WorkbenchError, "观测"):
                self.workbench.start_job({"config_id": config["id"], "mode": mode})

    def test_hardware_control_refused_before_network_and_shared_control_refused(self):
        scene = json.loads((ROOT / "examples/modbus_emulator.json").read_text())
        config = self.publish(scene)
        with patch("socket.create_connection", side_effect=AssertionError("should not connect")):
            with self.assertRaisesRegex(WorkbenchError, "服务未启用现场"):
                self.workbench.start_job({"config_id": config["id"], "kind": "modbus", "mode": "control"})
        shared = copy.deepcopy(self.scene)
        shared["control_domains"][0]["shared_hydraulics"] = True
        config = self.publish(shared)
        with self.assertRaisesRegex(WorkbenchError, "coordinator"):
            self.workbench.start_job({"config_id": config["id"], "mode": "control"})
        job = self.workbench.start_job({"config_id": config["id"], "mode": "monitor", "seconds": 5, "speed": 1000})
        self.assertEqual(self.finish(job)["status"], "completed")

    def test_network_monitor_uses_read_only_path_and_source_is_unverified(self):
        scene = json.loads((ROOT / "examples/modbus_monitor_only.json").read_text())
        config = self.publish(scene)
        # 不启动外部 TCP：模拟本轮连接失败，检查监测不会触发写入或接管。
        with patch("lc_control.modbus.ModbusCDUAdapter.read", side_effect=ConnectionError("offline")), \
             patch("lc_control.modbus.ModbusCDUAdapter.write", side_effect=AssertionError("write forbidden")), \
             patch("lc_control.modbus.ModbusCDUAdapter.release_to_local", side_effect=AssertionError("handoff forbidden")):
            job = self.workbench.start_job({"config_id": config["id"], "kind": "modbus", "mode": "monitor", "seconds": 1})
            final = self.finish(job)
        self.assertEqual(final["status"], "failed")
        self.assertEqual(final["source"], "modbus_network_unverified")
        self.assertIsNone(final["handoff_confirmed"])
        with self.assertRaisesRegex(WorkbenchError, "policy_required"):
            self.workbench.start_job({"config_id": config["id"], "kind": "modbus", "mode": "shadow"})

    def test_history_current_session_pagination_and_secret_redaction(self):
        output = self.root / "outputs" / "historical"
        store = Store(output / "runtime.sqlite")
        secret_scene = copy.deepcopy(self.scene)
        secret_scene["extensions"]["password"] = "historical-credential"
        store.put(1, "session", {"session": "old", "scene": self.scene})
        store.put(2, "telemetry", {"session": "old", "value": -1})
        store.put(3, "session", {"session": "new", "scene": secret_scene})
        for i in range(6):
            store.put(i + 4, "telemetry", {"session": "new", "value": i})
        store.put(12, "fault", {"session": "new", "reason": "url includes historical-credential"})
        store.close()
        catalog = self.workbench.catalog()
        run_id = next(r["id"] for r in catalog["runs"] if r["label"] == "historical")
        first = self.workbench.events(run_id, "telemetry", limit=2)
        second = self.workbench.events(run_id, "telemetry", after=first["next_cursor"], limit=2)
        self.assertEqual([e["payload"]["value"] for e in first["items"] + second["items"]], [0, 1, 2, 3])
        tail = self.workbench.events(run_id, "telemetry", limit=2, tail=True)
        self.assertEqual([e["payload"]["value"] for e in tail["items"]], [4, 5])
        self.assertNotIn("historical-credential", json.dumps(self.workbench.get_run(run_id)))
        self.assertNotIn("historical-credential", json.dumps(self.workbench.events(run_id, "fault")))

    def test_path_traversal_and_symlink_outside_outputs_are_not_exposed(self):
        outside = self.root / "private.sqlite"
        store = Store(outside)
        store.put(1, "session", {"scene": self.scene})
        store.close()
        output = self.root / "outputs" / "linked"
        output.mkdir(parents=True)
        (output / "runtime.sqlite").symlink_to(outside)
        self.assertFalse(self.workbench.catalog()["runs"])
        for identity in ("../../private.sqlite", str(outside)):
            with self.assertRaises(WorkbenchError):
                self.workbench.get_run(identity)
            with self.assertRaises(WorkbenchError):
                self.workbench.get_config(identity)

    def test_restarted_active_metadata_is_interrupted_and_never_resumed(self):
        config = self.publish()
        identity = "job-" + "a" * 32
        metadata = {"id": identity, "run_id": "run-" + "a" * 32, "config_id": config["id"],
                    "status": "running", "source": "modbus_network_unverified", "name": "旧现场运行", "mode": "control"}
        path = self.workbench.state / "jobs" / (identity + ".json")
        path.write_text(json.dumps(metadata))
        self.workbench.close()
        with patch("socket.create_connection", side_effect=AssertionError("should not resume")):
            self.workbench = Workbench(self.root)
        job = self.workbench.list_jobs()[0]
        self.assertEqual(job["status"], "interrupted")
        self.assertIsNone(job["handoff_confirmed"])
        self.assertFalse(job["active"])

    def test_second_instance_cannot_rewrite_active_job_metadata(self):
        config = self.publish()
        job = self.workbench.start_job({"config_id": config["id"], "seconds": 3600,
                                        "interval_s": 60, "speed": 1})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            running = next(j for j in self.workbench.list_jobs() if j["id"] == job["id"])
            if running["status"] == "running" and running["cycles"]:
                break
            time.sleep(0.01)
        self.assertEqual(running["status"], "running")
        self.assertGreater(running["cycles"], 0)
        path = self.workbench.state / "jobs" / (job["id"] + ".json")
        before = path.read_bytes()
        with self.assertRaises(WorkbenchError) as rejected:
            Workbench(self.root)
        self.assertEqual(rejected.exception.code, "workbench_state_in_use")
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.workbench.list_jobs()[0]["status"], "running")
        self.workbench.stop_job(job["id"])
        self.assertEqual(self.finish(job)["status"], "stopped")

    def test_failed_initialization_releases_state_directory_lock(self):
        state = self.root / "separate-state"
        with patch.object(Workbench, "_load_templates", side_effect=RuntimeError("fixture initialization failure")):
            with self.assertRaisesRegex(RuntimeError, "fixture initialization"):
                Workbench(self.root, state_dir=state)
        recovered = Workbench(self.root, state_dir=state)
        try:
            self.assertTrue(recovered.catalog()["configs"])
        finally:
            recovered.close()

    def test_close_is_idempotent_and_releases_state_lock(self):
        self.workbench.close()
        self.workbench.close()
        self.workbench = Workbench(self.root)
        self.assertTrue(self.workbench.catalog()["configs"])

    def test_only_published_and_bounded_job_parameters(self):
        draft = self.workbench.save_draft({"name": "未发布", "scene": self.scene})
        with self.assertRaisesRegex(WorkbenchError, "发布"):
            self.workbench.start_job({"config_id": draft["id"]})
        published = self.workbench.publish(draft["id"], 1)
        for changes in ({"seconds": 0}, {"seconds": float("nan")}, {"interval_s": True},
                        {"speed": 1001}, {"seconds": 86400, "interval_s": 0.1}):
            with self.subTest(changes=changes), self.assertRaises(WorkbenchError):
                self.workbench.start_job({"config_id": published["id"], **changes})

    def test_forecast_validation_is_read_only_and_invalid_replacement_preserves_input(self):
        config = self.publish()
        example = self.workbench.forecast_descriptor(config["id"])["example"]
        checked = self.workbench.validate_forecast(config["id"], example)
        self.assertTrue(checked["valid"])
        self.assertIsNone(self.workbench.get_forecast(config["id"])["input"])
        received = self.workbench.submit_forecast(config["id"], example)
        self.assertEqual(received["domains"][0]["status"], "ready")
        invalid = copy.deepcopy(example)
        invalid["forecast_id"] = "invalid-new-package"
        invalid["entries"][0]["liquid_fraction"] = 2
        self.assertFalse(self.workbench.validate_forecast(config["id"], invalid)["valid"])
        with self.assertRaises(WorkbenchError):
            self.workbench.submit_forecast(config["id"], invalid)
        self.assertEqual(self.workbench.get_forecast(config["id"])["input"]["forecast_id"], example["forecast_id"])
        self.assertEqual(self.workbench.list_jobs(), [])

    def test_forecast_persistence_clear_and_original_issuance_not_renewed(self):
        config = self.publish()
        example = self.workbench.forecast_descriptor(config["id"])["example"]
        self.workbench.submit_forecast(config["id"], example)
        self.workbench.close()
        self.workbench = Workbench(self.root)
        recovered = self.workbench.get_forecast(config["id"])
        self.assertEqual(recovered["input"]["issued_at"], 0)
        self.assertEqual(recovered["reference"]["now"], 0)
        self.assertIsNone(recovered["reference"]["job_id"])
        self.assertIsNone(self.workbench.clear_forecast(config["id"])["input"])
        self.workbench.close()
        self.workbench = Workbench(self.root)
        self.assertIsNone(self.workbench.get_forecast(config["id"])["input"])

    def test_forecast_changes_algorithm_demand_but_never_synthetic_actual_load(self):
        config = self.publish()
        baseline = self.workbench.start_job({"config_id": config["id"], "seconds": 5, "speed": 1000})
        self.finish(baseline)
        initial = self.workbench.get_run(baseline["run_id"])
        example = self.workbench.forecast_descriptor(config["id"])["example"]
        for entry in example["entries"]:
            for point in entry["samples"]:
                point["power_w"] = 65000
        self.workbench.submit_forecast(config["id"], example)
        prediction = self.workbench.start_job({"config_id": config["id"], "seconds": 5, "speed": 1000})
        self.finish(prediction)
        result = self.workbench.get_run(prediction["run_id"])
        before, after = initial["latest"]["decision"]["payload"], result["latest"]["decision"]["payload"]
        self.assertEqual(before["load_w_th"], 75000)
        self.assertEqual(after["load_w_th"], 104000)
        self.assertGreater(after["demand_flow_kg_s"], before["demand_flow_kg_s"])
        for run in (initial, result):
            self.assertEqual(run["latest"]["telemetry"]["payload"]["observations"]["cdu.liquid_load"]["value"], 75000)
        self.assertEqual(after["forecast_input"]["status"], "used")
        usage = self.workbench.events(prediction["run_id"], "forecast_usage")["items"]
        self.assertEqual(usage[0]["payload"]["forecast_id"], example["forecast_id"])
        self.assertEqual(self.workbench.get_forecast(config["id"], prediction["id"])["reference"]["now"], 5)

    def test_forecast_expiry_rejects_feedforward_and_preserves_feedback(self):
        config = self.publish()
        example = self.workbench.forecast_descriptor(config["id"])["example"]
        example["max_age_s"] = 10
        for entry in example["entries"]:
            for point in entry["samples"]:
                point["power_w"] = 65000
        self.workbench.submit_forecast(config["id"], example)
        job = self.workbench.start_job({"config_id": config["id"], "seconds": 20, "speed": 1000})
        self.assertEqual(self.finish(job)["status"], "completed")
        decisions = self.workbench.events(job["run_id"], "decision")["items"]
        self.assertEqual(decisions[0]["payload"]["forecast_input"]["status"], "used")
        last = decisions[-1]["payload"]
        self.assertEqual(last["forecast_input"]["reason"], "forecast_too_old")
        self.assertEqual(last["forecast_status"], "forecast_absent")
        self.assertEqual(last["load_w_th"], 75000)

    def test_forecast_id_cannot_change_content_and_unix_clock_not_used_in_simulation(self):
        config = self.publish()
        example = self.workbench.forecast_descriptor(config["id"])["example"]
        self.workbench.submit_forecast(config["id"], example)
        changed = copy.deepcopy(example)
        changed["entries"][0]["liquid_fraction"] = 0.5
        with self.assertRaisesRegex(WorkbenchError, "预测编号"):
            self.workbench.submit_forecast(config["id"], changed)
        now = time.time()
        external = copy.deepcopy(example)
        external.update(forecast_id="external-unix-example", source={"kind": "external", "name": "外部声明输入"},
                        clock_basis="unix", issued_at=now, valid_from=now, valid_until=now + 600)
        for entry in external["entries"]:
            for point in entry["samples"]:
                point["at"] += now
        self.workbench.submit_forecast(config["id"], external)
        job = self.workbench.start_job({"config_id": config["id"], "seconds": 5, "speed": 1000})
        self.finish(job)
        usage = self.workbench.get_forecast(config["id"], job["id"])["usage"][0]
        self.assertEqual(usage["reason"], "forecast_clock_mismatch")

    def test_forecast_archives_survive_replacement_and_clear_with_matching_hashes(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        a["forecast_id"] = "archive-A"
        first = self.workbench.submit_forecast(config["id"], a)
        job = self.workbench.start_job({"config_id": config["id"], "seconds": 5, "speed": 1000})
        self.finish(job)
        usage = self.workbench.get_forecast(config["id"], job["id"])["usage"][0]
        self.assertEqual(usage["input_sha256"], first["input_sha256"])
        b = copy.deepcopy(a)
        b["forecast_id"] = "archive-B"
        b["entries"][0]["samples"][0]["power_w"] += 1000
        second = self.workbench.submit_forecast(config["id"], b)
        self.assertNotEqual(first["input_sha256"], second["input_sha256"])
        self.assertIsNone(self.workbench.clear_forecast(config["id"])["input"])
        archive = self.workbench.state / "forecast_archive" / config["id"]
        self.assertEqual(len([p for p in archive.glob("*.json") if len(p.stem) == 64]), 2)
        for response in (first, second):
            content = (archive / (response["input_sha256"] + ".json")).read_bytes()
            self.assertEqual(hashlib.sha256(content).hexdigest(), response["input_sha256"])
            self.assertEqual(json.loads(content), response["input"])
        stored_usage = self.workbench.events(job["run_id"], "forecast_usage")["items"][0]["payload"]
        self.assertEqual(stored_usage["input_sha256"], first["input_sha256"])

    def test_archive_failure_never_activates_new_forecast(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        accepted = self.workbench.submit_forecast(config["id"], a)
        b = copy.deepcopy(a)
        b["forecast_id"] = "archive-fails"
        from lc_control.workbench import _atomic_json
        def fail_archive(path, payload, **kwargs):
            if "forecast_archive" in Path(path).parts:
                raise OSError("fixture disk failure")
            return _atomic_json(path, payload, **kwargs)
        with patch("lc_control.workbench._atomic_json", side_effect=fail_archive):
            with self.assertRaises(OSError):
                self.workbench.submit_forecast(config["id"], b)
        current = self.workbench.get_forecast(config["id"])
        self.assertEqual(current["input_sha256"], accepted["input_sha256"])
        record = json.loads((self.workbench.state / "forecasts" / (config["id"] + ".json")).read_text())
        self.assertEqual(record["input_sha256"], accepted["input_sha256"])

    def test_archive_rejects_symlink_paths_and_does_not_overwrite_corrupt_archive(self):
        config = self.publish()
        data = self.workbench.forecast_descriptor(config["id"])["example"]
        outside = self.root / "outside-archive"
        outside.mkdir()
        directory = self.workbench.state / "forecast_archive" / config["id"]
        directory.symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(WorkbenchError, "unsafe_forecast_archive_path"):
            self.workbench.submit_forecast(config["id"], data)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertIsNone(self.workbench.get_forecast(config["id"])["input"])
        directory.unlink()
        first = self.workbench.submit_forecast(config["id"], data)
        file = directory / (first["input_sha256"] + ".json")
        file.write_text("corrupt archive")
        with self.assertRaisesRegex(WorkbenchError, "哈希不一致"):
            self.workbench.submit_forecast(config["id"], data)
        self.assertEqual(file.read_text(), "corrupt archive")

    def test_forecast_id_cannot_change_content_after_another_package(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        a["forecast_id"] = "stable-A"
        self.workbench.submit_forecast(config["id"], a)
        b = copy.deepcopy(a); b["forecast_id"] = "stable-B"
        self.workbench.submit_forecast(config["id"], b)
        changed = copy.deepcopy(a); changed["entries"][0]["liquid_fraction"] = 0.5
        with self.assertRaises(WorkbenchError) as rejected:
            self.workbench.submit_forecast(config["id"], changed)
        self.assertEqual(rejected.exception.code, "forecast_id_conflict")
        self.assertEqual(self.workbench.get_forecast(config["id"])["input"]["forecast_id"], "stable-B")

    def test_forecast_id_binding_survives_clear(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        self.workbench.submit_forecast(config["id"], a)
        self.workbench.clear_forecast(config["id"])
        changed = copy.deepcopy(a); changed["entries"][0]["liquid_fraction"] = 0.5
        check = self.workbench.validate_forecast(config["id"], changed)
        self.assertFalse(check["valid"])
        self.assertEqual(check["errors"][0]["code"], "forecast_id_conflict")
        self.assertIsNone(self.workbench.get_forecast(config["id"])["input"])

    def test_forecast_id_binding_survives_restart_and_migrates_existing_archives(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        a["forecast_id"] = "old-A"
        self.workbench.submit_forecast(config["id"], a)
        b = copy.deepcopy(a); b["forecast_id"] = "old-B"
        self.workbench.submit_forecast(config["id"], b)
        # 模拟上一版已有内容归档、尚未建立编号索引；重启只迁移一次。
        path = self.workbench.state / "forecast_archive" / config["id"] / "id_index.json"
        path.unlink()
        self.workbench.close()
        self.workbench = Workbench(self.root)
        changed = copy.deepcopy(a); changed["entries"][0]["liquid_fraction"] = 0.5
        with self.assertRaises(WorkbenchError) as rejected:
            self.workbench.submit_forecast(config["id"], changed)
        self.assertEqual(rejected.exception.code, "forecast_id_conflict")
        index = json.loads(path.read_text())
        self.assertEqual(set(index["ids"]), {"old-A", "old-B"})

    def test_forecast_index_write_failure_never_activates_package(self):
        config = self.publish()
        a = self.workbench.forecast_descriptor(config["id"])["example"]
        first = self.workbench.submit_forecast(config["id"], a)
        b = copy.deepcopy(a); b["forecast_id"] = "index-write-fails"
        from lc_control.workbench import _atomic_json
        def fail_index(path, payload, **kwargs):
            if Path(path).name == "id_index.json":
                raise OSError("fixture index failure")
            return _atomic_json(path, payload, **kwargs)
        with patch("lc_control.workbench._atomic_json", side_effect=fail_index):
            with self.assertRaises(OSError):
                self.workbench.submit_forecast(config["id"], b)
        self.assertEqual(self.workbench.get_forecast(config["id"])["input_sha256"], first["input_sha256"])

    def test_forecast_migration_rejects_symlink_bad_hash_and_id_conflict(self):
        from lc_control.forecasting import validate_input
        for fault in ("symlink", "bad_hash", "id_conflict"):
            with self.subTest(fault=fault):
                config = self.publish()
                a = self.workbench.forecast_descriptor(config["id"])["example"]
                first = self.workbench.submit_forecast(config["id"], a)
                directory = self.workbench.state / "forecast_archive" / config["id"]
                self.workbench.clear_forecast(config["id"])
                (directory / "id_index.json").unlink()
                self.workbench._forecast_ids.pop(config["id"], None)
                if fault == "symlink":
                    (directory / ("a" * 64 + ".json")).symlink_to(directory / (first["input_sha256"] + ".json"))
                elif fault == "bad_hash":
                    (directory / ("a" * 64 + ".json")).write_text("{}")
                else:
                    changed = copy.deepcopy(a); changed["entries"][0]["liquid_fraction"] = 0.5
                    content = self.workbench._forecast_bytes(validate_input(self.scene, changed, 0))
                    (directory / (hashlib.sha256(content).hexdigest() + ".json")).write_bytes(content)
                check = self.workbench.validate_forecast(config["id"], a)
                self.assertFalse(check["valid"])
                self.assertIsNone(self.workbench.get_forecast(config["id"])["input"])


if __name__ == "__main__":
    unittest.main()
