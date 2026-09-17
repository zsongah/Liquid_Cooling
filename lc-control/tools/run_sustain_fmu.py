"""一键运行 Sustain-LC 新闭环实验；保留原仓库和所有硬件控制限制。

宿主机只需标准 Python 与已有 Docker 镜像。每种策略单独启动进程，
满足参考 FMU 的 canBeInstantiatedOnlyOncePerProcess 限制。
容器无网络、代码/FMU 只读，只有新实验输出目录可写。
"""
import argparse
import copy
import csv
import hashlib
import json
import math
import subprocess
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lc_control.configuration import finite, load_scene
from lc_control.engine import Engine
from lc_control.operations import require_runtime_scene
from lc_control.report import render_report
from lc_control.sustain_fmu import SustainFmuMaster, SustainFmuAdapter, FmuLaboratoryGate, inspect_contract

VARIANTS = ("baseline", "fixed", "adaptive")
LABELS = {"baseline": "固定目标", "fixed": "固定参数算法", "adaptive": "在线校准算法"}
POWER_KEYS = ("g1_pump_w", "five_cdu_pumps_w", "primary_pump_w", "tower_pump_w", "tower_fans_w", "represented_cooling_w")


def save(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def validate_experiment(scene):
    """校验时间网格和有界实验输入；不把实验设置描述成现场安全参数。"""
    require_runtime_scene(scene)
    e = scene["extensions"]["sustain_fmu_experiment"]
    for key in ("warmup_s", "evaluation_s", "communication_step_s", "control_interval_s", "load_phase_s"):
        if not finite(e[key]) or e[key] <= 0:
            raise ValueError("invalid_experiment_time:" + key)
    dt = e["communication_step_s"]
    for key in ("warmup_s", "evaluation_s", "control_interval_s", "load_phase_s"):
        if not math.isclose(e[key] / dt, round(e[key] / dt), abs_tol=1e-9):
            raise ValueError("experiment_time_grid_mismatch:" + key)
    if not e["blade_input_profile_w"] or any(not finite(x) or x < 0 for x in e["blade_input_profile_w"]):
        raise ValueError("invalid_load_profile")
    if len(e["branch_openings"]) != 3 or any(not finite(v) or not 0 < v <= 1 for v in e["branch_openings"]):
        raise ValueError("invalid_branch_openings")
    for key in ("supply_setpoint_degC", "baseline_dp_psi", "wet_bulb_k", "tower_setpoint_k",
                "background_blade_input_w", "all_group_return_limit_degC", "all_channel_limit_degC"):
        if not finite(e[key]):
            raise ValueError("invalid_experiment_value:" + key)
    if not 25 <= e["baseline_dp_psi"] <= 38 or e["background_blade_input_w"] < 0:
        raise ValueError("invalid_experiment_boundary")
    p = next(iter(scene["devices"].values()))
    if e["control_interval_s"] < p["controls"]["cdu.dp_sp"]["minimum_interval_s"]:
        raise ValueError("control_interval_shorter_than_capability")
    return e


def check_envelope(record, config):
    """整个 FMU 的可观测实验包络，防止只看受控组而忽略邻机。
    冷却通道温度是液体状态，不能用来宣称芯片结温安全。
    """
    for group in range(1, 6):
        if record["g%s_return_degC" % group] > config["all_group_return_limit_degC"]:
            raise RuntimeError("neighbor_return_envelope_exceeded:%s" % group)
        if record["g%s_channel_max_degC" % group] > config["all_channel_limit_degC"]:
            raise RuntimeError("channel_envelope_exceeded:%s" % group)
        if record["g%s_flow_kg_s" % group] <= 0 or record["g%s_dp_kpa" % group] <= 0:
            raise RuntimeError("invalid_hydraulic_response:%s" % group)
    if any(record[key] < 0 for key in POWER_KEYS):
        raise RuntimeError("negative_power_boundary")


def run_worker(fmu, scene, output, variant):
    """先共同预热，再测量统一时域。动作根据当前测量产生，只作用于之后的 FMU 步。"""
    require_runtime_scene(scene)
    output.mkdir(parents=True, exist_ok=False)
    scene = copy.deepcopy(scene)
    config = validate_experiment(scene)
    scene["devices"]["CDU_01"]["policy"]["adaptive_enabled"] = variant == "adaptive"
    save(output / "scene.json", scene)
    started = time.monotonic()
    master = SustainFmuMaster(fmu, config)
    engine = None
    records = []
    energies = {k.replace("_w", "_kwh"): 0.0 for k in POWER_KEYS}
    summary = {"variant": variant, "complete": False, "contract": master.contract,
               "experiment": config, "error": None}
    try:
        dt = config["communication_step_s"]
        for _ in range(round(config["warmup_s"] / dt)):
            master.advance(dt, config["background_blade_input_w"])
        previous = dict(master.last_record)
        check_envelope(previous, config)
        adapter = SustainFmuAdapter(master, scene)
        engine = Engine(scene, "FMU_DOMAIN_01", adapter, output, "monitor" if variant == "baseline" else "control",
                        service_factory=FmuLaboratoryGate)
        # t=warmup 先决策，再施加到下一个通信步；三组都用相同初态且不继承旧模型。
        decision = engine.tick(master.time)
        if engine.service.mode == "paused" or decision.get("error"):
            raise RuntimeError("initial_control_failed:" + str(decision))
        rows_per_action = round(config["control_interval_s"] / dt)
        for k in range(round(config["evaluation_s"] / dt)):
            elapsed = k * dt
            index = int(elapsed // config["load_phase_s"]) % len(config["blade_input_profile_w"])
            record = master.advance(dt, config["blade_input_profile_w"][index])
            record["elapsed_s"] = elapsed + dt
            record["gain"] = engine.policy.gain
            record["model_version"] = engine.policy.version
            records.append(record)
            check_envelope(record, config)
            for key in POWER_KEYS:
                energies[key.replace("_w", "_kwh")] += (previous[key] + record[key]) / 2 * dt / 3600000
            previous = record
            if (k + 1) % rows_per_action == 0 and k + 1 < round(config["evaluation_s"] / dt):
                decision = engine.tick(master.time)
                if engine.service.mode == "paused" or decision.get("error"):
                    raise RuntimeError("control_stopped:" + str(decision))
        receipts = [e["receipt"] for e in engine.service.events if e["event"] == "receipt"]
        summary.update(complete=True, duration_s=config["evaluation_s"], samples=len(records), energy=energies,
                       g1_peak_return_degC=max(r["g1_return_degC"] for r in records),
                       all_groups_peak_return_degC=max(r["g%s_return_degC" % g] for r in records for g in range(1, 6)),
                       all_channels_peak_degC=max(r["g%s_channel_max_degC" % g] for r in records for g in range(1, 6)),
                       input_changes=len(master.writes),
                       confirmed=sum(r["status"] == "setpoint_confirmed" for r in receipts),
                       rejected=sum(r["status"] == "rejected" for r in receipts),
                       model_updates=engine.policy.version, final_gain=engine.policy.gain,
                       min_absolute_pressure_pa=min(r["g%s_min_absolute_pressure_pa" % g] for r in records for g in range(1, 6)),
                       status_before_close=engine.service.mode)
    except Exception as error:
        summary["error"] = str(error)
        raise
    finally:
        try:
            if engine:
                engine.close()
                render_report(output)
            summary["fixed_hold_after_close"] = master.released
        finally:
            summary["fmi_statuses"] = sorted(set(master.statuses))
            summary["last_fmu_time_s"] = master.time
            summary["wall_time_s"] = time.monotonic() - started
            save(output / "summary.json", summary)
            save(output / "fmu_writes.json", master.writes)
            if records:
                with (output / "trace.csv").open("w", newline="") as file:
                    writer = csv.DictWriter(file, fieldnames=list(records[0]))
                    writer.writeheader()
                    writer.writerows(records)
            master.close()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def docker_command(image, fmu, output, arguments, name):
    """只挂载指定 FMU、项目只读目录和当前结果目录；不使用仓库 compose 的读写源码挂载。"""
    return ["docker", "run", "--rm", "--pull=never", "--name", name, "--platform", "linux/amd64",
            "--network", "none", "--cpus", "2", "--memory", "2g",
            "--mount", "type=bind,src=%s,dst=/app,readonly" % ROOT,
            "--mount", "type=bind,src=%s,dst=/model/reference.fmu,readonly" % fmu,
            "--mount", "type=bind,src=%s,dst=/results" % output,
            "--workdir", "/tmp", image, "python", "/app/tools/run_sustain_fmu.py"] + arguments


def run_suite(args):
    fmu = Path(args.fmu).resolve()
    scene = load_scene(args.config)
    validate_experiment(scene)
    contract = inspect_contract(fmu)
    output = Path(args.output).resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError("suite_requires_fresh_output_directory")
    output.mkdir(parents=True, exist_ok=True)
    save(output / "scene.json", scene)
    save(output / "interface_contract.json", contract)
    manifest = {"contract": contract, "runs": {}, "code_sha256": {
        str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted((ROOT / "lc_control").glob("*.py"))},
        "scope": "Single active CDU with four fixed neighbors under one FMU master; not a multi-CDU optimizer."}
    for variant in VARIANTS:
        name = "lc-fmu-" + uuid.uuid4().hex[:12]
        common = ["--worker", variant, "--fmu", "/model/reference.fmu", "--config", "/results/scene.json",
                  "--output", "/results/" + variant]
        command = docker_command(args.image, fmu, output, common, name) if args.docker else [
            sys.executable, str(Path(__file__).resolve()), "--worker", variant,
            "--fmu", str(fmu), "--config", str(output / "scene.json"), "--output", str(output / variant)]
        print("Running independent FMU experiment: " + variant, flush=True)
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=180, cwd="/tmp")
            (output / (variant + ".log")).write_text(process.stdout + "\n" + process.stderr)
            manifest["runs"][variant] = {"exit_code": process.returncode}
            if process.returncode:
                print((process.stdout + process.stderr)[-2400:], flush=True)
        except (subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            if args.docker:
                subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            if isinstance(error, KeyboardInterrupt):
                raise
            manifest["runs"][variant] = {"error": "timeout_180s"}
        summary_path = output / variant / "summary.json"
        if summary_path.exists():
            manifest["runs"][variant]["result"] = json.loads(summary_path.read_text())
        save(output / "manifest.json", manifest)
    if not all(r.get("exit_code") == 0 and r.get("result", {}).get("complete") for r in manifest["runs"].values()):
        raise RuntimeError("FMU suite incomplete; inspect logs and partial results")
    # 报告与图表也在相同依赖环境生成，不要求 macOS 安装 PyFMI 或 matplotlib。
    name = "lc-fmu-report-" + uuid.uuid4().hex[:12]
    if args.docker:
        command = docker_command(args.image, fmu, output, ["--report-worker", "--output", "/results"], name)
        try:
            subprocess.run(command, check=True, timeout=60)
        except (subprocess.TimeoutExpired, KeyboardInterrupt):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=20)
            raise
    else:
        from sustain_fmu_report import build_report
        build_report(output)
    print(output / "index.html")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fmu")
    parser.add_argument("--config", default=str(ROOT / "examples/sustain_fmu_experiment.json"))
    parser.add_argument("--output", default=str(ROOT / "outputs/sustain-fmu-closed-loop"))
    parser.add_argument("--docker", action="store_true")
    parser.add_argument("--image", default="sustain-lc:amd64")
    parser.add_argument("--worker", choices=VARIANTS)
    parser.add_argument("--report-worker", action="store_true")
    args = parser.parse_args()
    if args.report_worker:
        from sustain_fmu_report import build_report
        build_report(Path(args.output))
    elif not args.fmu:
        parser.error("--fmu is required")
    elif args.worker:
        run_worker(Path(args.fmu), load_scene(args.config), Path(args.output), args.worker)
    else:
        run_suite(args)


if __name__ == "__main__":
    main()
