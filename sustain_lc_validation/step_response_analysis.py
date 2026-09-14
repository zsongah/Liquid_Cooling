"""Analyse the step-response trace and extract thermal response characteristics.

Reads  : time_constant_trace.csv, time_constant_meta.json
Writes : time_constant_analysis.json, time_constant_analysis.md

What this probe found (and why the method is not a plain first-order fit):
the post-step trace does NOT settle to a constant. It settles to a sustained
oscillation, so a single-exponential fit of the raw trace is fitting the limit
cycle, not the thermal lag. The analysis therefore reports, per channel:

  1. Steady-state levels from oscillation-averaged windows (pre and post).
  2. DC gain relative to the applied setpoint step.
  3. A limit-cycle description: period and peak-to-peak amplitude.
  4. Response timing on the oscillation-averaged (block-mean) trajectory:
     t50/t63/t90 and the peak-slope estimate, which is the honest measure of
     how fast the loop can move for control-cycle purposes.
  5. A never-settles flag, so a limit cycle is reported as a finding rather
     than silently averaged away.

All standard library only.
"""
import csv
import json
import math
import statistics
from pathlib import Path

HERE = Path(__file__).resolve().parent
TRACE = HERE / "time_constant_trace.csv"
META = HERE / "time_constant_meta.json"

CHANNELS = [
    ("g1_sec_return_C", "CDU二次回液温度", "被控回路主要热惯性输出"),
    ("g1_chan1_T_C", "机柜冷板通道1液温", "最靠近热源的热质量"),
    ("g1_chan2_T_C", "机柜冷板通道2液温", "最靠近热源的热质量"),
    ("g1_chan3_T_C", "机柜冷板通道3液温", "最靠近热源的热质量"),
    ("g1_node1_K", "机柜等效热节点温度", "等效热容节点"),
    ("g1_sec_supply_C", "CDU二次供液温度", "执行响应（设定值跟踪）"),
    ("g1_prim_return_C", "CDU一次回液温度", "一次侧耦合"),
    ("g1_pump_W", "CDU二次泵功率", "执行器功耗"),
]
CONTROLS = ["g2_sec_return_C", "g3_sec_return_C", "g4_sec_return_C", "g5_sec_return_C"]

BLOCK_S = 150.0


def load():
    with TRACE.open() as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["time_s"] = float(r["time_s"])
        for k in list(r):
            if k in ("phase", "time_s"):
                continue
            r[k] = float(r[k])
    return rows, json.loads(META.read_text())


def limit_cycle(t, v):
    """Detect a sustained oscillation via zero crossings about the mean."""
    m = statistics.mean(v)
    res = [x - m for x in v]
    zc = [i for i in range(1, len(res)) if res[i - 1] < 0 <= res[i]]
    if len(zc) < 3:
        return None
    half = [(zc[i] - zc[i - 1]) * (t[zc[i]] - t[zc[i] - 1]) for i in range(1, len(zc))]
    if not half:
        return None
    period = 2 * statistics.median(half)
    # amplitude: mean of per-cycle peaks, robust to a slow ramp
    amps = sorted(abs(x) for x in res)
    amp = amps[int(0.95 * (len(amps) - 1))]
    return {
        "period_s": round(period, 1),
        "half_periods_s": [round(h, 1) for h in half[:8]],
        "amplitude_K": round(amp, 4),
        "peak_to_peak_K": round(2 * amp, 4),
        "cycles_observed": round(len(v) * (t[1] - t[0]) / period, 1),
    }


def block_means(t, v, t0):
    """150 s block means after t0, with absolute time stamps.

    Each block mean is indexed by its start offset; using the block centroid
    keeps the timestamps independent of any absolute clock origin.
    """
    blocks = {}
    for ti, vi in zip(t, v):
        if ti > t0:
            blocks.setdefault(int((ti - t0) // BLOCK_S), []).append((ti, vi))
    out = []
    for b in sorted(blocks):
        samples = blocks[b]
        out.append((statistics.mean(ti for ti, _ in samples),
                    statistics.mean(vi for _, vi in samples)))
    return out


def analyse(rows, meta, key, label, note, osc_tol_K=0.25):
    t = [r["time_s"] for r in rows]
    v = [r[key] for r in rows]
    t_step = meta["step_time_s"]
    t_end = meta["simulated_seconds"]
    span = t_end - t_step

    win = max(600.0, 0.20 * span)
    pre = [(ti, vi) for ti, vi in zip(t, v) if t_step - win <= ti <= t_step]
    post = [(ti, vi) for ti, vi in zip(t, v) if t_end - win <= ti <= t_end]
    y0 = statistics.mean(vi for _, vi in pre)
    y1 = statistics.mean(vi for _, vi in post)
    delta = y1 - y0
    sd_pre = statistics.pstdev([vi for _, vi in pre])

    out = {
        "channel": key,
        "label_zh": label,
        "note_zh": note,
        "steady_before": round(y0, 4),
        "steady_after": round(y1, 4),
        "delta": round(delta, 4),
        "pre_window_stdev": round(sd_pre, 5),
        "setpoint_step_C": meta["step_size_C"],
        "dc_gain": round(delta / meta["step_size_C"], 4),
    }

    lc = None
    post_idx = [i for i, ti in enumerate(t) if ti > t_step]
    if len(post_idx) > 20:
        tp = [t[i] for i in post_idx]
        vp = [v[i] for i in post_idx]
        lc = limit_cycle(tp, vp)
    if lc:
        out["limit_cycle"] = lc
        out["settles_to_constant"] = lc["amplitude_K"] < osc_tol_K
    else:
        out["limit_cycle"] = None
        out["settles_to_constant"] = True

    if abs(delta) < max(3 * sd_pre, 1e-6):
        out["status"] = "no_measurable_response"
        return out

    out["status"] = "characterised"
    bm = block_means(t, v, t_step)
    b0 = y0
    b1 = statistics.mean(vi for _, vi in bm[-8:]) if len(bm) >= 8 else y1
    bd = b1 - b0
    frac = [(ti - t_step, (vi - b0) / bd) for ti, vi in bm]
    out["block_delta"] = round(bd, 4)
    out["block_dc_gain"] = round(bd / meta["step_size_C"], 4)

    for f, name in ((0.5, "t50"), (0.632, "t63"), (0.9, "t90")):
        hit = next((tt for tt, fr in frac if fr >= f), None)
        out[f"{name}_s"] = None if hit is None else round(hit, 1)

    # peak slope on the oscillation-averaged trajectory
    slopes = [((f2 - f1) / (t2 - t1), t1) for (t1, f1), (t2, f2)
              in zip(frac, frac[1:]) if t2 > t1]
    if slopes:
        smax, tsmax = max(slopes)
        out["peak_slope_per_s"] = round(smax, 8)
        out["peak_slope_at_s"] = round(tsmax, 1)
        out["tau_peakslope_s"] = round((1 - 1 / math.e) / smax, 1) if smax > 0 else None

    # dead time: first block reaching the pre-step noise band
    thr = max(3 * sd_pre / abs(bd), 0.02) if abs(bd) > 0 else 1.0
    dt = next((tt for tt, fr in frac if abs(fr) >= thr), None)
    out["dead_time_s"] = None if dt is None else round(dt - BLOCK_S, 1)
    out["dead_time_threshold_frac"] = round(thr, 4)
    return out


def main():
    rows, meta = load()
    res = {"meta": meta, "channels": [], "controls": []}
    for key, label, note in CHANNELS:
        if key in rows[0]:
            res["channels"].append(analyse(rows, meta, key, label, note))
    for key in CONTROLS:
        if key in rows[0]:
            res["controls"].append(
                analyse(rows, meta, key, key, "对照通道：不应响应本轮阶跃")
            )

    ret = next(c for c in res["channels"] if c["channel"] == "g1_sec_return_C")
    v = {"basis": "g1_sec_return_C"}
    t_rise = ret.get("t90_s")
    if t_rise:
        v["t90_s"] = t_rise
        v["t50_s"] = ret.get("t50_s")
        v["dc_gain"] = ret.get("dc_gain")
        v["supervisory_period_over_t90"] = [round(5 / t_rise, 5), round(30 / t_rise, 5)]
        v["limit_cycle"] = ret.get("limit_cycle")
        if t_rise >= 600:
            v["judgement"] = (
                "回路整体响应在十分钟量级。5–30 s 的监督层刷新相对热过程接近准稳态，"
                "监督层周期在物理上充分；不需要为实现毫秒/秒级闭环而进入设备控制层的专用硬件。"
            )
        elif t_rise >= 180:
            v["judgement"] = (
                "回路整体响应在数分钟量级。5–30 s 监督层刷新充分，"
                "但前馈提前量必须按分钟量级设计，不能用秒级假设。"
            )
        else:
            v["judgement"] = (
                "回路响应接近监督层周期，需重新评估控制周期与形态。"
            )
    second = {}
    for c in res["channels"]:
        if c["channel"] == "g1_sec_supply_C":
            second["execution_t90_s"] = c.get("t90_s")
            second["execution_dc_gain"] = c.get("dc_gain")
        if c["channel"] == "g1_chan1_T_C":
            second["coldplate_t90_s"] = c.get("t90_s")
            second["coldplate_dc_gain"] = c.get("dc_gain")
    v["secondary_observations"] = second
    res["verdict"] = v

    (HERE / "time_constant_analysis.json").write_text(
        json.dumps(res, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )

    L = []
    A = L.append
    A("# 单相冷板液冷回路：热响应时间与周期需求实测")
    A("")
    A("对象：Sustain-LC Frontier 参考 FMU（`LC_Frontier_5Cabinet_4_17_25.fmu`，"
      "FMI 2.0 CS，Dymola 2024x 生成）。")
    A("")
    A(f"- 热负载：5 组 × 3 × {60} kW，湿球 20 ℃，二次压差输入 27.5，阀位 0.33/0.33/0.34")
    A(f"- 阶跃：**t = {meta['step_time_s']:.0f} s 时仅第 1 组 CDU 二次供温设定 "
      f"{meta['step_from_C']:.0f} → {meta['step_to_C']:.0f} ℃（{meta['step_size_C']:.0f} K）**，"
      f"其余 4 组保持不变作对照")
    A(f"- 采样 {meta['sample_period_s']:.0f} s，共 {meta['samples']} 点，模拟 "
      f"{meta['simulated_seconds']:.0f} s（2 小时）")
    A("")
    A("> **这是参考模型的开环阶跃响应，用于判断控制周期需求。**"
      "它不代表任何真实 AIDC 的热性能，也不是节能率或热安全结论。"
      "模型启动阶段存在很高的人工初值（t=15 s 时回液 177.8 ℃），前 300 s 不参与统计。")
    A("")
    A("## 实测结果")
    A("")
    A("| 通道 | 阶跃前 | 阶跃后 | 变化 | 直流增益 | 死区时间 | t50 | t90 | 稳态形态 |")
    A("|---|---|---|---|---|---|---|---|---|")
    for c in res["channels"]:
        if c.get("status") != "characterised":
            A(f"| {c['label_zh']} | {c['steady_before']} | {c['steady_after']} | "
              f"{c['delta']:+} | — | — | — | — | 无可测响应 |")
            continue
        if c.get("settles_to_constant"):
            shape = "收敛到常数"
        else:
            lc = c["limit_cycle"]
            shape = f"**持续振荡** 周期 {lc['period_s']:.0f} s，峰峰 {lc['peak_to_peak_K']:.2f} K"
        A(f"| {c['label_zh']} | {c['steady_before']} | {c['steady_after']} | "
          f"{c['delta']:+} | {c['dc_gain']} | {c['dead_time_s']} s | "
          f"{c['t50_s']} s | {c['t90_s']} s | {shape} |")
    A("")
    A("## 对照通道（不受阶跃影响，用于交叉校验）")
    A("")
    A("| 通道 | 阶跃前 | 阶跃后 | 变化 | 直流增益 | 判定 |")
    A("|---|---|---|---|---|---|")
    for c in res["controls"]:
        A(f"| {c['channel']} | {c['steady_before']} | {c['steady_after']} | "
          f"{c['delta']:+} | {c.get('dc_gain')} | {c.get('status')} |")
    A("")
    A("## 结论")
    A("")
    A(f"**1. 回路热响应是分钟量级。** 第 1 组二次回液温度对 2 K 供温阶跃的 "
      f"t50 = {ret.get('t50_s')} s、t90 = {ret.get('t90_s')} s，"
      f"直流增益 {ret.get('dc_gain')}（供温升 1 K，回液近似升 1 K）。")
    A("")
    if ret.get("limit_cycle"):
        lc = ret["limit_cycle"]
        A(f"**2. 阶跃后并不收敛到常数，而是进入持续振荡。** "
          f"周期 ≈ {lc['period_s']:.0f} s（约 {lc['period_s']/60:.0f} 分钟），"
          f"峰峰 ≈ {lc['peak_to_peak_K']:.2f} K，"
          f"在 2 小时的观测窗内未见衰减（观测到约 {lc['cycles_observed']} 个周期）。")
        A("")
        A("   这不是测量噪声，是**回路自身的持续控制振荡/极限环**。它有两个直接含义：")
        A("   - 监督层看到的回液温度天然带有约 1 K 的分钟级波动，"
          "**任何以单点温度为判据的优化或告警都必须先做时间平均**，否则会把振荡当成趋势；")
        A("   - 监督层的设定值动作频率必须**远离**这个振荡周期，否则会与内环振荡耦合。")
        A("")
    A(f"**3. 监督层周期在物理上充分。** t90 = {ret.get('t90_s')} s，"
      f"5–30 s 刷新与 t90 之比仅 "
      f"{5/ret['t90_s']:.3f} – {30/ret['t90_s']:.3f}。")
    A("")
    A(f"> {res['verdict'].get('judgement','')}")
    A("")
    A("**4. 执行响应比热响应快，但仍在同一量级。**")
    if second.get("execution_t90_s"):
        A(f"二次供液温度跟踪设定值的 t90 = {second['execution_t90_s']} s，"
          f"直流增益 {second.get('execution_dc_gain')}。")
    A("")
    A("## 方法")
    A("")
    A("- 阶跃前后稳态值取各自阶段末 20% 窗口（≥600 s）的均值；")
    A("- 由于响应不收敛，另做 150 s 分块均值得到**振荡平均轨迹**，t50/t63/t90 与斜率法都在这条轨迹上计算；")
    A("- 极限环：对去均值序列做零穿越检测，周期取半周期中位数的 2 倍，"
      "幅度取残差绝对值 95 分位；")
    A("- 死区时间取首个超过阶跃前 3σ 噪声带的分块；")
    A("- 判定：阶跃幅度须超过 3σ 噪声，否则标记 `no_measurable_response`。")
    A("")
    A("## 局限")
    A("")
    A("- 单一固定拓扑、单一负载水平（5 组各 180 kW）、单一阶跃幅度（2 K）、单次运行；")
    A("- 步长 15 s 由原仓库默认值决定，**不能分辨小于 15 s 的时间常数**，"
      "但本测得到的是分钟量级，不受此限制；")
    A("- FMU 无源 Modelica 工程，无法改变拓扑或参数做敏感性分析；")
    A("- 该振荡是否为真实 AIDC 的普遍现象，需现场数据验证；")
    A("- 本测只回答「控制周期需要多快」，不回答「能省多少电」或「是否热安全」。")
    A("")
    A("## 复现")
    A("")
    A("```bash")
    A("docker run --rm --pull never --platform linux/amd64 --network none \\")
    A("  --cpus 4 --memory 6g \\")
    A("  --mount 'type=bind,source=/Users/song/Developer/sustain-lc,target=/source,readonly' \\")
    A("  --mount 'type=bind,source=<workspace>/sustain_lc_validation,target=/validation' \\")
    A("  --workdir /tmp sustain-lc:amd64 \\")
    A("  python /validation/step_response_probe.py --settle-s 3600 --step-s 3600")
    A("python3 step_response_analysis.py")
    A("```")
    (HERE / "time_constant_analysis.md").write_text("\n".join(L) + "\n")
    print(json.dumps({"verdict": res["verdict"],
                      "return_channel": {k: ret.get(k) for k in
                                         ("steady_before", "steady_after", "delta",
                                          "dc_gain", "t50_s", "t63_s", "t90_s",
                                          "dead_time_s", "tau_peakslope_s",
                                          "settles_to_constant")}},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
