"""从实际 FMU CSV 和结果摘要生成对比图及中文报告，不重新运行模型。"""
import csv
import html
import json
from pathlib import Path


def build_report(output):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    variants = ("baseline", "fixed", "adaptive")
    labels = {"baseline": "Fixed setpoint", "fixed": "Fixed model policy", "adaptive": "Adaptive policy"}
    chinese = {"baseline": "固定目标", "fixed": "固定参数算法", "adaptive": "在线校准算法"}
    colors = {"baseline": "#8b9aa7", "fixed": "#c48a36", "adaptive": "#087f8c"}
    summaries = {v: json.loads((output / v / "summary.json").read_text()) for v in variants}
    traces = {}
    for v in variants:
        with (output / v / "trace.csv").open() as file:
            traces[v] = [{k: float(x) for k, x in r.items()} for r in csv.DictReader(file)]
    signature = lambda rows: [(r["time_s"], r["blade_input_w"]) for r in rows]
    if any(signature(traces[v]) != signature(traces["baseline"]) for v in variants):
        raise ValueError("comparison_time_or_disturbance_mismatch")
    fig, axes = plt.subplots(3, 2, figsize=(12, 10), constrained_layout=True)
    specs = [("blade_input_w", "FMU heat input per channel", "kW", .001),
             ("g1_return_degC", "CDU 1 return temperature", "degC", 1),
             ("g1_flow_kg_s", "CDU 1 secondary mass flow", "kg/s", 1),
             ("g1_dp_kpa", "CDU 1 measured differential pressure", "kPa", 1),
             ("g1_pump_w", "CDU 1 pump power", "kW", .001),
             ("gain", "Controller hydraulic gain", "kg/s / sqrt(kPa)", 1)]
    for ax, (key, title, unit, factor) in zip(axes.flat, specs):
        for v in variants:
            if key == "blade_input_w" and v != "baseline":
                continue
            if key == "gain" and v == "baseline":
                continue
            ax.plot([r["elapsed_s"] / 60 for r in traces[v]], [r[key] * factor for r in traces[v]],
                    label=labels[v], color=colors[v], linewidth=1.5)
        ax.set(title=title, xlabel="Time after warmup (min)", ylabel=unit)
        ax.grid(alpha=.2)
        ax.spines[["top", "right"]].set_visible(False)
    handles, names = axes[0, 1].get_legend_handles_labels()
    fig.legend(handles, names, loc="outside upper center", ncol=3, frameon=False)
    fig.savefig(output / "comparison.png", dpi=160)
    fig.savefig(output / "comparison.svg")
    plt.close(fig)
    rows = []
    for v in variants:
        r = summaries[v]
        rows.append("<tr><td>%s</td><td>%.3f</td><td>%.3f</td><td>%.2f</td><td>%.2f</td><td>%s / %s</td></tr>" % (
            chinese[v], r["energy"]["g1_pump_kwh"], r["energy"]["represented_cooling_kwh"],
            r["g1_peak_return_degC"], r["all_groups_peak_return_degC"], r["confirmed"], r["model_updates"]))
    e = summaries["baseline"]["experiment"]
    links = " · ".join('<a href="%s/report.html">%s运行日志</a>' % (v, chinese[v]) for v in variants)
    negative = min(r["min_absolute_pressure_pa"] for r in summaries.values())
    body = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sustain-LC FMU 闭环控制实验</title><style>
*{box-sizing:border-box}body{margin:0;background:#f1f5f6;color:#17333e;font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1180px;margin:auto;padding:40px 24px}h1{font-size:32px;line-height:1.5;margin:12px 0}h2{font-size:21px}section{background:white;border:1px solid #dce7e9;border-radius:12px;padding:24px;margin:20px 0}
.eyebrow,a{color:#087f8c}.note{background:#fff6e6;border-left:4px solid #c48a36;padding:16px 20px;margin:20px 0}table{width:100%%;border-collapse:collapse;font-size:14px;min-width:700px}td,th{text-align:left;padding:12px;border-bottom:1px solid #dce7e9}th{background:#edf5f5}.table{overflow:auto}img{display:block;width:100%%}code{background:#edf5f5;padding:2px 5px}li{margin:6px 0}.muted{color:#5e747b;font-size:14px}@media(max-width:650px){main{padding:20px 12px}section{padding:16px}h1{font-size:26px}}
</style><main><div class="eyebrow">LC CONTROL / SUSTAIN-LC · 新 FMU 闭环实验</div>
<h1>现有算法，正在控制实际 FMU</h1><p>FMU 测量 → 标准设备接口 → 原有 FlowPolicy 与命令网关 → FMU 压差输入 → 下一步物理响应。</p>
<div class="note">这是固定五组拓扑的参考模型实验。仅第 1 组 CDU 接受算法调节，其余四组、阀门和冷源目标保持固定；整个模型由一个主程序推进。不是厂家实机验证，也不是多 CDU 或全站能耗优化。</div>
<section><h2>匹配条件与运行结果</h2><p>各策略在独立进程加载同一 FMU，预热 %.0f 分钟，比较随后 %.0f 分钟；通信步长 %.0f 秒，控制间隔 %.0f 秒。所有策略使用相同负荷输入和邻机边界。</p>
<div class="table"><table><thead><tr><th>策略</th><th>CDU 1 泵电量<br>kWh</th><th>模型所列冷却电量<br>kWh</th><th>CDU 1 最高回温<br>°C</th><th>五组最高回温<br>°C</th><th>目标确认 / 参数更新</th></tr></thead><tbody>%s</tbody></table></div>
<p class="muted">模型所列冷却电量 = 5 台 CDU 泵 + 热水环路泵 + 冷却塔环路泵 + 冷却塔风机。没有计入模型外设备和 IT 用电；它不是完整 AIDC 总电耗。按通信步的端点功率使用梯形积分。</p></section>
<section><h2>实际响应曲线</h2><p class="muted">横轴从预热结束开始；热源输入是 FMU 外部通道输入，不等于已验证的 CDU 液冷热负荷。算法本轮仍从温差与流量估算热需求。</p><img src="comparison.svg" alt="三种策略的负荷、回温、流量、实际压差、泵功率和模型增益对比"><p><a href="comparison.png">PNG 图</a> · <a href="comparison.svg">矢量图</a></p></section>
<section><h2>必须一起阅读的边界</h2><ul>
<li>FMU 原始压差输入按 psi 比较；27.5 对应约 189.6 kPa。当前映射使用内部转换系数及真实供回压差，旧回放的 kPa 假设已被本次审计纠正。</li>
<li>原 FMU 启动时存在很大的温度瞬态，预热不纳入性能比较。预热后的包络检查不能证明启动过程安全。</li>
<li>参考模型输出出现负绝对压力，最低约 %.0f Pa。差压和动态可用于当前软件实验，但这个物理一致性问题限制了工程外推，不能把模型当作已校准现场对象。</li>
<li>温度包络均为实验参数。冷却通道温度不是芯片结温；本次不验证机柜或芯片热安全。</li>
<li>回读确认的是 FMU 输入值，过程压差和温度仍需时间响应。实验释放只回到固定目标，不代表 OEM 本地接管或看门狗验收。</li>
<li>算法没有以模型所列总冷却电量为目标；即使泵耗下降，也须同时看温度和其他冷却设备电量。</li>
</ul></section><section><h2>运行证据</h2><p>%s</p><p><a href="manifest.json">完整结果与代码哈希</a> · <a href="interface_contract.json">FMU 接口核对</a> · <a href="scene.json">实验配置</a></p><p>每组目录保存 trace.csv、runtime.sqlite、commands.jsonl、fmu_writes.json 和 summary.json，可逐周期检查实际输入和输出。</p></section></main></html>''' % (
        e["warmup_s"] / 60, e["evaluation_s"] / 60, e["communication_step_s"], e["control_interval_s"],
        "".join(rows), negative, links)
    (output / "index.html").write_text(body, encoding="utf-8")
    print(output / "index.html")
