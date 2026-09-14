"""Sustain-LC 专用 FMU 实验适配器，不是现场设备驱动。

一个 Master 独占整个五组回路 FMU，只允许算法写第 1 组的压差输入；
其余组的目标、阀门及一次侧策略保持固定，所有状态共享同一个通信时钟。
标准量仍使用 K、kg/s、kPa、W。原 FMU 的无单位 dp 输入实际上按 psi 比较，
加载时必须检查内部 Pa→psi 转换系数及目标别名，不能沿用历史回放中的 kPa 假设。
PyFMI 仅在实例化时导入，普通设备运行和单元测试不依赖该库。
"""
import hashlib
import math
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from .configuration import finite
from .contracts import Reading, Snapshot, describe_profile
from .runtime import ControlService

PA_TO_PSI = 0.00014503773800721815
KPA_PER_PSI = 1.0 / (1000 * PA_TO_PSI)
DP_QUANTITY = "cdu.dp_sp"


def block_prefix(group):
    return "simulator[1].datacenter[1].computeBlock[%s]" % group


def input_prefix(group):
    return "simulator_1_datacenter_1_computeBlock_%s" % group


def inspect_contract(path):
    """核对本参考模型的输入因果性、单位变换和读点，返回可追溯的接口证据。

    内部压力为 Pa；gain 将供回压差转为 psi，与 dp_nom_RL 输入比较。
    这项检查针对指定结构，其他 FMU 必须另做适配，不能仅靠文件名判断兼容。
    """
    with ZipFile(path) as archive:
        root = ET.fromstring(archive.read("modelDescription.xml"))
    variables = {v.attrib["name"]: v for v in root.findall("./ModelVariables/ScalarVariable")}
    types = {t.get("name"): list(t)[0].get("unit") for t in root.findall("./TypeDefinitions/SimpleType")}
    cs = root.find("CoSimulation")
    if root.get("fmiVersion") != "2.0" or cs is None:
        raise ValueError("requires_fmi2_cosimulation")

    def require(name, unit=None, input_only=False):
        v = variables[name]
        real = v.find("Real")
        if real is None or (input_only and v.get("causality") != "input"):
            raise ValueError("invalid_fmu_point:" + name)
        actual = real.get("unit") or types.get(real.get("declaredType"))
        if unit and actual != unit:
            raise ValueError("fmu_unit_mismatch:" + name)
        return v, real

    for group in range(1, 6):
        p = block_prefix(group) + ".cdu[1]"
        inp = input_prefix(group)
        require(inp + "_cdu_1_sources_dp_nom_RL", input_only=True)
        v, _ = require(p + ".sources.dp_nom_RL")
        comparator, _ = require(p + ".controls.PID_CDUP.u_s")
        if comparator.get("valueReference") != v.get("valueReference"):
            raise ValueError("dp_input_alias_mismatch")
        _, gain = require(p + ".controls.gain.k")
        if not math.isclose(float(gain.get("start")), PA_TO_PSI, rel_tol=1e-10):
            raise ValueError("unverified_dp_conversion")
        for key, unit in (("T_sec_s", "K"), ("T_sec_r", "K"), ("p_sec_s", "Pa"),
                          ("p_sec_r", "Pa"), ("m_flow_sec", "kg/s"), ("W_flow_CDUP", "W")):
            require(p + ".summary." + key, unit)
        require(inp + "_cdu_1_sources_Tsec_supply_nom_RL", input_only=True)
        for branch in range(1, 4):
            require(inp + "_cabinet_1_sources_Valve_Stpts[%s]" % branch, input_only=True)
            require(inp + "_cabinet_1_sources_ComputePowerBlade%s" % branch, input_only=True)
            require(block_prefix(group) + ".cabinet[1].coolingChannels_%s.medium.T" % branch, "K")
    return {
        "fmu_sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "model_name": root.get("modelName"), "co_simulation": cs.attrib,
        "dp_input_unit": "psi (inferred and checked from internal comparator conversion)",
        "kpa_per_input_unit": KPA_PER_PSI,
        "dp_observation": "(summary.p_sec_s - summary.p_sec_r) / 1000",
        "temperature_unit": "K", "flow_unit": "kg/s", "power_unit": "W",
    }


class SustainFmuMaster:
    """独占 FMU、推进时间并施加实验扰动；禁止每个 CDU 自己调用 do_step。"""
    def __init__(self, path, experiment, loader=None):
        self.contract = inspect_contract(path)
        self.config = experiment
        self.time = 0.0
        self.active = True
        self.failed = False
        self.released = False
        self.adapter_attached = False
        self.statuses = []
        self.writes = []
        self.last_record = None
        if loader is None:
            from pyfmi import load_fmu
            loader = load_fmu
        self.model = loader(str(path), kind="CS", log_level=0)
        try:
            self.model.setup_experiment(start_time=0, stop_time=experiment["warmup_s"] + experiment["evaluation_s"])
            for group in range(1, 6):
                p = input_prefix(group)
                self.model.set(p + "_cdu_1_sources_Tsec_supply_nom_RL", experiment["supply_setpoint_degC"])
                self.model.set(p + "_cdu_1_sources_dp_nom_RL", experiment["baseline_dp_psi"])
                for branch, opening in enumerate(experiment["branch_openings"], 1):
                    self.model.set(p + "_cabinet_1_sources_Valve_Stpts[%s]" % branch, opening)
                    self.model.set(p + "_cabinet_1_sources_ComputePowerBlade%s" % branch, experiment["background_blade_input_w"])
            p = "simulator_1_centralEnergyPlant_1_coolingTowerLoop_1_sources_"
            self.model.set(p + "Towb", experiment["wet_bulb_k"])
            self.model.set(p + "CT_RL_stpt", experiment["tower_setpoint_k"])
            self.model.initialize()
        except Exception:
            self.close()
            raise

    def value(self, name):
        value = float(self.model.get(name)[0])
        if not finite(value):
            raise ValueError("nonfinite_fmu_output:" + name)
        return value

    def advance(self, dt, blade_input_w):
        """在当前时刻施加第 1 组负荷，再推进一步；非 OK 状态停止，不伪造成功样本。"""
        if not self.active or self.failed or not finite(dt) or dt <= 0 or not finite(blade_input_w) or blade_input_w < 0:
            raise ValueError("invalid_fmu_step")
        if self.time + dt > self.config["warmup_s"] + self.config["evaluation_s"] + 1e-8:
            raise ValueError("fmu_stop_time_exceeded")
        for branch in range(1, 4):
            self.model.set(input_prefix(1) + "_cabinet_1_sources_ComputePowerBlade%s" % branch, blade_input_w)
        try:
            status = int(self.model.do_step(current_t=self.time, step_size=dt))
        except Exception:
            self.failed = True
            raise
        self.statuses.append(status)
        if status != 0:
            self.failed = True
            raise RuntimeError("fmi_non_ok_status:%s at %s" % (status, self.time))
        self.time += dt
        if not math.isclose(float(self.model.time), self.time, abs_tol=1e-7):
            self.failed = True
            raise RuntimeError("fmu_clock_mismatch")
        try:
            self.last_record = self.measure()
        except Exception:
            self.failed = True
            raise
        self.last_record["blade_input_w"] = blade_input_w
        return dict(self.last_record)

    def measure(self):
        """读取五组温压流及明确列出的电功率边界；不把绝对压力或设定值冒充实际压差。"""
        record = {"time_s": self.time}
        pump_sum = 0.0
        for group in range(1, 6):
            p = block_prefix(group) + ".cdu[1].summary."
            supply, returned = self.value(p + "T_sec_s"), self.value(p + "T_sec_r")
            ps, pr = self.value(p + "p_sec_s"), self.value(p + "p_sec_r")
            pump = self.value(p + "W_flow_CDUP")
            external_target = self.value(input_prefix(group) + "_cdu_1_sources_dp_nom_RL")
            internal_target = self.value(block_prefix(group) + ".cdu[1].controls.PID_CDUP.u_s")
            if not math.isclose(external_target, internal_target, abs_tol=1e-8):
                raise RuntimeError("external_dp_target_not_reaching_comparator")
            record.update({"g%s_supply_degC" % group: supply - 273.15,
                           "g%s_return_degC" % group: returned - 273.15,
                           "g%s_flow_kg_s" % group: self.value(p + "m_flow_sec"),
                           "g%s_dp_kpa" % group: (ps - pr) / 1000,
                           "g%s_pump_w" % group: pump,
                           "g%s_dp_input_psi" % group: self.value(input_prefix(group) + "_cdu_1_sources_dp_nom_RL"),
                           "g%s_min_absolute_pressure_pa" % group: min(ps, pr)})
            record["g%s_channel_max_degC" % group] = max(
                self.value(block_prefix(group) + ".cabinet[1].coolingChannels_%s.medium.T" % branch)
                for branch in range(1, 4)) - 273.15
            pump_sum += pump
        p = "simulator[1].centralEnergyPlant[1]."
        record["five_cdu_pumps_w"] = pump_sum
        record["primary_pump_w"] = self.value(p + "hotWaterLoop[1].summary.W_flow_HTWP")
        record["tower_pump_w"] = self.value(p + "coolingTowerLoop[1].summary.W_flow_CTWP")
        record["tower_fans_w"] = self.value(p + "coolingTowerLoop[1].summary.W_flow_CT")
        record["represented_cooling_w"] = sum(record[k] for k in (
            "five_cdu_pumps_w", "primary_pump_w", "tower_pump_w", "tower_fans_w"))
        return record

    def write_dp(self, kpa):
        """只接受实验授权的第 1 组压差；所有现场写入仍走原 Modbus 驱动。"""
        if not self.active or self.failed or self.released or not finite(kpa):
            raise ValueError("fmu_not_owned")
        raw = kpa / KPA_PER_PSI
        if not 25 <= raw <= 38:
            raise ValueError("outside_reference_action_envelope")
        self.model.set(input_prefix(1) + "_cdu_1_sources_dp_nom_RL", raw)
        self.writes.append({"time_s": self.time, "quantity": DP_QUANTITY, "value_kpa": kpa, "fmu_input_psi": raw})

    def close(self):
        """释放模型资源；一个进程只实例化一次此 FMU，不用 reset 代替独立对照实验。"""
        if not self.active:
            return
        self.active = False
        try:
            self.model.terminate()
        finally:
            self.model.free_instance()


class SustainFmuAdapter:
    """把 Master 的第 1 组公开为标准 CDU 接口；owner/mode 属于实验调度器，不冒充 OEM 信号。"""
    def __init__(self, master, scene):
        if master.adapter_attached:
            raise ValueError("one_active_adapter_per_fmu_master")
        if len(scene["control_domains"]) != 1:
            raise ValueError("laboratory_requires_one_active_domain")
        self.master = master
        self.domain = scene["control_domains"][0]
        self.asset_id = self.domain["cdu_id"]
        self.profile = scene["devices"][self.asset_id]
        if (self.profile["adapter"] != "sustain_fmu" or not self.domain["shared_hydraulics"]
                or set(self.profile["controls"]) != {DP_QUANTITY}):
            raise ValueError("invalid_laboratory_scope")
        master.adapter_attached = True
        self.owner = self.domain["owner"]

    def describe(self):
        return describe_profile(self.asset_id, self.profile, "simulation")

    def owns_experiment(self, domain):
        return (domain == self.domain and self.master.active and not self.master.failed and not self.master.released
                and self.owner == domain["owner"])

    def read(self, now):
        """读取真实 FMU 输出；时间戳来自 FMU 时钟，绝不通过刷新采集时间掩盖未推进。"""
        if not self.master.active or self.master.failed or not math.isclose(now, self.master.time, abs_tol=1e-8):
            raise ValueError("read_must_match_fmu_time")
        p = block_prefix(1) + ".cdu[1].summary."
        timestamp = self.master.time
        observation = {
            "cdu.sec_supply_temp": (self.master.value(p + "T_sec_s"), "K"),
            "cdu.sec_return_temp": (self.master.value(p + "T_sec_r"), "K"),
            "cdu.sec_flow": (self.master.value(p + "m_flow_sec"), "kg/s"),
            "cdu.sec_dp": ((self.master.value(p + "p_sec_s") - self.master.value(p + "p_sec_r")) / 1000, "kPa"),
            "cdu.electric_power": (self.master.value(p + "W_flow_CDUP"), "W_e"),
        }
        # 不把外部 ComputePowerBlade 输入总和直接当作 CDU 实时热负荷。
        # 现阶段保留 FlowPolicy 的温差/流量反算路径，避免模型内部热源与储热误差。
        observations = {k: Reading(v, u, timestamp, provenance="sustain_fmu") for k, (v, u) in observation.items()}
        raw = self.master.value(input_prefix(1) + "_cdu_1_sources_dp_nom_RL")
        return Snapshot(self.asset_id, timestamp,
                        "experiment_dp" if self.owner else "experiment_fixed_hold", self.owner, (),
                        observations, {DP_QUANTITY: Reading(raw * KPA_PER_PSI, "kPa", timestamp,
                                                           provenance="fmu_input_readback_not_process_response")})

    def write(self, request):
        if request.asset_id != self.asset_id or request.quantity != DP_QUANTITY or request.unit != "kPa":
            raise ValueError("unsupported_fmu_request")
        self.master.write_dp(request.value)

    def release_to_local(self, owner):
        """实验释放：回到固定基线目标，读取输入确认。并非厂家本地自动或失联看门狗。"""
        if owner != self.owner or not self.master.active or self.master.failed:
            return False
        target = self.master.config["baseline_dp_psi"]
        tag = input_prefix(1) + "_cdu_1_sources_dp_nom_RL"
        self.master.model.set(tag, target)
        confirmed = math.isclose(self.master.value(tag), target, abs_tol=1e-8)
        if confirmed:
            self.owner = None
            self.master.released = True
        return confirmed


class FmuLaboratoryGate(ControlService):
    """专用仿真主控：允许固定邻机边界下单组实验，普通硬件共享控制限制保持不变。"""
    def _shared_control_permitted(self):
        return (isinstance(self.adapter, SustainFmuAdapter)
                and self.adapter.describe().deployment == "simulation"
                and self.adapter.owns_experiment(self.domain))
