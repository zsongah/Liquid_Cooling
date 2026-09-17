"""把同一发布版本的独立控制域汇集成机房视图。

这里只查询各运行已经落盘的观测，不创建控制器、不估计缺失分支量，也不把
不同时刻/不同时钟的 CDU 数值相加冒充全站瞬时功率。每个域保留来源、时间、
任务状态与控制交接证据；机房展示层不能承担共享水路协调器的职责。
"""
import math
import time

from .workbench import WorkbenchError


def site_snapshot(workbench, config_id):
    """按域选择活动任务，否则选择最近任务；只组合相同配置版本的记录。"""
    config = workbench.get_config(config_id)
    validation = config.get("validation", {})
    if config["scene"].get("schema_version") == "0.2":
        # 分析域不等同于旧版单 CDU 运行域，即使配置/域标签相似也绝不串用
        # 历史任务或测量。模型估计只能从独立 analysis 接口获得。
        domains = [{"domain_id": d["id"], "member_asset_ids": d.get("member_asset_ids", []),
                    "circuit_ids": d.get("circuit_ids", []), "branch_ids": d.get("branch_ids", []),
                    "actuator_ids": d.get("actuator_ids", []), "cdu_id": None,
                    "status": "analysis_only", "source": "configuration_only",
                    "mode": "analysis_only", "active": False, "control_active": False,
                    "selected_run_id": None, "clock_basis": None,
                    "latest_telemetry": None, "latest_decision": None, "latest_fault": None,
                    "freshness_status": "not_sampled", "measurement_timestamp": None,
                    "sample_age_s": None, "handoff_confirmed": None}
                   for d in config["scene"].get("control_domains", [])]
        return {"config_id": config_id, "config_revision": config["revision"],
                "schema_version": "0.2", "scene_id": config["scene"].get("scene_id"),
                "source": "configuration_only", "hardware_writes": False,
                "topology": validation.get("topology"), "domains": domains,
                "telemetry_by_asset": {}, "site_power_w": None,
                "coverage": {"configured_domains": len(domains), "observed_domains": 0, "active_domains": 0},
                "scope": "analysis_only", "analysis_ready": validation.get("analysis_ready", False),
                "aggregation_note": "仅展示 0.2 物理配置；无设备连接、实测遥测或已执行动作。水力分析结果另行标记为模型估计。"}
    jobs = [j for j in workbench.list_jobs() if j.get("config_id") == config_id]
    domains, telemetry = [], {}
    for domain in config["scene"].get("control_domains", []):
        candidates = [j for j in jobs if j.get("domain_id") == domain["id"]]
        candidates.sort(key=lambda j: (bool(j.get("active")), j.get("updated_at", 0)), reverse=True)
        job = candidates[0] if candidates else None
        item = {"domain_id": domain["id"], "cdu_id": domain["cdu_id"],
                "served_racks": domain.get("served_racks", []),
                "selected_run_id": job.get("run_id") if job else None,
                "status": job.get("status") if job else "not_started",
                "source": job.get("source") if job else None,
                "requested_mode": job.get("mode") if job else None,
                "last_runtime_mode": job.get("runtime_mode", job.get("mode")) if job else None,
                "clock_basis": ("simulation" if job.get("kind") == "thermal_sim" else "unix") if job else None,
                "active": bool(job and job.get("active")),
                "updated_at": job.get("updated_at") if job else None,
                "handoff_confirmed": job.get("handoff_confirmed") if job else None,
                "latest_telemetry": None, "latest_decision": None, "latest_fault": None,
                "read_error": None, "job_error": job.get("error") if job else None,
                "required_action": job.get("required_action") if job else None,
                "is_historical": bool(job and not job.get("active")),
                "measurement_timestamp": None, "sample_age_s": None, "freshness_status": "not_sampled"}
        item["mode"] = item["last_runtime_mode"] if item["active"] else (
            "local_handoff_confirmed" if item["handoff_confirmed"] else "inactive")
        item["control_active"] = item["active"] and item["last_runtime_mode"] == "control"
        if job:
            try:
                run = workbench.get_run(job["run_id"])
                fault = run.get("latest", {}).get("fault")
                item["latest_fault"] = fault.get("payload") if fault else None
                for kind in ("telemetry", "decision"):
                    event = run.get("latest", {}).get(kind)
                    payload = event.get("payload") if event else None
                    # 拒绝把意外的其他域/资产记录展示为当前设备；旧历史无域 ID 时
                    # 仍须有明确且匹配的采集资产 ID，不能按列表位置推断。
                    if payload and payload.get("asset_id") == domain["cdu_id"] and payload.get("domain_id", domain["id"]) == domain["id"]:
                        item["latest_" + kind] = payload
                if item["latest_telemetry"]:
                    telemetry[domain["cdu_id"]] = item["latest_telemetry"]
                    at = item["latest_telemetry"].get("timestamp")
                    item["measurement_timestamp"] = at
                    if item["is_historical"]:
                        item["freshness_status"] = "historical"
                    elif item["clock_basis"] == "simulation":
                        item["freshness_status"] = "simulation_time"
                    elif isinstance(at, (int, float)) and not isinstance(at, bool) and math.isfinite(at):
                        item["sample_age_s"] = time.time() - at
                        max_age = config["scene"]["devices"][domain["cdu_id"]].get("max_data_age_s", 0)
                        item["freshness_status"] = "fresh" if 0 <= item["sample_age_s"] <= max_age else "stale"
                    else:
                        item["freshness_status"] = "invalid_timestamp"
            except WorkbenchError as error:
                # 尚未建立数据库、活动采集暂时失败都作为缺数据呈现，其他域仍可读。
                item["read_error"] = error.code
        domains.append(item)
    return {"config_id": config_id, "config_revision": config["revision"],
            "scene_id": config["scene"].get("scene_id"),
            "topology": validation.get("topology"), "domains": domains,
            "telemetry_by_asset": telemetry,
            "coverage": {"configured_domains": len(domains), "observed_domains": len(telemetry),
                         "active_domains": sum(d["active"] for d in domains)},
            "scope": "independent_domains_only", "site_power_w": None,
            "aggregation_note": "域记录可能来自不同采集时刻、仿真时钟或历史任务，未计算全站瞬时功率。"}
