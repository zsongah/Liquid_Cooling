"""验证机房视图组合真实独立域记录，而不是复制单 CDU 遥测。"""
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from lc_control.site_view import site_snapshot
from lc_control.workbench import Workbench

ROOT = Path(__file__).resolve().parents[1]


class SiteViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.manager = Workbench(self.temp.name)
        self.scene = json.loads((ROOT / "examples/workbench_independent_cdus.json").read_text())
        self.config = self.publish(self.scene)

    def tearDown(self):
        self.manager.close()
        self.temp.cleanup()

    def publish(self, scene):
        draft = self.manager.save_draft({"scene": scene, "name": "独立域机房验收"})
        return self.manager.publish(draft["id"], draft["revision"])

    def run_domain(self, domain):
        job = self.manager.start_job({"config_id": self.config["id"], "domain_id": domain["id"],
            "mode": "control", "seconds": 15, "interval_s": 5, "speed": 100})
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            found = next(j for j in self.manager.list_jobs() if j["id"] == job["id"])
            if not found["active"]:
                self.assertEqual(found["status"], "completed", found)
                return found
            time.sleep(0.02)
        self.fail("bounded simulation did not complete")

    def test_unstarted_domains_have_no_fabricated_observations(self):
        view = site_snapshot(self.manager, self.config["id"])
        self.assertEqual(view["coverage"], {"configured_domains": 2, "observed_domains": 0, "active_domains": 0})
        self.assertEqual(view["telemetry_by_asset"], {})
        self.assertTrue(all(d["status"] == "not_started" for d in view["domains"]))
        self.assertIsNone(view["site_power_w"])

    def test_two_domains_retain_their_own_flow_pressure_and_session(self):
        for domain in self.scene["control_domains"]:
            self.run_domain(domain)
        view = site_snapshot(self.manager, self.config["id"])
        self.assertEqual(view["coverage"]["observed_domains"], 2)
        self.assertEqual(view["coverage"]["active_domains"], 0)
        samples = list(view["telemetry_by_asset"].values())
        self.assertNotEqual(samples[0]["session"], samples[1]["session"])
        self.assertEqual({d["latest_decision"]["policy_quantity"] for d in view["domains"]}, {"cdu.dp_sp", "cdu.sec_flow_sp"})
        self.assertTrue(all(d["handoff_confirmed"] for d in view["domains"]))
        self.assertTrue(all(d["mode"] == "local_handoff_confirmed" and not d["control_active"] for d in view["domains"]))
        self.assertTrue(all(d["freshness_status"] == "historical" for d in view["domains"]))
        self.assertIsNone(view["site_power_w"], "different runs must not imply synchronous site power")

    def test_clone_does_not_inherit_other_configuration_telemetry(self):
        self.run_domain(self.scene["control_domains"][0])
        another = self.publish(copy.deepcopy(self.scene))
        self.assertEqual(site_snapshot(self.manager, another["id"])["telemetry_by_asset"], {})

    def test_latest_run_is_selected_instead_of_duplicate_asset_values(self):
        domain = self.scene["control_domains"][0]
        old = self.run_domain(domain)
        new = self.run_domain(domain)
        view = site_snapshot(self.manager, self.config["id"])
        shown = next(d for d in view["domains"] if d["domain_id"] == domain["id"])
        self.assertEqual(shown["selected_run_id"], new["run_id"])
        self.assertNotEqual(shown["selected_run_id"], old["run_id"])
        self.assertEqual(view["coverage"]["observed_domains"], 1)

    def test_active_run_wins_over_more_recently_completed_metadata(self):
        domain = self.scene["control_domains"][0]
        old = self.run_domain(domain)
        active = self.manager.start_job({"config_id": self.config["id"], "domain_id": domain["id"],
            "mode": "shadow", "seconds": 300, "interval_s": 5, "speed": 1})
        # 对旧任务的后续审计更新不能把正在采集的任务从机房视图挤掉。
        self.manager._update_job(old["id"], updated_at=time.time() + 100)
        view = site_snapshot(self.manager, self.config["id"])
        shown = next(d for d in view["domains"] if d["domain_id"] == domain["id"])
        self.assertEqual(shown["selected_run_id"], active["run_id"])
        self.assertTrue(shown["active"])
        self.assertFalse(shown["is_historical"])
        self.manager.stop_job(active["id"])


if __name__ == "__main__":
    unittest.main()
