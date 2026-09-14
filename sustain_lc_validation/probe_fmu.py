"""Bounded local feasibility probes. Run in the existing linux/amd64 PyFMI image.

The source repository is mounted read-only at /source. This file and probe
outputs live in /validation; temporary FMU files/logs live inside the container.
No training, production control, or modifications to the source repository.
"""
import argparse
import csv
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time
import traceback

SOURCE = Path(os.environ.get("SUSTAIN_SOURCE", "/source"))
OUTPUT = Path(os.environ.get("VALIDATION_OUTPUT", "/validation"))
FMU = SOURCE / "LC_Frontier_5Cabinet_4_17_25.fmu"
STEP_S = 15.0
STEPS = 60
PREFIX = "simulator[1].datacenter[1].computeBlock[1]"
POINTS = {
    "cdu1_supply_K": PREFIX + ".cdu[1].summary.T_sec_s",
    "cdu1_return_K": PREFIX + ".cdu[1].summary.T_sec_r",
    "cdu1_mass_flow_kg_s": PREFIX + ".cdu[1].summary.m_flow_sec",
    "branch1_mass_flow_kg_s": PREFIX + ".cabinet[1].valveLinear_1.m_flow",
    "branch2_mass_flow_kg_s": PREFIX + ".cabinet[1].valveLinear_2.m_flow",
    "branch3_mass_flow_kg_s": PREFIX + ".cabinet[1].valveLinear_3.m_flow",
    "thermal_node1_K": PREFIX + ".cabinet[1].boundary_1.port.T",
    "cdu1_pump_summary_W": PREFIX + ".cdu[1].summary.W_flow_CDUP",
    "cdu1_pump_electric_W": PREFIX + ".cdu[1].CDUP.P",
    "tower_cell1_fan_W": "simulator[1].centralEnergyPlant[1].coolingTowerLoop[1].coolingTower[1].cell[1].CT.PFan",
}


def save(name, data):
    (OUTPUT / name).write_text(json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def cleanup(model):
    if model is not None:
        try:
            model.terminate()
        finally:
            model.free_instance()


def direct(mode):
    import pyfmi
    model = None
    rows = []
    statuses = []
    started = time.monotonic()
    try:
        model = pyfmi.load_fmu(str(FMU), kind="CS", log_level=0)
        model.setup_experiment(start_time=0, stop_time=STEPS * STEP_S)
        names = model.get_model_variables()
        available = {label: tag for label, tag in POINTS.items() if tag in names}
        missing = {label: tag for label, tag in POINTS.items() if tag not in names}
        # All conditions below are synthetic and independent of the CSV generators.
        for i in range(1, 6):
            base = f"simulator_1_datacenter_1_computeBlock_{i}"
            model.set(base + "_cdu_1_sources_Tsec_supply_nom_RL", 28.0)
            model.set(base + "_cdu_1_sources_dp_nom_RL", 27.5)
            for j, opening in enumerate((0.33, 0.33, 0.34), 1):
                model.set(base + f"_cabinet_1_sources_Valve_Stpts[{j}]", opening)
                model.set(base + f"_cabinet_1_sources_ComputePowerBlade{j}", 60000.0)
        model.set("simulator_1_centralEnergyPlant_1_coolingTowerLoop_1_sources_Towb", 293.15)
        model.set("simulator_1_centralEnergyPlant_1_coolingTowerLoop_1_sources_CT_RL_stpt", 293.15 + 50.0 / 9.0)
        model.initialize()
        for k in range(STEPS):
            t0 = k * STEP_S
            target = 30.0 if mode == "supply_step" and t0 >= 600 else 28.0
            model.set("simulator_1_datacenter_1_computeBlock_1_cdu_1_sources_Tsec_supply_nom_RL", target)
            status = int(model.do_step(current_t=t0, step_size=STEP_S))
            statuses.append(status)
            if status != 0:
                raise RuntimeError(f"Non-OK FMI status={status} at t={t0}")
            row = {"time_s": t0 + STEP_S, "cdu1_supply_setpoint_C": target}
            row.update({label: float(model.get(tag)[0]) for label, tag in available.items()})
            if not all(math.isfinite(x) for x in row.values()):
                raise RuntimeError(f"Nonfinite output at t={t0}")
            rows.append(row)
        with (OUTPUT / f"{mode}_trace.csv").open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        save(f"{mode}_result.json", {
            "mode": mode, "status": "passed", "steps": len(rows),
            "simulated_seconds": len(rows) * STEP_S, "elapsed_wall_s": time.monotonic() - started,
            "fmi_statuses": sorted(set(statuses)), "point_map": available, "missing_points": missing,
            "conditions": "Synthetic: 15 heat inputs at 60 kW; wet bulb 20 C; tower target wet bulb + 50/9 K; CDU target 28 C; differential-pressure input 27.5; direct valve commands 0.33/0.33/0.34. Optional CDU1 supply target 30 C from t=600 s.",
            "first": rows[0], "before_step": rows[39], "last": rows[-1],
            "limits": "Load and commands are illustrative. This is load/step/readability verification, not validated thermal safety or energy savings.",
        })
    finally:
        cleanup(model)


def wrapper():
    import numpy as np
    sys.path.insert(0, str(SOURCE))
    import frontier_env
    frontier_env.EXOGENOUS_VAR_PATH = str(SOURCE / "input_04-07-24.csv")
    env = None
    calls = []
    states = []
    try:
        env = frontier_env.SmallFrontierModel(stop_time=120, step_size=15, exogen_gen_v=1)
        env.reset()
        original = env.fmu

        class Proxy:
            def __getattr__(self, name):
                return getattr(original, name)

            def do_step(self, *args, **kwargs):
                entry = {"args": list(args), "kwargs": kwargs}
                calls.append(entry)
                result = original.do_step(*args, **kwargs)
                entry["status"] = int(result)
                entry["fmu_time_after"] = float(original.time)
                return result

        env.fmu = Proxy()
        action = {f"cdu-cabinet-{i}": np.array([-0.2, -0.6153846153846154, -0.34, -0.34, -0.32]) for i in range(1, 6)}
        action["cooling-tower-1"] = 4
        caught = None
        for _ in range(3):
            try:
                env.step(action)
                states.append({"env_current_time": float(env.current_time), "fmu_time": float(original.time)})
            except Exception as exc:
                caught = repr(exc)
                break
        wrong_clock = len(calls) > 1 and calls[1]["kwargs"].get("current_t") == calls[0]["kwargs"].get("current_t")
        save("wrapper_result.json", {"status": "issue_reproduced" if wrong_clock else "inspect", "calls": calls, "states": states, "exception": caught,
             "finding": "Check whether the wrapper repeats current_t while the FMU advances or reports failure; the wrapper does not validate do_step status."})
    finally:
        if env is not None:
            cleanup(env.fmu)


def suite():
    OUTPUT.mkdir(parents=True, exist_ok=True)
    outcome = {"fmu_sha256": hashlib.sha256(FMU.read_bytes()).hexdigest(), "runs": {}}
    for mode in ["baseline", "baseline_repeat", "supply_step", "wrapper"]:
        print(f"Running bounded probe: {mode}", flush=True)
        try:
            p = subprocess.run([sys.executable, __file__, "--mode", mode], capture_output=True, text=True, timeout=90, cwd="/tmp")
            (OUTPUT / f"{mode}.log").write_text(p.stdout + "\n" + p.stderr)
            outcome["runs"][mode] = {"exit_code": p.returncode}
            result = OUTPUT / ("wrapper_result.json" if mode == "wrapper" else f"{mode}_result.json")
            if p.returncode == 0 and result.exists():
                outcome["runs"][mode]["result"] = json.loads(result.read_text())
            if p.returncode:
                print((p.stdout + p.stderr)[-1600:], flush=True)
        except subprocess.TimeoutExpired:
            outcome["runs"][mode] = {"status": "timeout_90s"}
        save("runtime_probe_summary.json", outcome)
    traces = []
    for mode in ["baseline", "baseline_repeat"]:
        p = OUTPUT / f"{mode}_trace.csv"
        if outcome["runs"][mode].get("exit_code") == 0 and p.exists():
            with p.open() as f:
                traces.append(list(csv.DictReader(f)))
    if len(traces) == 2 and len(traces[0]) == len(traces[1]):
        outcome["baseline_repeat_max_abs_difference"] = max(abs(float(a[key]) - float(b[key])) for a,b in zip(*traces) for key in a)
    save("runtime_probe_summary.json", outcome)
    print(json.dumps({"runs": {k: v.get("result", {}).get("status", v) for k,v in outcome["runs"].items()}, "repeat_max_abs_difference": outcome.get("baseline_repeat_max_abs_difference")}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["suite", "baseline", "baseline_repeat", "supply_step", "wrapper"], default="suite")
    mode = parser.parse_args().mode
    try:
        if mode == "suite":
            suite()
        elif mode == "wrapper":
            wrapper()
        else:
            direct(mode)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
