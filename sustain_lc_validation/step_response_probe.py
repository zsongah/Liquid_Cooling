"""Thermal time-constant probe for the Sustain-LC Frontier FMU.

Purpose: quantify the thermal response time of the single-phase cold-plate loop,
specifically the coolant/thermal-mass time constant that decides whether a
supervisory (5-30 s) control layer is physically sufficient.

Design:
  Phase 1 (settle)   : t in [0, T_SETTLE)      - all inputs held, system relaxes
  Phase 2 (step)     : t >= T_SETTLE           - CDU group 1 secondary supply
                                                 setpoint steps 28 -> 30 C
  All other 4 CDU groups stay at 28 C, so group 1 is the step channel and the
  rest act as unchanged controls / cross-coupling checks.

This is a reference-model experiment. It characterises the FMU, not any real
AIDC. Results are only valid for this model, these loads and this configuration.

Run inside the existing linux/amd64 PyFMI image (see companion .md for command).
"""
import argparse
import csv
import json
import math
import os
from pathlib import Path
import time
import traceback

SOURCE = Path(os.environ.get("SUSTAIN_SOURCE", "/source"))
OUTPUT = Path(os.environ.get("VALIDATION_OUTPUT", "/validation"))
FMU = SOURCE / "LC_Frontier_5Cabinet_4_17_25.fmu"

STEP_S = 15.0
BLOCK = "simulator[1].datacenter[1].computeBlock[{}]"


def points(group):
    p = BLOCK.format(group)
    return {
        f"g{group}_sec_supply_C": p + ".cdu[1].summary.T_sec_s_C",
        f"g{group}_sec_return_C": p + ".cdu[1].summary.T_sec_r_C",
        f"g{group}_prim_supply_C": p + ".cdu[1].summary.T_prim_s_C",
        f"g{group}_prim_return_C": p + ".cdu[1].summary.T_prim_r_C",
        f"g{group}_sec_mass_flow_kg_s": p + ".cdu[1].summary.m_flow_sec",
        f"g{group}_pump_W": p + ".cdu[1].summary.W_flow_CDUP",
        f"g{group}_chan1_T_C": p + ".cabinet[1].coolingChannels_1.medium.T",
        f"g{group}_chan2_T_C": p + ".cabinet[1].coolingChannels_2.medium.T",
        f"g{group}_chan3_T_C": p + ".cabinet[1].coolingChannels_3.medium.T",
        f"g{group}_node1_K": p + ".cabinet[1].boundary_1.port.T",
        f"g{group}_branch1_kg_s": p + ".cabinet[1].valveLinear_1.m_flow",
        f"g{group}_branch2_kg_s": p + ".cabinet[1].valveLinear_2.m_flow",
        f"g{group}_branch3_kg_s": p + ".cabinet[1].valveLinear_3.m_flow",
    }


TAGS = {}
for g in range(1, 6):
    TAGS.update(points(g))

SETPOINT_BASE_C = 28.0
SETPOINT_STEP_C = 30.0
HEAT_W = 60000.0
VALVES = (0.33, 0.33, 0.34)
WET_BULB_K = 293.15


def run(settle_s, step_s, mode):
    import pyfmi

    model = None
    rows = []
    statuses = []
    started = time.monotonic()
    total_s = settle_s + step_s
    n = int(round(total_s / STEP_S))
    do_step_applied = mode == "step"
    suffix = "" if do_step_applied else "_hold"
    try:
        model = pyfmi.load_fmu(str(FMU), kind="CS", log_level=0)
        model.setup_experiment(start_time=0, stop_time=total_s)
        names = model.get_model_variables()
        available = {k: v for k, v in TAGS.items() if v in names}
        missing = {k: v for k, v in TAGS.items() if v not in names}
        print(f"available={len(available)} missing={len(missing)}", flush=True)
        if missing:
            print("MISSING:", json.dumps(missing, indent=1)[:1200], flush=True)

        for i in range(1, 6):
            base = f"simulator_1_datacenter_1_computeBlock_{i}"
            model.set(base + "_cdu_1_sources_Tsec_supply_nom_RL", SETPOINT_BASE_C)
            model.set(base + "_cdu_1_sources_dp_nom_RL", 27.5)
            for j, opening in enumerate(VALVES, 1):
                model.set(base + f"_cabinet_1_sources_Valve_Stpts[{j}]", opening)
                model.set(base + f"_cabinet_1_sources_ComputePowerBlade{j}", HEAT_W)
        model.set(
            "simulator_1_centralEnergyPlant_1_coolingTowerLoop_1_sources_Towb",
            WET_BULB_K,
        )
        model.set(
            "simulator_1_centralEnergyPlant_1_coolingTowerLoop_1_sources_CT_RL_stpt",
            WET_BULB_K + 50.0 / 9.0,
        )
        model.initialize()

        step_tag = (
            "simulator_1_datacenter_1_computeBlock_1_cdu_1_sources_Tsec_supply_nom_RL"
        )
        for k in range(n):
            t0 = k * STEP_S
            stepped = do_step_applied and t0 >= settle_s
            target = SETPOINT_STEP_C if stepped else SETPOINT_BASE_C
            model.set(step_tag, target)
            status = int(model.do_step(current_t=t0, step_size=STEP_S))
            statuses.append(status)
            if status != 0:
                raise RuntimeError(f"Non-OK FMI status={status} at t={t0}")
            row = {
                "time_s": t0 + STEP_S,
                "phase": "step" if stepped else "settle",
                "g1_supply_setpoint_C": target,
            }
            row.update({k: float(model.get(v)[0]) for k, v in available.items()})
            numeric = [v for k, v in row.items() if k != "phase"]
            if not all(math.isfinite(x) for x in numeric):
                raise RuntimeError(f"Nonfinite output at t={t0}")
            rows.append(row)
            if k % 60 == 0:
                print(
                    f"  t={row['time_s']:.0f}s g1_sec_s={row['g1_sec_supply_C']:.3f} "
                    f"g1_sec_r={row['g1_sec_return_C']:.3f} "
                    f"g1_chan1={row['g1_chan1_T_C']:.3f}",
                    flush=True,
                )

        trace = OUTPUT / f"time_constant_trace{suffix}.csv"
        with trace.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(rows[0]))
            w.writeheader()
            w.writerows(rows)

        meta = {
            "purpose": "Thermal time-constant characterisation of the reference FMU",
            "status": "passed",
            "mode": mode,
            "step_applied": do_step_applied,
            "settle_s": settle_s,
            "step_s": step_s,
            "step_time_s": settle_s,
            "step_channel": (
                "computeBlock[1] CDU secondary supply setpoint"
                if do_step_applied else None
            ),
            "step_from_C": SETPOINT_BASE_C,
            "step_to_C": SETPOINT_STEP_C if do_step_applied else SETPOINT_BASE_C,
            "step_size_C": (SETPOINT_STEP_C - SETPOINT_BASE_C) if do_step_applied else 0.0,
            "sample_period_s": STEP_S,
            "samples": len(rows),
            "simulated_seconds": len(rows) * STEP_S,
            "elapsed_wall_s": round(time.monotonic() - started, 2),
            "fmi_statuses": sorted(set(statuses)),
            "point_map": available,
            "missing_points": missing,
            "conditions": (
                f"Synthetic and illustrative: 15 heat inputs at {HEAT_W/1000:.0f} kW each; "
                f"wet bulb {(WET_BULB_K-273.15):.0f} C; tower target wet bulb + 50/9 K; "
                f"all 5 CDU groups at {SETPOINT_BASE_C} C; differential-pressure input 27.5; "
                f"valve commands {VALVES}; "
                + (
                    f"group 1 supply setpoint steps to {SETPOINT_STEP_C} C at "
                    f"t={settle_s} s."
                    if do_step_applied
                    else "all group supply setpoints held constant (no step)."
                )
            ),
            "limits": (
                "Single fixed topology and load level. Characterises this reference "
                "model only. Not a real AIDC, not a validated thermal-safety or "
                "energy-saving result, and not a substitute for site measurement."
            ),
        }
        (OUTPUT / f"time_constant_meta{suffix}.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        print(f"wrote {trace} ({len(rows)} rows)", flush=True)
    finally:
        if model is not None:
            try:
                model.terminate()
            finally:
                model.free_instance()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--settle-s", type=float, default=3600.0)
    ap.add_argument("--step-s", type=float, default=3600.0)
    ap.add_argument("--mode", choices=["step", "hold"], default="step",
                    help="step: apply the setpoint step. hold: hold all setpoints "
                         "constant to characterise the baseline oscillation.")
    a = ap.parse_args()
    try:
        run(a.settle_s, a.step_s, a.mode)
    except Exception:
        traceback.print_exc()
        raise SystemExit(1)
