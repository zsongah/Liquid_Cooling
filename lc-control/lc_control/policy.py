"""基础主动热管理算法，以及一个可解释的聚合水力参数校准器。

热需求由独立液冷热负荷或当前流量/温差估算；可叠加有效的外部负荷预测。
按 Q/(cp·ΔT) 求流量需求，叠加正向温差反馈，再转换为流量或压差设定。
供温目标在本版保持。回液预测是一阶近似，不是完整数字孪生或芯片热模型。
学习只更新 flow≈gain·sqrt(dp) 的 gain，不能学习/放宽工程安全边界。"""
import math
import statistics
import uuid
from dataclasses import asdict

from .configuration import finite
from .contracts import ControlRequest


def clamp(value, lower, upper):
    """把候选值限制在闭区间内；最终设备安全检查仍在网关重复执行。"""
    return min(upper, max(lower, value))


class FlowPolicy:
    """与厂商协议无关的热需求控制器，同时维护一个聚合水力增益。"""
    def __init__(self, config):
        self.config = config
        feedback_gain = config.get("feedback_flow_per_k", 0.08)
        if not finite(feedback_gain) or feedback_gain < 0:
            raise ValueError("invalid_feedback_flow_per_k")
        for key in ("cp_j_kg_k", "target_delta_k", "minimum_flow_kg_s", "maximum_flow_kg_s",
                    "return_soft_limit_k", "flow_per_sqrt_kpa", "thermal_tau_s", "horizon_s",
                    "command_ttl_s", "max_data_age_s", "deadband", "max_liquid_load_w"):
            if not finite(config.get(key)) or config[key] <= 0:
                raise ValueError("invalid_policy_parameter:" + key)
        if config["minimum_flow_kg_s"] >= config["maximum_flow_kg_s"]:
            raise ValueError("invalid_policy_flow_bounds")
        bounds = config["gain_bounds"]
        if not (len(bounds) == 2 and all(finite(x) for x in bounds)
                and 0 < bounds[0] < config["flow_per_sqrt_kpa"] < bounds[1]):
            raise ValueError("invalid_gain_bounds")
        if config["control_quantity"] not in ("cdu.dp_sp", "cdu.sec_flow_sp"):
            raise ValueError("policy_supports_only_dp_or_flow")
        self.gain = config["flow_per_sqrt_kpa"]
        self.version = 0
        self.samples = []
        self.previous = None
        self.last_candidate_count = 0
        self.last_decision = {}

    def value(self, snapshot, name, unit, now):
        """获取一个满足单位、质量、新鲜度要求的观测；不自动把缺测补成正常值。"""
        p = snapshot.observations.get(name)
        if (p is None or p.unit != unit or p.quality != "good" or not finite(p.value)
                or not finite(p.timestamp) or not 0 <= now - p.timestamp <= self.config["max_data_age_s"]):
            raise ValueError("policy_observation_unavailable:" + name)
        return p.value

    def forecast_load(self, external, now, racks):
        """校验外部机柜电功率预测：完整机柜覆盖、相同时间网格、有效期、单位与液冷分担比例。
        聚合为 CDU 未来液冷热负荷峰值；无效整包拒绝。预测只能提高本周期冷却需求，不能降低当前需求。"""
        if not external:
            return None, "forecast_absent"
        try:
            if (not finite(external["issued_at"]) or not finite(external["valid_until"])
                    or not 0 <= now - external["issued_at"] <= self.config["max_data_age_s"]
                    or now >= external["valid_until"] or external["unit"] != "W_e"):
                raise ValueError()
            entries = external["racks"]
            if len(entries) != len(racks) or {r["rack_id"] for r in entries} != set(racks):
                raise ValueError()
            totals = {}
            grid = None
            for rack in entries:
                fraction = rack["liquid_fraction"]
                if not finite(fraction) or not 0 <= fraction <= 1:
                    raise ValueError()
                series = rack["samples"]
                times = [p["at"] for p in series]
                if (not times or any(not finite(t) or not now <= t <= min(
                        external["valid_until"], now + self.config["horizon_s"]) for t in times)
                        or times != sorted(set(times)) or (grid is not None and grid != times)):
                    raise ValueError()
                grid = times
                for p in series:
                    if not finite(p["power_w"]) or p["power_w"] < 0:
                        raise ValueError()
                    totals[p["at"]] = totals.get(p["at"], 0) + p["power_w"] * fraction
            peak = max(totals.values())
            if peak > self.config["max_liquid_load_w"]:
                raise ValueError()
            return peak, "forecast_used"
        except (KeyError, TypeError, ValueError):
            return None, "forecast_rejected"

    def observe(self, snapshot, now, controls, safe):
        """从准稳态、无报警、非饱和的数据估计 flow = gain × sqrt(dp)。
        最近合格样本中留最后 8 个作较晚时间验证，缺乏激励则冻结。
        候选最多改变 2%，验证均方误差至少下降 2% 才晋升；不是独立长期验证。"""
        c = self.config
        if not safe or snapshot.alarms:
            self.previous = None
            self.samples.clear()
            return {"status": "frozen", "reason": "unsafe_or_unqualified_data"}
        try:
            flow = self.value(snapshot, "cdu.sec_flow", "kg/s", now)
            dp = self.value(snapshot, "cdu.sec_dp", "kPa", now)
            if flow <= 0 or dp <= 0:
                raise ValueError()
        except ValueError:
            self.previous = None
            self.samples.clear()
            return {"status": "frozen", "reason": "missing_hydraulic_measurements"}
        previous = self.previous
        self.previous = (flow, dp)
        if previous is None or any(abs(a - b) / max(abs(b), 0.01) > 0.02
                                   for a, b in zip((flow, dp), previous)):
            return {"status": "collecting", "reason": "transient"}
        q = c["control_quantity"]
        cap = controls[q]
        sp = snapshot.setpoints[q].value
        if sp <= cap.minimum + c["deadband"] or sp >= cap.maximum - c["deadband"]:
            return {"status": "frozen", "reason": "actuator_at_limit"}
        self.samples.append((math.sqrt(dp), flow))
        self.samples = self.samples[-60:]
        if len(self.samples) < 20:
            return {"status": "collecting", "samples": len(self.samples)}
        self.last_candidate_count += 1
        if self.last_candidate_count % 10:
            return {"status": "collecting", "reason": "slow_calibration_cadence"}
        train, validation = self.samples[:-8], self.samples[-8:]
        if max(x for x, _ in train) - min(x for x, _ in train) < 0.15:
            return {"status": "frozen", "reason": "insufficient_excitation"}
        estimate = sum(x * y for x, y in train) / sum(x * x for x, y in train)
        if not c["gain_bounds"][0] <= estimate <= c["gain_bounds"][1]:
            return {"status": "frozen", "reason": "candidate_outside_physical_bounds"}
        candidate = clamp(estimate, self.gain * 0.98, self.gain * 1.02)
        def mse(gain):
            return statistics.mean((gain * x - y) ** 2 for x, y in validation)
        old_error, new_error = mse(self.gain), mse(candidate)
        event = {"status": "rejected", "old_gain": self.gain, "candidate_gain": candidate,
                 "validation_old_mse": old_error, "validation_new_mse": new_error,
                 "training_count": len(train), "validation_count": len(validation)}
        if new_error < old_error * 0.98:
            if c.get("adaptive_enabled", False):
                self.gain = candidate
                self.version += 1
                event["status"] = "promoted"
            else:
                event["status"] = "candidate_only"
        event["model_version"] = self.version
        return event

    def propose(self, snapshot, context):
        """以 Q/(cp·ΔT) 计算流量需求，叠加回液温差正反馈，按能力输出流量或压差候选。
        候选受上下限、最大单步变化和死区限制；返回 None 表示当前不需调整。
        同时记录一阶回液预测、容量不足告警和负荷来源；最终动作由网关决定。"""
        now, domain, controls = context["now"], context["domain"], context["controls"]
        c = self.config
        supply = self.value(snapshot, "cdu.sec_supply_temp", "K", now)
        returned = self.value(snapshot, "cdu.sec_return_temp", "K", now)
        flow = self.value(snapshot, "cdu.sec_flow", "kg/s", now)
        if flow <= 0 or returned < supply or snapshot.alarms:
            raise ValueError("invalid_thermal_operating_state")
        heat = c["cp_j_kg_k"] * flow * (returned - supply)
        provenance = "derived_from_cdu_temperatures_and_flow"
        if "cdu.liquid_load" in snapshot.observations:
            heat = self.value(snapshot, "cdu.liquid_load", "W_th", now)
            provenance = snapshot.observations["cdu.liquid_load"].provenance
        if not 0 <= heat <= c["max_liquid_load_w"]:
            raise ValueError("liquid_load_outside_configured_envelope")
        predicted, forecast_status = self.forecast_load(context.get("forecast"), now, domain["served_racks"])
        load = max(heat, predicted or 0)
        available_delta = min(c["target_delta_k"], c["return_soft_limit_k"] - supply)
        if available_delta <= 0:
            raise ValueError("supply_above_policy_return_limit")
        demand = load / (c["cp_j_kg_k"] * available_delta)
        # Positive thermal feedback; avoid an integral that winds up at actuator bounds.
        # kg/(s·K)，需按回路规模整定；默认值仅保持旧开发示例兼容。
        demand += max(0, returned - supply - available_delta) * c.get("feedback_flow_per_k", 0.08)
        target_flow = clamp(demand, c["minimum_flow_kg_s"], c["maximum_flow_kg_s"])
        q = c["control_quantity"]
        if q not in controls:
            raise ValueError("policy_control_not_advertised")
        cap, current = controls[q], snapshot.setpoints[q].value
        raw = (target_flow / self.gain) ** 2 if q == "cdu.dp_sp" else target_flow
        target = clamp(clamp(raw, cap.minimum, cap.maximum), current - cap.max_step, current + cap.max_step)
        predicted_return = supply + load / (c["cp_j_kg_k"] * max(flow, 0.01))
        predicted_return = returned + (predicted_return - returned) * (1 - math.exp(-c["horizon_s"] / c["thermal_tau_s"]))
        self.last_decision = {"load_w_th": load, "load_provenance": provenance,
                              "forecast_status": forecast_status, "demand_flow_kg_s": demand,
                              "target_flow_kg_s": target_flow, "model_version": self.version,
                              "gain": self.gain, "return_forecast_k": predicted_return,
                              "warnings": (["return_soft_limit_risk"] if predicted_return > c["return_soft_limit_k"] else [])
                              + (["flow_capacity_saturated"] if demand > c["maximum_flow_kg_s"] else [])
                              + (["control_capacity_saturated"] if raw > cap.maximum else [])}
        if abs(target - current) < c["deadband"]:
            return None
        return ControlRequest(str(uuid.uuid4()), domain["cdu_id"], context["topology_version"],
                              domain["owner"], q, target, cap.unit, now, now + c["command_ttl_s"])
