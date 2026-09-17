"""面向用户的运行流程：场景选择、仿真对比、设备连续运行。

run 默认只读，只有显式 control 才写；共享回路依然被网关阻断。
每个设备和输出目录加同机锁，finally 负责交还控制并生成报告。
仿真比较使用独立初始化的对象和相同负荷轨迹，避免沿用前一策略的状态。"""
import copy
import json
import time
from pathlib import Path
from .engine import EndpointLock, Engine
from .registry import create_adapter
from .report import render_report


def domain_of(scene, domain_id=None):
    """单 CDU 场景可自动选域；多个 CDU 必须明确指定域，不默默只操作第一台。"""
    if domain_id:
        return next(d for d in scene["control_domains"] if d["id"] == domain_id)
    if len(scene["control_domains"]) != 1:
        raise ValueError("explicit_domain_required_for_multi_cdu_scene")
    return scene["control_domains"][0]


def load_at(elapsed):
    """固定的开发实验热负荷轨迹，保证不同策略使用相同扰动；不是预测模型。"""
    return (75000, 95000, 85000, 110000, 65000, 90000)[int(elapsed // 300) % 6]


def simulate(scene, output, seconds=1800, interval=5, adaptive=True):
    """驱动独立合成对象和产品 Engine，记录能耗、温度、边界事件及模型更新。"""
    scene = copy.deepcopy(scene)
    domain = domain_of(scene)
    profile = scene["devices"][domain["cdu_id"]]
    if profile["adapter"] != "thermal_sim":
        raise ValueError("simulate_requires_thermal_sim_profile")
    profile["policy"]["adaptive_enabled"] = adaptive
    adapter = create_adapter(domain["cdu_id"], profile, domain["owner"])
    engine = Engine(scene, domain["id"], adapter, output, "control")
    energy, load_energy, peak_return, breaches, elapsed = 0, 0, 0, 0, 0
    try:
        while elapsed < seconds:
            step = min(interval, seconds - elapsed)
            load = load_at(elapsed)
            adapter.advance(step, load)
            elapsed += step
            engine.tick(elapsed)
            obs = adapter.observations
            energy += obs["cdu.electric_power"]["value"] * step / 3600000
            load_energy += load * step / 3600000
            peak_return = max(peak_return, obs["cdu.sec_return_temp"]["value"])
            breaches += int(obs["cdu.sec_return_temp"]["value"] > profile["guards"]["cdu.sec_return_temp"]["maximum"])
            if engine.service.mode == "paused":
                break
        result = {"seconds": elapsed, "pump_energy_kwh": energy, "liquid_heat_kwh_th": load_energy,
                  "peak_return_degC": peak_return - 273.15, "return_guard_exceedance_samples": breaches,
                  "model_updates": engine.policy.version, "final_gain": engine.policy.gain,
                  "mode_before_shutdown": engine.service.mode, "source": "synthetic_plant"}
    finally:
        engine.close()
    return result


def compare(scene, output, seconds=1800, interval=5):
    """在独立初始化且同负荷的对象上比较固定设定、固定参数算法和在线校准算法。
    要求新输出目录，防止旧数据/旧模型污染比较。"""
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    if (output / "runtime.sqlite").exists():
        raise ValueError("comparison_requires_fresh_output_directory")
    adaptive = simulate(scene, output, seconds, interval, True)
    fixed_model = simulate(scene, output / "fixed_model", seconds, interval, False)
    domain = domain_of(scene)
    profile = scene["devices"][domain["cdu_id"]]
    baseline = create_adapter(domain["cdu_id"], profile, domain["owner"])
    energy, peak, elapsed, breaches = 0, 0, 0, 0
    while elapsed < seconds:
        step = min(interval, seconds - elapsed)
        baseline.advance(step, load_at(elapsed))
        elapsed += step
        energy += baseline.observations["cdu.electric_power"]["value"] * step / 3600000
        peak = max(peak, baseline.observations["cdu.sec_return_temp"]["value"])
        breaches += int(baseline.observations["cdu.sec_return_temp"]["value"] > profile["guards"]["cdu.sec_return_temp"]["maximum"])
    result = {"boundary": "synthetic_CDU_pump_only", "duration_s": seconds,
              "fixed_setpoint": {"pump_energy_kwh": energy, "peak_return_degC": peak - 273.15,
                                 "return_guard_exceedance_samples": breaches},
              "fixed_model_policy": fixed_model, "adaptive_policy": adaptive,
              "limitations": ["not_a_site_savings_claim", "no_rack_or_chip_model",
                              "no_facility_or_room_cooling_energy", "gain_error_reduction_does_not_prove_faster_thermal_response"]}
    (output / "comparison.json").write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n')
    render_report(output)
    render_report(output / "fixed_model")
    return result


def run(scene, output, mode="monitor", steps=12, interval=5, domain_id=None, forecast_file=None):
    """在边缘机连续采集并按选定模式运行。0 步表示常驻，其余达到次数后退出。
    设备和输出目录都加锁，退出尝试交还控制；故障通过结果 exit_code=2 对外可见。"""
    domain = domain_of(scene, domain_id)
    profile = scene["devices"][domain["cdu_id"]]
    if profile["adapter"] != "modbus_tcp":
        raise ValueError("run_requires_modbus_tcp_profile_use_simulate_for_thermal_sim")
    connection = profile["connection"]
    identity = json.dumps([connection[k] for k in ("host", "port", "unit_id")])
    if mode == "control":
        timeout = profile.get("commissioning", {}).get("watchdog_timeout_s")
        if not isinstance(timeout, (int, float)) or not 0 < interval < timeout / 2:
            raise ValueError("control_interval_must_be_less_than_half_verified_device_watchdog_timeout")
    with EndpointLock(identity), EndpointLock(str(Path(output).resolve())):
        adapter = create_adapter(domain["cdu_id"], profile, domain["owner"])
        engine = Engine(scene, domain["id"], adapter, output, mode)
        count = 0
        fault_cycles = 0
        stopped_for_fault = False
        try:
            while steps == 0 or count < steps:
                start = time.monotonic()
                now = time.time()
                forecast = None
                if forecast_file:
                    try:
                        forecast = json.loads(Path(forecast_file).read_text())
                    except (OSError, ValueError):
                        engine.store.put(now, "forecast_input_error", {"session": engine.session, "reason": "file_unavailable_or_invalid_json"})
                decision = engine.tick(now, forecast)
                fault_cycles += int(bool(decision.get("error")))
                stopped_for_fault = engine.service.mode == "paused"
                print(json.dumps({"cycle": count + 1, "mode": engine.service.mode,
                                  "warnings": decision.get("warnings", []), "error": decision.get("error"),
                                  "receipt": decision.get("receipt")}, ensure_ascii=False), flush=True)
                count += 1
                if engine.service.mode == "paused" or (steps and count >= steps):
                    break
                time.sleep(max(0, interval - (time.monotonic() - start)))
        finally:
            engine.close()
            render_report(output)
    return {"cycles": count, "output": str(Path(output).resolve()), "mode": engine.service.mode,
            "fault_cycles": fault_cycles, "exit_code": 2 if stopped_for_fault or fault_cycles else 0,
            "termination_reason": "fault" if stopped_for_fault else "completed_requested_cycles"}
