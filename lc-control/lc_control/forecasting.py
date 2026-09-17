"""有界的外部功率预测接入与拓扑聚合，不训练或伪造功率预测模型。

输入功率属于节点或机柜的电功率，必须显式给出液冷捕热比例。按已声明资产归属
聚合为各独立 CDU 域的未来液冷热负荷；没有支路阀控制、负荷迁移或共享域调度。
预测只准备现有 FlowPolicy 的前馈包，实际动作仍受观测、限幅、控制权和网关约束。
"""
import copy
import math
import re
from collections import defaultdict

MAX_ENTRIES = 512
MAX_SAMPLES = 721
MAX_POINTS = 50000
MAX_SPAN_S = 86400
MAX_AGE_S = 3600


class ForecastError(ValueError):
    """原因码保持稳定，客户端可直接显示明确的拒绝依据。"""
    def __init__(self, code, message=None):
        self.code, self.message = code, message or code
        super().__init__(self.message)


def _require(condition, code, message=None):
    if not condition:
        raise ForecastError(code, message)


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _topology(scene):
    assets = {a["id"]: a for a in scene["assets"]}
    parents, children, owners = {}, defaultdict(list), defaultdict(list)
    for asset in assets.values():
        if asset["kind"] == "node":
            parent = asset.get("parent_id")
            _require(parent in assets and assets[parent]["kind"] == "rack", "node_requires_rack_parent")
            parents[asset["id"]] = parent
            children[parent].append(asset["id"])
    for domain in scene["control_domains"]:
        for rack in domain["served_racks"]:
            owners[rack].append(domain["id"])
    return assets, parents, children, owners


def descriptor(scene, config_id, revision):
    """契约与模板来自当前发布场景；模板明确 synthetic，未提交前不会被使用。"""
    if scene.get("schema_version") == "0.2":
        return {"schema_version": "1", "scene_schema_version": "0.2",
                "config_id": config_id, "config_revision": revision,
                "status": "unsupported", "reason": "analysis_schema_forecast_unsupported",
                "hardware_writes": False, "unit": "W_e", "clock_bases": [],
                "assets": [], "domains": [{"id": d["id"], "status": "unsupported"}
                                           for d in scene.get("control_domains", [])],
                "example": None, "limits": {},
                "notes": ["0.2 当前仅支持离线水力分析，尚未接入节点功率预测或支路冷量分配。"]}
    assets, parents, children, owners = _topology(scene)
    supported = [{"id": a["id"], "kind": a["kind"], "label": a.get("label", a["id"]),
                  "parent_id": a.get("parent_id"), "rack_id": parents.get(a["id"], a["id"])}
                 for a in assets.values() if a["kind"] in {"rack", "node"}]
    domains = []
    rack_power = {}
    for domain in scene["control_domains"]:
        policy = scene["devices"][domain["cdu_id"]].get("policy", {})
        domains.append({"id": domain["id"], "cdu_id": domain["cdu_id"], "served_racks": domain["served_racks"],
                        "shared_hydraulics": domain["shared_hydraulics"], "horizon_s": policy.get("horizon_s"),
                        "max_liquid_load_w": policy.get("max_liquid_load_w")})
        for rack in domain["served_racks"]:
            rack_power[rack] = min(60000, policy.get("max_liquid_load_w", 100000) * 0.6 / max(1, len(domain["served_racks"])) / 0.8)
    example = {"forecast_id": "synthetic-example-change-id", "source": {"kind": "synthetic", "name": "手动预测接口试验"},
               "clock_basis": "simulation", "issued_at": 0, "valid_from": 0, "valid_until": 600,
               "max_age_s": 600, "unit": "W_e", "selection": "central", "entries": []}
    for rack, power in rack_power.items():
        example["entries"].append({"asset_id": rack, "liquid_fraction": 0.8,
            "samples": [{"at": t, "power_w": round(power * factor, 3)} for t, factor in ((0, 0.8), (30, 1.3), (120, 1.3), (300, 0.9), (600, 0.8))]})
    return {"schema_version": "1", "config_id": config_id, "config_revision": revision,
            "unit": "W_e", "clock_bases": ["simulation", "unix"], "assets": supported,
            "domains": domains, "example": example,
            "limits": {"max_entries": MAX_ENTRIES, "max_samples_per_entry": MAX_SAMPLES,
                       "max_points": MAX_POINTS, "max_span_s": MAX_SPAN_S, "max_age_s": MAX_AGE_S},
            "notes": ["simulation 使用每次运行起点为 0 的相对秒，必须标为 synthetic；unix 使用 Unix 秒，必须声明 external。",
                      "同一机柜只能提交整柜预测或全部已配置节点预测，不能重复相加；节点全覆盖不代表未建模的机柜辅助负荷已包含。",
                      "各域需要完整机柜覆盖；共享水路或重复归属不提供独立冷量分配。",
                      "使用相同时间网格并分段线性插值，不在样本区间外外推。",
                      "该接口不产生预测、不修改实际负荷；前馈只提高需求，不减少实时热安全反馈。"]}


def validate_input(scene, payload, now):
    """验证输入和时效，返回只包含契约字段的副本；失败不替换此前有效输入。"""
    _require(scene.get("schema_version") != "0.2", "analysis_schema_forecast_unsupported")
    _require(isinstance(payload, dict), "forecast_object_required")
    allowed = {"forecast_id", "source", "clock_basis", "issued_at", "valid_from", "valid_until", "max_age_s", "unit", "selection", "entries", "job_id"}
    _require(not set(payload) - allowed, "unknown_forecast_field")
    identity = payload.get("forecast_id")
    _require(isinstance(identity, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", identity), "invalid_forecast_id")
    source = payload.get("source")
    _require(isinstance(source, dict) and set(source) == {"kind", "name"}, "source_kind_and_name_required")
    _require(isinstance(source["name"], str) and 0 < len(source["name"].strip()) <= 160, "invalid_source_name")
    clock = payload.get("clock_basis")
    _require(isinstance(clock, str) and clock in {"simulation", "unix"}, "invalid_clock_basis")
    _require(source.get("kind") == {"simulation": "synthetic", "unix": "external"}[clock], "source_clock_mismatch")
    _require(payload.get("unit") == "W_e", "electrical_power_unit_must_be_W_e")
    selection = payload.get("selection", "central")
    _require(isinstance(selection, str) and selection in {"central", "upper_bound"}, "invalid_uncertainty_selection")
    issued, start, end, age = (payload.get("issued_at"), payload.get("valid_from"), payload.get("valid_until"), payload.get("max_age_s", 300))
    _require(all(_finite(x) for x in (issued, start, end, age, now)), "nonfinite_forecast_time")
    _require(0 <= issued <= start < end and end - start <= MAX_SPAN_S, "invalid_forecast_validity_range")
    _require(0 < age <= MAX_AGE_S, "invalid_max_age_s")
    _require(issued <= now, "forecast_issued_in_future")
    _require(now - issued <= age, "forecast_too_old")
    _require(now < end, "forecast_expired")
    entries = payload.get("entries")
    _require(isinstance(entries, list) and 0 < len(entries) <= MAX_ENTRIES, "invalid_forecast_entries")
    assets, parents, children, owners = _topology(scene)
    seen, coverage, times = set(), defaultdict(set), None
    total_points = 0
    canonical_entries = []
    for entry in entries:
        _require(isinstance(entry, dict) and set(entry) == {"asset_id", "liquid_fraction", "samples"}, "invalid_entry_fields")
        asset_id = entry.get("asset_id")
        _require(isinstance(asset_id, str) and asset_id in assets and assets[asset_id]["kind"] in {"rack", "node"}, "unknown_forecast_asset")
        _require(asset_id not in seen, "duplicate_forecast_asset")
        seen.add(asset_id)
        rack = parents.get(asset_id, asset_id)
        _require(len(owners[rack]) == 1, "ambiguous_or_unserved_rack", "机柜需唯一归属于控制域，重复供冷分配需协调器。")
        coverage[rack].add(asset_id)
        fraction = entry.get("liquid_fraction")
        _require(_finite(fraction) and 0 <= fraction <= 1, "invalid_liquid_fraction")
        samples = entry.get("samples")
        _require(isinstance(samples, list) and 2 <= len(samples) <= MAX_SAMPLES, "invalid_sample_count")
        total_points += len(samples)
        _require(total_points <= MAX_POINTS, "forecast_too_many_points")
        grid, canonical_samples = [], []
        for sample in samples:
            _require(isinstance(sample, dict) and not set(sample) - {"at", "power_w", "lower_power_w", "upper_power_w"}, "invalid_sample_fields")
            at, power = sample.get("at"), sample.get("power_w")
            _require(_finite(at) and start <= at <= end, "sample_outside_validity")
            _require(_finite(power) and 0 <= power <= 1e9, "invalid_forecast_power")
            lower, upper = sample.get("lower_power_w", power), sample.get("upper_power_w", power)
            _require(_finite(lower) and _finite(upper) and 0 <= lower <= power <= upper <= 1e9, "invalid_uncertainty_bounds")
            _require(selection != "upper_bound" or "upper_power_w" in sample, "upper_bound_required_for_every_sample")
            grid.append(at)
            canonical_samples.append(copy.deepcopy(sample))
        _require(grid == sorted(set(grid)), "sample_times_not_strictly_increasing")
        _require(grid[0] == start and grid[-1] == end, "samples_must_cover_validity_endpoints")
        _require(times is None or times == grid, "unaligned_forecast_time_grid")
        times = grid
        canonical_entries.append({"asset_id": asset_id, "liquid_fraction": fraction, "samples": canonical_samples})
    for rack, members in coverage.items():
        if rack in members:
            _require(members == {rack}, "parent_and_child_double_counting")
        else:
            _require(bool(children[rack]) and members == set(children[rack]), "incomplete_node_coverage")
    return {"forecast_id": identity, "source": copy.deepcopy(source), "clock_basis": clock,
            "issued_at": issued, "valid_from": start, "valid_until": end, "max_age_s": age,
            "unit": "W_e", "selection": selection, "entries": canonical_entries}


def _interpolate(samples, at, key):
    """仅在经验证的区间内线性插值；调用方不能用它预测区间外的负荷。"""
    for a, b in zip(samples, samples[1:]):
        if a["at"] <= at <= b["at"]:
            weight = (at - a["at"]) / (b["at"] - a["at"])
            return a[key] + weight * (b[key] - a[key])
    raise ForecastError("forecast_does_not_cover_horizon")


def resolve_domain(scene, domain_id, package, now, clock_basis):
    """每周期从原始包重新检查时效；返回派生 FlowPolicy 包和可审计聚合结果。

    派生包 issued_at=now 是本次物化时间，并保留 source_issued_at。它不是原始预报
    更新时间；只有原始发行时间/有效期/最大年龄都通过，才生成这份内部即时包。
    """
    if scene.get("schema_version") == "0.2":
        return None, {"domain_id": domain_id, "clock_basis": None, "now": None,
                      "status": "unsupported", "reason": "analysis_schema_forecast_unsupported",
                      "forecast_id": None, "peak_liquid_w": None, "series": [], "racks": []}
    domain = next(d for d in scene["control_domains"] if d["id"] == domain_id)
    policy = scene["devices"][domain["cdu_id"]].get("policy")
    result = {"domain_id": domain_id, "clock_basis": clock_basis, "now": now,
              "status": "absent", "reason": "forecast_absent", "forecast_id": None,
              "peak_liquid_w": None, "series": [], "racks": []}
    if not package:
        return None, result
    result.update(forecast_id=package["forecast_id"], source=package["source"], source_issued_at=package["issued_at"],
                  max_age_s=package["max_age_s"], selection=package["selection"])
    try:
        _require(package["clock_basis"] == clock_basis, "forecast_clock_mismatch")
        _require(package["issued_at"] <= now, "forecast_issued_in_future")
        _require(now - package["issued_at"] <= package["max_age_s"], "forecast_too_old")
        _require(package["valid_from"] <= now, "forecast_not_yet_valid")
        _require(now < package["valid_until"], "forecast_expired")
        _require(not domain["shared_hydraulics"], "shared_hydraulics_coordinator_required")
        _require(bool(policy), "automatic_policy_not_configured")
        finish = now + policy["horizon_s"]
        _require(finish <= package["valid_until"], "forecast_does_not_cover_horizon")
        _, parents, _, owners = _topology(scene)
        entries = defaultdict(list)
        for entry in package["entries"]:
            entries[parents.get(entry["asset_id"], entry["asset_id"])].append(entry)
        _require(bool(domain["served_racks"]) and all(rack in entries for rack in domain["served_racks"]), "incomplete_domain_rack_coverage")
        _require(all(len(owners[rack]) == 1 for rack in domain["served_racks"]), "ambiguous_rack_domain")
        first_samples = package["entries"][0]["samples"]
        grid = sorted({now, finish} | {p["at"] for p in first_samples if now < p["at"] < finish})
        key = "upper_power_w" if package["selection"] == "upper_bound" else "power_w"
        totals = {t: {"at": t, "electric_w": 0, "liquid_w": 0} for t in grid}
        racks, internal = [], []
        for rack in domain["served_racks"]:
            series, internal_samples = [], []
            for at in grid:
                power = sum(_interpolate(e["samples"], at, key) for e in entries[rack])
                liquid = sum(_interpolate(e["samples"], at, key) * e["liquid_fraction"] for e in entries[rack])
                series.append({"at": at, "electric_w": power, "liquid_w": liquid})
                totals[at]["electric_w"] += power
                totals[at]["liquid_w"] += liquid
                internal_samples.append({"at": at, "power_w": liquid})
            racks.append({"rack_id": rack, "source_assets": [e["asset_id"] for e in entries[rack]],
                          "series": series, "peak_liquid_w": max(p["liquid_w"] for p in series)})
            # 内部桥接是液侧等效功率，fraction=1，避免不同节点捕热比例被再次平均。
            # 对外原始 electrical 数据与实际液侧总量在 result 中分别保留。
            internal.append({"rack_id": rack, "liquid_fraction": 1.0, "samples": internal_samples})
        series = list(totals.values())
        peak = max(point["liquid_w"] for point in series)
        _require(peak <= policy["max_liquid_load_w"], "forecast_above_policy_load_envelope")
        result.update(status="ready", reason="validated_domain_feedforward", materialized_at=now,
                      peak_liquid_w=peak, series=series, racks=racks, horizon_s=policy["horizon_s"])
        internal_payload = {"issued_at": now, "materialized_at": now, "source_issued_at": package["issued_at"],
                            "forecast_id": package["forecast_id"], "valid_until": package["valid_until"],
                            "unit": "W_e", "racks": internal, "representation": "liquid_equivalent_internal_bridge"}
        return internal_payload, result
    except ForecastError as exc:
        result.update(status="rejected", reason=exc.code)
        return None, result


def preview(scene, package, now, clock_basis):
    """机房汇总只合并可用、互斥域的同时刻值，绝不把各域峰值直接相加。"""
    if scene.get("schema_version") == "0.2":
        return {"status": "unsupported", "reason": "analysis_schema_forecast_unsupported",
                "domains": [resolve_domain(scene, d["id"], None, None, None)[1]
                            for d in scene.get("control_domains", [])],
                "site": {"status": "unsupported", "included_domains": [],
                         "excluded_domains": [d["id"] for d in scene.get("control_domains", [])],
                         "series": [], "peak_liquid_w": None, "scope": "analysis_only_no_forecast_allocation"}}
    domains = [resolve_domain(scene, d["id"], package, now, clock_basis)[1] for d in scene["control_domains"]]
    ready = [d for d in domains if d["status"] == "ready"]
    site = {"status": "complete" if len(ready) == len(domains) and ready else "partial" if ready else "unavailable",
            "included_domains": [d["domain_id"] for d in ready], "excluded_domains": [d["domain_id"] for d in domains if d["status"] != "ready"],
            "series": [], "peak_liquid_w": None, "scope": "available_independent_domains_only_no_branch_allocation"}
    if ready:
        end = min(d["series"][-1]["at"] for d in ready)
        grid = sorted({now, end} | {p["at"] for d in ready for p in d["series"] if now < p["at"] < end})
        for at in grid:
            site["series"].append({"at": at,
                "liquid_w": sum(_interpolate(d["series"], at, "liquid_w") for d in ready),
                "electric_w": sum(_interpolate(d["series"], at, "electric_w") for d in ready)})
        site["peak_liquid_w"] = max(p["liquid_w"] for p in site["series"])
    return {"domains": domains, "site": site}
