"""工作台端到端边界测试：配置生命周期、真实仿真事件与请求隔离。"""
import copy
import http.client
import json
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path

from lc_control.console_server import ConsoleServer
from lc_control.workbench import Workbench


ROOT = Path(__file__).resolve().parents[1]


class ConsoleServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "examples").mkdir()
        shutil.copy(ROOT / "examples/thermal_liquid_to_liquid.json", self.root / "examples/thermal.json")
        self.scene = json.loads((self.root / "examples/thermal.json").read_text())
        self.manager = Workbench(self.root)
        self.server = ConsoleServer(("127.0.0.1", 0), self.manager)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]
        self.cookie = ""
        self.csrf = ""
        status, body, headers = self.request("GET", "/api/session", authenticated=False)
        self.assertEqual(status, 200)
        self.cookie = dict(headers)["Set-Cookie"].split(";", 1)[0]
        self.csrf = body["csrf_token"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.manager.close()
        self.thread.join(3)
        self.temp.cleanup()

    def request(self, method, path, payload=None, extra=None, authenticated=True, raw=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=8)
        headers = {"Content-Type": "application/json"}
        if authenticated:
            headers.update({"Cookie": self.cookie, "X-LC-CSRF": self.csrf})
        if extra:
            headers.update(extra)
        data = raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        conn.request(method, path, body=data, headers=headers)
        response = conn.getresponse()
        body = response.read()
        result = json.loads(body) if response.getheader("Content-Type", "").startswith("application/json") else body
        result_tuple = response.status, result, response.getheaders()
        conn.close()
        return result_tuple

    def test_draft_publish_simulate_and_query_real_cycle(self):
        status, draft, _ = self.request("POST", "/api/configs/draft", {"name": "端到端配置", "scene": self.scene})
        self.assertEqual(status, 200, draft)
        self.assertTrue(draft["validation"]["valid"])
        self.assertEqual(draft["status"], "draft")
        status, _, _ = self.request("POST", "/api/jobs", {"config_id": draft["id"], "kind": "thermal_sim"})
        self.assertGreaterEqual(status, 400, "draft must not become running configuration")
        status, published, _ = self.request("POST", "/api/configs/%s/publish" % draft["id"], {"expected_revision": draft["revision"]})
        self.assertEqual(status, 200, published)
        self.assertEqual(published["status"], "published")
        status, job, _ = self.request("POST", "/api/jobs", {"config_id": published["id"], "domain_id": "CDU_DOMAIN_01", "kind": "thermal_sim", "mode": "control", "seconds": 20, "interval_s": 5, "speed": 100})
        self.assertEqual(status, 202, job)
        deadline = time.monotonic() + 6
        events = {"items": []}
        while time.monotonic() < deadline:
            code, events, _ = self.request("GET", "/api/runs/%s/events?kind=decision&limit=10&tail=1" % job["run_id"])
            if code == 200 and events.get("items"):
                break
            time.sleep(0.03)
        self.assertTrue(events.get("items"), events)
        decision = events["items"][0]["payload"]
        self.assertIn("cycle_id", decision)
        self.assertEqual(decision["policy_quantity"], "cdu.dp_sp")
        self.assertIn("raw_control_target", decision)
        self.assertIn("limited_control_target", decision)
        status, run, _ = self.request("GET", "/api/runs/" + job["run_id"])
        self.assertEqual(status, 200, run)
        self.assertIn("telemetry", run["latest"])
        self.assertNotEqual(run.get("source"), "field_verified")

    def test_csrf_origin_host_and_session_are_independent_gates(self):
        body = {"name": "should-not-save", "scene": self.scene}
        for headers, authenticated in (({}, False), ({"X-LC-CSRF": "wrong"}, True),
                                       ({"Origin": "https://other.example"}, True),
                                       ({"Host": "attacker.example:%s" % self.port}, True)):
            status, _, _ = self.request("POST", "/api/configs/draft", body, headers, authenticated)
            self.assertIn(status, (401, 403))
        status, _, _ = self.request("GET", "/api/catalog", authenticated=False)
        self.assertEqual(status, 401)

    def test_published_config_and_revision_cannot_be_overwritten(self):
        _, draft, _ = self.request("POST", "/api/configs/draft", {"name": "版本测试", "scene": self.scene})
        data = {"id": draft["id"], "name": "修改", "scene": self.scene, "expected_revision": draft["revision"] + 5}
        status, _, _ = self.request("POST", "/api/configs/draft", data)
        self.assertEqual(status, 409)
        _, published, _ = self.request("POST", "/api/configs/%s/publish" % draft["id"], {"expected_revision": draft["revision"]})
        data["expected_revision"] = published["revision"]
        status, _, _ = self.request("POST", "/api/configs/draft", data)
        self.assertGreaterEqual(status, 400)

    def test_strict_json_and_no_arbitrary_file_api(self):
        for raw in (b'{"scene": {}, "scene": {}}', b'{"scene": {"x": NaN}}'):
            status, _, _ = self.request("POST", "/api/configs/validate", raw=raw)
            self.assertEqual(status, 400)
        for path in ("/../../etc/passwd", "/api/runs/../../secret", "/api/exec"):
            status, _, _ = self.request("GET", path)
            self.assertGreaterEqual(status, 400)

    def test_invalid_scene_validation_is_reported_without_execution(self):
        invalid = copy.deepcopy(self.scene)
        invalid["devices"]["CDU_01"]["policy"]["control_quantity"] = "not.a.quantity"
        status, result, _ = self.request("POST", "/api/configs/validate", {"scene": invalid})
        self.assertEqual(status, 200)
        self.assertFalse(result["valid"])
        self.assertTrue(result["errors"])
        self.assertEqual(self.manager.list_jobs(), [])

    def test_validation_redacts_asset_extension_credentials(self):
        scene = copy.deepcopy(self.scene)
        scene["assets"][0]["password"] = "test-secret-never-return"
        status, result, _ = self.request("POST", "/api/configs/validate", {"scene": scene})
        self.assertEqual(status, 200)
        self.assertNotIn("test-secret-never-return", json.dumps(result))

    def test_power_forecast_reaches_engine_without_changing_plant_load(self):
        """HTTP 输入必须真正提高算法需求，不能只出现在预测页面。"""
        _, draft, _ = self.request("POST", "/api/configs/draft", {"name": "预测联调", "scene": self.scene})
        _, config, _ = self.request("POST", "/api/configs/%s/publish" % draft["id"], {"expected_revision": draft["revision"]})
        base = "/api/configs/" + config["id"]
        code, descriptor, _ = self.request("GET", base + "/forecast/descriptor")
        self.assertEqual(code, 200, descriptor)
        forecast = descriptor["example"]
        forecast["forecast_id"] = "http-feedforward-001"
        for entry in forecast["entries"]:
            entry["liquid_fraction"] = 0.8
            for sample in entry["samples"]:
                sample["power_w"] = 65000
        code, validated, _ = self.request("POST", base + "/forecast/validate", forecast)
        self.assertEqual(code, 200)
        self.assertTrue(validated["valid"], validated)
        _, empty, _ = self.request("GET", base + "/forecast")
        self.assertIsNone(empty["input"], "validation must not activate feedforward")
        code, received, _ = self.request("POST", base + "/forecast", forecast)
        self.assertEqual(code, 200, received)
        self.assertEqual(received["input"]["forecast_id"], "http-feedforward-001")
        self.assertEqual(self.manager.list_jobs(), [], "import must not start control")
        code, job, _ = self.request("POST", "/api/jobs", {"config_id": config["id"], "domain_id": "CDU_DOMAIN_01",
            "kind": "thermal_sim", "mode": "control", "seconds": 20, "interval_s": 5, "speed": 100})
        self.assertEqual(code, 202, job)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if not next(j for j in self.manager.list_jobs() if j["id"] == job["id"])["active"]:
                break
            time.sleep(0.03)
        _, usage, _ = self.request("GET", base + "/forecast?job_id=" + job["id"])
        self.assertTrue(any(u["status"] == "used" for u in usage["usage"]), usage)
        _, run, _ = self.request("GET", "/api/runs/" + job["run_id"])
        decision = run["latest"]["decision"]["payload"]
        observations = run["latest"]["telemetry"]["payload"]["observations"]
        self.assertEqual(decision["forecast_status"], "forecast_used")
        self.assertAlmostEqual(decision["load_w_th"], 104000)
        self.assertAlmostEqual(observations["cdu.liquid_load"]["value"], 75000)
        _, view, _ = self.request("GET", base + "/site")
        self.assertEqual(view["coverage"]["observed_domains"], 1)
        self.assertFalse(view["domains"][0]["control_active"])
        _, cleared, _ = self.request("POST", base + "/forecast/clear", {})
        self.assertIsNone(cleared["input"])

    def test_forecast_mutations_share_session_guard_and_reject_malformed_power(self):
        _, draft, _ = self.request("POST", "/api/configs/draft", {"name": "预测边界", "scene": self.scene})
        _, config, _ = self.request("POST", "/api/configs/%s/publish" % draft["id"], {"expected_revision": draft["revision"]})
        base = "/api/configs/" + config["id"] + "/forecast"
        code, _, _ = self.request("POST", base, {}, authenticated=False)
        self.assertEqual(code, 401)
        code, _, _ = self.request("POST", base + "/clear", {}, extra={"X-LC-CSRF": "bad"})
        self.assertEqual(code, 403)
        code, _, _ = self.request("POST", base, {})
        self.assertIn(code, (400, 422))
        _, saved, _ = self.request("GET", base)
        self.assertIsNone(saved["input"])


if __name__ == "__main__":
    unittest.main()
