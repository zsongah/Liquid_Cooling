"""独立合成热工对象，只用于开发验证。

模拟泵响应、供温跟踪、回液热惯性和泵电功率；参数与控制器可故意失配。
热负荷由实验脚本注入，控制器只能通过公开 Reading 获取可见数据。
液对气示例未包含风机/室内空调，不能用这个对象证明全站节能或机柜安全。"""
import math
from .adapters import MockCDUAdapter


class ThermalPlant(MockCDUAdapter):
    """合成热工对象，继承协议替身，但通过 advance 真正计算可见温压流变化。"""
    def __init__(self, asset_id, profile, owner):
        super().__init__(asset_id, profile, owner)
        self.observations = {k: dict(v) for k, v in profile["initial_observations"].items()}
        self.plant = profile["simulation"]
        self.elapsed = 0

    def read(self, now):
        # Profile copy isolates dynamic plant readings from static scene metadata.
        """返回当前仿真测量与设定值，使用副本隔离动态观测与静态场景配置。"""
        self.profile = dict(self.profile, initial_observations=self.observations)
        return super().read(now)

    def advance(self, dt, load_w):
        """推进 dt 秒：泵惯性 → 供温惯性 → 回液热平衡 → 压差/泵电功率。
        电功率按 dp·体积流量/效率加空载项估算，不能作为厂家认证性能曲线。"""
        p = self.plant
        obs = self.observations
        q = self.profile["policy"]["control_quantity"]
        if self.owner is None:
            target_flow = p["local_fallback_flow_kg_s"]
        elif q == "cdu.dp_sp":
            target_flow = p["true_flow_per_sqrt_kpa"] * math.sqrt(self.setpoints[q])
        else:
            target_flow = self.setpoints[q]
        a = 1 - math.exp(-dt / p["pump_tau_s"])
        flow = obs["cdu.sec_flow"]["value"] + a * (target_flow - obs["cdu.sec_flow"]["value"])
        supply = obs["cdu.sec_supply_temp"]["value"]
        supply_target = self.setpoints["cdu.sec_supply_temp_sp"] + 273.15
        supply += (1 - math.exp(-dt / p["supply_tau_s"])) * (supply_target - supply)
        steady_return = supply + load_w / (p["cp_j_kg_k"] * flow)
        returned = obs["cdu.sec_return_temp"]["value"]
        returned += (1 - math.exp(-dt / p["return_tau_s"])) * (steady_return - returned)
        dp = (flow / p["true_flow_per_sqrt_kpa"]) ** 2
        power = p["pump_idle_w"] + dp * 1000 * flow / p["density_kg_m3"] / p["pump_efficiency"]
        for name, value, unit in (("cdu.sec_supply_temp", supply, "K"), ("cdu.sec_return_temp", returned, "K"),
                                  ("cdu.sec_flow", flow, "kg/s"), ("cdu.sec_dp", dp, "kPa"),
                                  ("cdu.liquid_load", load_w, "W_th"), ("cdu.electric_power", power, "W_e")):
            obs[name] = {"value": value, "unit": unit}
        self.elapsed += dt
