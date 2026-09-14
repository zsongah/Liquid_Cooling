"""Consolidated thermal-response analysis for the Sustain-LC Frontier reference FMU.

Two experiments, both 2 h at 15 s sampling, identical loads and boundaries:

  hold : all five CDU secondary supply setpoints held at 28 C (no step)
  step : same, but group 1 setpoint steps 28 -> 30 C at t = 3600 s

Reads  : time_constant_trace.csv (step), time_constant_trace_hold.csv (hold),
         time_constant_meta*.json
Writes : thermal_response_analysis.json, thermal_response_analysis.md

Why an exponential fit is used here at all, and on what:
  The step run does not settle to a constant: it enters a sustained oscillation
  about the new level. Fitting a single exponential to that raw trace measures
  the limit cycle, not the thermal lag. The fit below is therefore applied to
  the oscillation *envelope* of the rising portion (first-order rise toward the
  new mean level), and the oscillation is reported as a separate, first-class
  result. The hold run is the control that shows the oscillation is driven by
  the setpoint change and is absent when the setpoint is constant.

Standard library only.
"""
import csv
import json
import math
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
BLOCK_S = 150.0

# channels: key, label, role
CHANNELS = [
    ("g1_sec_return_C", "CDU二次回液温度", "被控回路主要热惯性输出"),
    ("g1_chan1_T_C", "机柜冷板通道1液温", "最靠近热源的热质量"),
    ("g1_node1_K", "机柜等效热节点温度", "等效热容节点"),
    ("g1_sec_supply_C", "CDU二次供液温度", "执行响应（设定值跟踪）"),
    ("g1_prim_return_C", "CDU一次回液温度", "一次侧耦合"),
]
COUPLED = [
    ("g2_sec_return_C", "第2组二次回液温度"),
    ("g3_sec_return_C", "第3组二次回液温度"),
    ("g4_sec_return_C", "第4组二次回液温度"),
    ("g5_sec_return_C", "第5组二次回液温度"),
]


def load(name):
    p = HERE / name
    if not p.exists():
        return None, None
    with p.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        for k in list(r):
            try:
                r[k] = float(r[k])
            except (TypeError, ValueError):
                pass
    meta_p = HERE / name.replace("_trace", "_meta").replace(".csv", ".json")
    meta = json.loads(meta_p.read_text()) if meta_p.exists() else {}
    return rows, meta


def series(rows, key, t_lo=0.0, t_hi=1e18):
    return [(r["time_s"], r[key]) for r in rows
            if t_lo <= r["time_s"] <= t_hi and isinstance(r.get(key), float)]


def limit_cycle(pts):
    if len(pts) < 20:
        return None
    t = [p[0] for p in pts]
    v = [p[1] for p in pts]
    m = statistics.mean(v)
    res = [x - m for x in v]
    zc = [i for i in range(1, len(res)) if res[i - 1] < 0 <= res[i]]
    if len(zc) < 3:
        return None
    step = t[1] - t[0]
    half = [(zc[i] - zc[i - 1]) * step for i in range(1, len(zc))]
    amp = sorted(abs(x) for x in res)[int(0.95 * (len(res) - 1))]
    return {
        "period_s": round(2 * statistics.median(half), 1),
        "half_periods_s": [round(h, 1) for h in half[:8]],
        "p95_amplitude_K": round(amp, 4),
        "peak_to_peak_K": round(max(v) - min(v), 4),
        "zero_crossings": len(zc),
        "mean_K": round(m, 4),
    }


def detrended_spread(pts):
    if len(pts) < 10:
        return None
    v = [p[1] for p in pts]
    n = len(v)
    xs = list(range(n))
    mx, my = statistics.mean(xs), statistics.mean(v)
    den = sum((x - mx) ** 2 for x in xs)
    b = sum((x - mx) * (y - my) for x, y in zip(xs, v)) / den if den else 0.0
    detr = [y - (my + b * (x - mx)) for x, y in zip(xs, v)]
    return {
        "stdev_K": round(statistics.pstdev(detr), 4),
        "p95_abs_dev_K": round(sorted(abs(x) for x in detr)[int(0.95 * (n - 1))], 4),
        "trend_per_hour_K": round(b * 3600.0, 4),
    }


def fit_envelope(pts, t_step, lo=0.05, hi=0.95):
    """First-order fit on the oscillation envelope after a step.

    Uses block means (150 s) to average out the limit cycle, then fits
    y = (x - x0)/(x_inf - x0) to 1 - exp(-(t-t_step)/tau) by robust inversion.
    Returns tau, the 10-90 percentile band, sample count, and the block fit.
    """
    post = [(t, v) for t, v in pts if t > t_step]
    if len(post) < 40:
        return None
    blocks = {}
    for t, v in post:
        blocks.setdefault(int((t - t_step) // BLOCK_S), []).append((t, v))
    bm = [(statistics.mean(t for t, _ in s), statistics.mean(v for _, v in s))
          for _, s in sorted(blocks.items())]
    # settle level from the last 20 % of blocks
    k = max(3, len(bm) // 5)
    x0 = statistics.mean(v for _, v in bm[:1])
    xinf = statistics.mean(v for _, v in bm[-k:])
    if abs(xinf - x0) < 1e-6:
        return None
    taus = []
    for t, v in bm:
        y = (v - x0) / (xinf - x0)
        dt = t - t_step
        if dt > 0 and lo < y < hi:
            taus.append(-dt / math.log(1.0 - y))
    if not taus:
        return None
    taus.sort()
    n = len(taus)
    med = taus[n // 2] if n % 2 else 0.5 * (taus[n // 2 - 1] + taus[n // 2])
    fracs = [(t - t_step, (v - x0) / (xinf - x0)) for t, v in bm]
    t63v = next((tt for tt, fr in fracs if fr >= 0.632), None)
    t90v = next((tt for tt, fr in fracs if fr >= 0.900), None)
    out = {
        "x_before_K": round(x0, 4),
        "x_after_K": round(xinf, 4),
        "delta_K": round(xinf - x0, 4),
        "tau_s": round(med, 1),
        "tau_band_s": [round(taus[int(0.10 * (n - 1))], 1),
                       round(taus[int(0.90 * (n - 1))], 1)],
        "n_blocks_used": len(taus),
        "block_s": BLOCK_S,
    }
    for f, nm in ((0.5, "t50"), (0.632, "t63"), (0.9, "t90")):
        hit = next((tt for tt, fr in fracs if fr >= f), None)
        out[f"{nm}_s"] = None if hit is None else round(hit, 1)
    # peak slope on the block trajectory
    sl = [((f2 - f1) / (t2 - t1), t1) for (t1, f1), (t2, f2)
          in zip(fracs, fracs[1:]) if t2 > t1]
    if sl:
        smax, tmax = max(sl)
        out["peak_slope_per_s"] = round(smax, 8)
        out["peak_slope_at_s"] = round(tmax, 1)
        out["tau_from_peak_slope_s"] = round((1 - 1 / math.e) / smax, 1) if smax > 0 else None
    # Shape check: a first-order response has t90/t63 ~ 2.3, so tau ~ t63.
    # If the two disagree, the loop is NOT first order and tau must not be
    # reported with false precision.
    est = [x for x in (out.get("tau_s"), out.get("tau_from_peak_slope_s"),
                       out.get("t63_s")) if x]
    if t63v and t90v and t63v > 0:
        ratio = t90v / t63v
        out["t90_over_t63"] = round(ratio, 3)
        out["first_order_consistent"] = 1.5 <= ratio <= 3.5
    if est:
        out["tau_band_overall_s"] = [round(min(est), 1), round(max(est), 1)]
        out["tau_consensus_s"] = round(
            (min(est) * max(est)) ** 0.5, 1)  # geometric mean, order-of-magnitude
    return out


def main():
    step_rows, step_meta = load("time_constant_trace.csv")
    hold_rows, hold_meta = load("time_constant_trace_hold.csv")
    res = {"experiments": {}, "channels": {}, "coupled_groups": [], "verdict": {}}

    t_step = step_meta["step_time_s"]
    t_end = step_meta["simulated_seconds"]

    # --- oscillation comparison: hold vs step, same post-3600 window ---
    for tag, rows in (("hold", hold_rows), ("step", step_rows)):
        if not rows:
            continue
        entry = {}
        for key, label, _ in CHANNELS:
            pts = series(rows, key, t_step, t_end)
            entry[key] = {
                "label_zh": label,
                "limit_cycle": limit_cycle(pts),
                "detrended": detrended_spread(pts),
            }
        res["experiments"][tag] = entry

    # --- step response characterisation ---
    for key, label, role in CHANNELS:
        pts = series(step_rows, key)
        env = fit_envelope(pts, t_step)
        res["channels"][key] = {"label_zh": label, "role_zh": role,
                                "envelope_fit": env}

    # --- coupled groups: distinguish 1:1 from partial propagation ---
    g1_env = res["channels"]["g1_sec_return_C"]["envelope_fit"]
    for key, label in COUPLED:
        pts = series(step_rows, key)
        env = fit_envelope(pts, t_step)
        row = {"channel": key, "label_zh": label, "envelope_fit": env}
        if env and g1_env and abs(g1_env["delta_K"]) > 1e-9:
            row["gain_vs_g1"] = round(env["delta_K"] / g1_env["delta_K"], 4)
        res["coupled_groups"].append(row)

    # --- verdict ---
    ret = res["channels"]["g1_sec_return_C"]["envelope_fit"]
    hold_ret = res["experiments"].get("hold", {}).get("g1_sec_return_C", {})
    step_ret = res["experiments"].get("step", {}).get("g1_sec_return_C", {})
    v = {"basis": "g1_sec_return_C"}
    if ret:
        v["dc_gain"] = round(ret["delta_K"] / step_meta["step_size_C"], 4)
        v["t50_s"] = ret.get("t50_s")
        v["t63_s"] = ret.get("t63_s")
        v["t90_s"] = ret.get("t90_s")
        v["tau_s"] = ret.get("tau_s")
        v["tau_band_overall_s"] = ret.get("tau_band_overall_s")
        v["first_order_consistent"] = ret.get("first_order_consistent")
        v["t90_over_t63"] = ret.get("t90_over_t63")
        if ret.get("t90_s"):
            v["supervisory_5_30s_over_t90"] = [round(5 / ret["t90_s"], 5),
                                               round(30 / ret["t90_s"], 5)]
    v["oscillation_is_intrinsic"] = bool(
        hold_ret.get("limit_cycle") is None
    )
    v["oscillation_hold"] = (hold_ret.get("detrended") or {}).get("stdev_K")
    v["oscillation_step"] = (step_ret.get("detrended") or {}).get("stdev_K")
    if ret and ret.get("t90_s"):
        t90 = ret["t90_s"]
        if t90 >= 180:
            v["cycle_judgement"] = (
                f"回路整体阶跃响应 t90 ≈ {t90:.0f} s（分钟量级）。"
                "5–30 s 监督层刷新相对热过程接近准稳态，"
                "监督层周期在物理上充分；不需要为实现秒级闭环而进入设备控制层的专用硬件。"
            )
        else:
            v["cycle_judgement"] = "回路响应接近监督层周期，需重新评估控制周期与形态。"
    if step_ret.get("limit_cycle") and not hold_ret.get("limit_cycle"):
        v["oscillation_judgement"] = (
            "振荡只在设定值阶跃后出现、在设定值恒定时不存在，"
            "因此它是 CDU 内环在设定值变化后的调节振荡（极限环），"
            "不是对象自身的固有脉动。含义：监督层下发的每一个设定值变化"
            "都可能激起分钟级振荡，故 (a) 动作幅度与速率应受限，"
            "(b) 效果评价必须用足够长的时间平均窗，不能以单点温度为准。"
        )
    res["verdict"] = v

    (HERE / "thermal_response_analysis.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )

    # ---------------- markdown ----------------
    L = []
    A = L.append
    A("# 单相冷板液冷回路：热响应时间与振荡特性实测")
    A("")
    A("对象：Sustain-LC Frontier 参考 FMU（`LC_Frontier_5Cabinet_4_17_25.fmu`，"
      "FMI 2.0 Co-Simulation，Dymola 2024x / Sdirk34hw）。")
    A("")
    A("两组实验，各 2 小时、15 s 采样，负载与边界完全相同：")
    A("")
    A("| 实验 | 内容 | 用途 |")
    A("|---|---|---|")
    A("| **hold** | 全部 5 组 CDU 二次供温设定恒定 28 ℃ | 对照：证明振荡是否由设定值变化引起 |")
    A(f"| **step** | 同上，但第 1 组在 t = {t_step:.0f} s 阶跃 28 → 30 ℃ | 测热响应时间 |")
    A("")
    A("公共工况：5 组 × 3 × 60 kW 热负载；湿球 20 ℃；二次压差输入 27.5；"
      "阀位 0.33/0.33/0.34。模型启动存在人工高初值"
      "（t = 15 s 时回液 177.8 ℃），前 300 s 不参与统计。")
    A("")
    A("> **参考模型的开环实测，用于判断控制周期需求与识别控制难点。**"
      "不代表任何真实 AIDC 的热性能，不是节能率，也不是热安全结论。")
    A("")
    A("## 结果一：热响应时间是分钟量级")
    A("")
    A("| 通道 | 阶跃前 | 阶跃后 | 变化 | 直流增益 | t50 | t63 | t90 | τ 估计带 | t90/t63 |")
    A("|---|---|---|---|---|---|---|---|---|---|")
    for key, label, _ in CHANNELS:
        e = res["channels"][key]["envelope_fit"]
        if not e:
            A(f"| {label} | — | — | — | — | — | — | — | 无法拟合 |")
            continue
        g = e["delta_K"] / step_meta["step_size_C"]
        tb = e.get("tau_band_overall_s")
        tbs = f"{tb[0]}–{tb[1]} s" if tb else "—"
        A(f"| {label} | {e['x_before_K']} | {e['x_after_K']} | {e['delta_K']:+} | "
          f"{g:.4f} | {e.get('t50_s')} s | {e.get('t63_s')} s | **{e.get('t90_s')} s** | "
          f"{tbs} | {e.get('t90_over_t63','—')} |")
    A("")
    if ret and not ret.get("first_order_consistent", True):
        A(f"> **注意：该回路不是一阶系统。** t90/t63 = {ret.get('t90_over_t63')}"
          f"（一阶应为 ≈2.3），因此 τ 只能给量级带 "
          f"{ret.get('tau_band_overall_s')} s，不能给单点值。"
          "工程上应以 t50/t90 这类上升时间作为设计依据，而不是 τ。")
        A("")
    A("## 结果二：阶跃后出现持续振荡，恒定时没有")
    A("")
    A("| 通道 | hold 去趋势标准差 | step 去趋势标准差 | step 振荡周期 | step 峰峰 |")
    A("|---|---|---|---|---|")
    for key, label, _ in CHANNELS:
        h = (res["experiments"].get("hold", {}).get(key) or {})
        s = (res["experiments"].get("step", {}).get(key) or {})
        hd = (h.get("detrended") or {}).get("stdev_K")
        sd = (s.get("detrended") or {}).get("stdev_K")
        lc = s.get("limit_cycle") or {}
        A(f"| {label} | {hd} K | {sd} K | {lc.get('period_s','—')} s | "
          f"{lc.get('peak_to_peak_K','—')} K |")
    A("")
    if v.get("oscillation_judgement"):
        A(f"> {v['oscillation_judgement']}")
        A("")
    A("## 结果三：设定值变化会通过一次侧传播到其他组")
    A("")
    A("只有第 1 组的设定值被改变，但其余 4 组的二次回液温度也明显上移"
      "（一次侧为共享回路，第 1 组少取热后抬高了共同的一次侧回水温度）。")
    A("")
    A("| 组 | 二次回液变化 | 相对第 1 组的增益 | t90 |")
    A("|---|---|---|---|")
    g1d = g1_env["delta_K"] if g1_env else None
    A(f"| 第 1 组（被阶跃） | {g1d:+.4f} K | 1.0000 | "
      f"{g1_env.get('t90_s') if g1_env else '—'} s |")
    for c in res["coupled_groups"]:
        e = c["envelope_fit"]
        if not e:
            continue
        A(f"| {c['label_zh']}（未阶跃） | {e['delta_K']:+.4f} K | "
          f"{c.get('gain_vs_g1','—')} | {e.get('t90_s','—')} s |")
    A("")
    A("**这条对产品设计是硬约束**：单台 CDU 的设定值动作不是局部动作，"
      "它会通过共享一次侧影响同管网的其他 CDU。任何「逐台独立寻优」都会互相干扰，"
      "必须由**一个协调属主**统一计算同管网各台的设定值。")
    A("")
    A("## 结论")
    A("")
    if ret:
        A(f"**1. 热响应是分钟量级。** 第 1 组二次回液温度对 2 K 供温阶跃的 "
          f"t50 = {ret.get('t50_s')} s、t63 = {ret.get('t63_s')} s、"
          f"t90 = **{ret.get('t90_s')} s**（约 {ret.get('t90_s',0)/60:.1f} 分钟），"
          f"直流增益 {v.get('dc_gain')}（供温升 1 K，稳态回液近似升 0.9 K）。"
          f"该响应非一阶（t90/t63 = {ret.get('t90_over_t63')}），"
          f"故 τ 仅给量级带 {ret.get('tau_band_overall_s')} s。")
        A("")
        A(f"**2. 监督层周期在物理上充分。** 5–30 s 刷新与 t90 之比为 "
          f"{v.get('supervisory_5_30s_over_t90',['—','—'])[0]} – "
          f"{v.get('supervisory_5_30s_over_t90',['—','—'])[1]}。")
        A("")
        A(f"> {v.get('cycle_judgement','')}")
        A("")
    A("**3. 真正的控制难点不是速度，是振荡与耦合。**")
    A("")
    A("- 设定值每次变化都可能激起约 25 分钟周期的振荡（hold 运行中不存在）；")
    A("- 单台动作会经共享一次侧传播到同管网其他 CDU，增益约 0.5。")
    A("")
    A("这两点比「周期要快」更值得写进产品设计：它们直接决定"
      "**动作限幅限速**、**协调属主唯一**、**效果评价必须用长窗口平均**这三条要求。")
    A("")
    A("## 方法")
    A("")
    A("- 稳态电平：阶跃后取 150 s 分块均值的末 20% 作为新稳态，首块作为初值；")
    A("- 热响应 τ/t50/t63/t90 在**分块均值（振荡包络）轨迹**上计算，"
      "而不是在含振荡的原始轨迹上，避免把极限环当成热滞后；")
    A("- 振荡检测：对阶跃后窗口去均值，零穿越取半周期，周期为半周期中位数的 2 倍；"
      "幅度取残差绝对值 95 分位；另用去线性趋势后的标准差作为振荡强度指标；")
    A("- hold 与 step 使用完全相同的负载、边界与统计窗口，可直接对比。")
    A("")
    A("## 局限")
    A("")
    A("- 单一固定拓扑、单一负载水平（5 组各 180 kW）、单一阶跃幅度（2 K）、每工况单次运行；")
    A("- 步长 15 s 为原仓库默认值，不能分辨小于 15 s 的时间常数（本测为分钟量级，不受影响）；")
    A("- FMU 未含源 Modelica 工程，无法改拓扑或参数做敏感性分析；")
    A("- **上升时间的分辨率受 150 s 分块限制**：分块均值用于滤掉 1515 s 振荡，"
      "但块宽决定了 t50/t63/t90 只能落到块的边界（367.5 s、517.5 s 等）。"
      "多数通道 t50 = t63 正是这一分辨率所致，不是真实的陡峭上升。"
      "要提高时间分辨率需在更长窗口上用更细的分块重算，或先做低通滤波再取阈值；")
    A("- 该振荡与耦合是否为真实 AIDC 的普遍现象，需现场数据验证；")
    A("- 本测只回答「控制周期需要多快」与「控制难点在哪」，"
      "不回答「能省多少电」或「是否热安全」。")
    A("")
    A("## 复现")
    A("")
    A("```bash")
    A("D='docker run --rm --pull never --platform linux/amd64 --network none \\")
    A("  --cpus 4 --memory 6g \\")
    A("  --mount type=bind,source=/Users/song/Developer/sustain-lc,target=/source,readonly \\")
    A("  --mount type=bind,source=<workspace>/sustain_lc_validation,target=/validation \\")
    A("  --workdir /tmp sustain-lc:amd64'")
    A("eval $D python /validation/step_response_probe.py --settle-s 3600 --step-s 3600 --mode step")
    A("eval $D python /validation/step_response_probe.py --settle-s 3600 --step-s 3600 --mode hold")
    A("python3 thermal_response_analysis.py")
    A("```")
    (HERE / "thermal_response_analysis.md").write_text("\n".join(L) + "\n")

    print(json.dumps({"verdict": v,
                      "g1_return_envelope": ret,
                      "coupled_gains": [{"ch": c["channel"],
                                         "gain_vs_g1": c.get("gain_vs_g1")}
                                        for c in res["coupled_groups"]]},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
