# -*- coding: utf-8 -*-
"""重跑 P0/P1 合成验收案例并生成离线 HTML；不访问任何设备。

每个案例保存完整配置和原始分析结果。留出数据由独立二次阻力解析式生成，
不把求解器自己的输出回填成实测；这仍只验证内部数值一致性，不验证现场。
"""
import copy
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lc_control.hydraulics import analyze_scene
from benchmark_hydraulics import run_benchmark

OUT = ROOT / "outputs/hydraulic-analysis"


def evidence_scene(total_only):
    """两个纯二次阻力并联：q1=sqrt(dp/1e5)，q2=sqrt(dp/4e5)。"""
    value = json.loads((ROOT / "examples/hydraulic_parallel_v02.json").read_text())
    value["scene_id"] = "analytic_observability_fixture"
    value["assets"] = [{"id": "CDU", "kind": "cdu"}]
    value["hydraulics"]["junctions"] = [
        {"id": key, "circuit_id": "SECONDARY", "elevation_m": 0, "pressure_pa": p,
         "pressure_uncertainty_pa": 0, "source": "independent analytic fixture", "evidence_scope": "synthetic"}
        for key, p in (("S", 100000), ("T", 0))]
    value["hydraulics"]["elements"] = [
        {"id": key, "kind": "resistance", "from_junction": "S", "to_junction": "T",
         "parameters": {"resistance_pa_per_kg_s2": r, "relative_uncertainty": 0}}
        for key, r in (("E1", 100000), ("E2", 400000))]
    value["branches"] = [{"id": "B1", "flow_element_id": "E1", "min_flow_kg_s": 0.1},
                         {"id": "B2", "flow_element_id": "E2", "min_flow_kg_s": 0.1}]
    value["control_domains"] = [{"id": "D", "member_asset_ids": ["CDU"], "circuit_ids": ["SECONDARY"],
                                  "branch_ids": ["B1", "B2"], "actuator_ids": []}]
    observations = [{"element_ids": ["E1", "E2"], "uncertainty_kg_s": 0.001}] if total_only else [
        {"element_ids": [key], "uncertainty_kg_s": 0.001} for key in ("E1", "E2")]
    value["analysis"] = {"identifiability": {
        "parameters": [{"element_id": key, "name": "resistance_pa_per_kg_s2"} for key in ("E1", "E2")],
        "experiments": [{"id": str(p), "boundary_pressures_pa": {"S": p}, "observations": observations}
                        for p in (100000, 25000)],
        "rank_rtol": 1e-7, "max_condition": 10000, "max_relative_interval_width": 0.1},
        "validation": {"evidence_scope": "synthetic", "dataset_id": "analytic-held-out",
            "calibration_dataset_id": "declared-parameter-fixture", "acceptance": {
                "absolute_error_kg_s": 0.002, "relative_error": 0.01,
                "minimum_cases_per_branch": 2, "require_ordering": True},
            "cases": [{"id": str(p), "boundary_pressures_pa": {"S": p}, "observations": [
                {"branch_id": key, "flow_kg_s": math.sqrt(p / r), "uncertainty_kg_s": 0.001}
                for key, r in (("B1", 100000), ("B2", 400000))]} for p in (64000, 144000)]}}
    value["extensions"] = {"purpose": "Independent analytic reference, not field data"}
    return value


def cases():
    base = json.loads((ROOT / "examples/hydraulic_parallel_v02.json").read_text())
    reduced = copy.deepcopy(base)
    reduced["hydraulics"]["junctions"][0]["pressure_pa"] = 260000
    blocked = copy.deepcopy(base)
    blocked["hydraulics"]["elements"][-1]["parameters"]["resistance_pa_per_kg_s2"] *= 4
    uncertain = copy.deepcopy(base)
    del uncertain["hydraulics"]["elements"][-1]["parameters"]["relative_uncertainty"]
    isolated = copy.deepcopy(base)
    for element in isolated["hydraulics"]["elements"]:
        if element["id"] in ("RACK_B_SUPPLY", "RACK_B_RETURN"):
            element["state"]["closed"] = True
    return [("base", "共享总管 · 两柜四节点", "100 kPa 边界压差；总管与各支路串并联耦合。B2 最小要求 1 kg/s，需求不强制加入方程。", base),
            ("lower_pressure", "可用压差降低", "边界压差改为 60 kPa，其余参数保持；这是静态边界比较，不是泵降额模型。", reduced),
            ("high_resistance", "单支路阻力增大", "B2 等效二次阻力变为四倍；同时查看其他支路的耦合变化。不能据此认定真实堵塞原因。", blocked),
            ("missing_uncertainty", "误差声明缺失", "保留名义解，但停止给出有保证的流量区间，判定 unknown。", uncertain),
            ("isolated", "机柜供回同时隔离", "形成无压力基准的浮置子图；返回 failed，不沿用旧解或伪造压力。", isolated),
            ("total_only", "只有总流量 · 秩不足", "两参数、两压差工况只有总量观测；局部秩应为 1，辨识 fail。", evidence_scene(True)),
            ("branch_meters", "增加支路反馈 · 局部筛查", "每条支路观测，局部秩为 2；解析留出案例同时检查误差与排序。pass 不代表全局唯一或现场准入。", evidence_scene(False))]


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    return "—" if value is None else "%.4g" % value


def chart(branches):
    """固定公共横轴展示名义点、误差线和最小要求；无区间时仅显示名义点。"""
    xmax = max([b.get("min_flow_kg_s") or 0 for b in branches] +
               [max(b.get("flow_interval_kg_s") or [b.get("estimated_flow_kg_s") or 0]) for b in branches] + [1]) * 1.1
    width, left, scale = 860, 175, 610 / xmax
    out = ['<svg role="img" aria-label="流量估计与区间，竖线为最小要求" viewBox="0 0 860 %s">' % (len(branches) * 42 + 50)]
    for i, b in enumerate(branches):
        y = i * 42 + 24
        color = {"satisfied": "#087f79", "violated": "#bd453e", "unknown": "#a97715"}[b["status"]]
        out.append('<text x="0" y="%s">%s</text><path d="M175 %s H800" stroke="#e4eaed"/>' % (y + 4, esc(b["id"]), y))
        interval = b["flow_interval_kg_s"]
        if interval:
            out.append('<path d="M%.3f %s H%.3f" stroke="%s" stroke-width="8" opacity=".45"/>' % (left + interval[0] * scale, y, left + interval[1] * scale, color))
        if b["estimated_flow_kg_s"] is not None:
            out.append('<circle cx="%.3f" cy="%s" r="5" fill="%s"/>' % (left + b["estimated_flow_kg_s"] * scale, y, color))
        if b["min_flow_kg_s"] is not None:
            x = left + b["min_flow_kg_s"] * scale
            out.append('<path d="M%.3f %s V%s" stroke="#183446" stroke-width="2"/>' % (x, y - 11, y + 11))
    out.append('<text x="175" y="%s">0</text><text x="740" y="%s">%s kg/s</text></svg>' % (len(branches) * 42 + 32, len(branches) * 42 + 32, number(xmax)))
    return "".join(out)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    runs, cards = [], []
    for key, title, note, scene in cases():
        report = analyze_scene(scene)
        (OUT / (key + "-scene.json")).write_text(json.dumps(scene, ensure_ascii=False, indent=2, allow_nan=False))
        (OUT / (key + ".json")).write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
        runs.append({"id": key, "title": title, "result": report})
        rows = "".join('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td class="%s">%s</td></tr>' % (
            esc(b["id"]), number(b["estimated_flow_kg_s"]),
            " — ".join(number(x) for x in b["flow_interval_kg_s"]) if b["flow_interval_kg_s"] else "未确定",
            number(b["min_flow_kg_s"]), b["status"], b["status"]) for b in report["branches"])
        cards.append('<section id="%s"><div class="eyebrow">CASE %02d / MODEL ESTIMATE</div><h2>%s</h2><p>%s</p><div class="pills"><b>求解 %s</b><b>流量约束 %s</b><b>辨识 %s</b><b>留出校核 %s</b></div><p class="reason">%s</p>%s<div class="scroll"><table><thead><tr><th>支路</th><th>估计 kg/s</th><th>区间 kg/s</th><th>最低 kg/s</th><th>判定</th></tr></thead><tbody>%s</tbody></table></div><details><summary>配置、判断依据和原始结果</summary><p><a href="%s-scene.json">完整输入配置</a> · <a href="%s.json">原始 JSON 结果</a></p><pre>%s</pre></details></section>' % (
            key, len(runs), esc(title), esc(note), esc(report["solver"]["status"]), esc(report["feasibility"]["status"]),
            esc(report["identifiability"]["status"]), esc(report["validation"]["status"]), esc(report["solver"].get("reason") or "质量守恒与元件原方程残差检查通过；所有数值均为模型估计。"),
            chart(report["branches"]), rows, key, key, esc(json.dumps(report, ensure_ascii=False, indent=2))))
    benchmark = run_benchmark()
    (OUT / "benchmark.json").write_text(json.dumps(benchmark, ensure_ascii=False, indent=2, allow_nan=False))
    (OUT / "results.json").write_text(json.dumps(runs, ensure_ascii=False, indent=2, allow_nan=False))
    timing = "".join('<tr><td>%s</td><td>%s / %s</td><td>%s</td><td>%.3f / %.3f</td><td>%s</td></tr>' % (
        esc(c["id"]), c["element_count"], c["unknown_pressure_count"],
        "超限拒绝延迟" if c["timing_kind"] == "rejection_latency" else "名义求解",
        c["latency_ms"]["p95"], c["latency_ms"]["max"], "符合预期" if c["all_expectations_met"] else "失败") for c in benchmark["cases"])
    page = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>P0/P1 多支路水力分析验收</title><style>
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f0f4f5;color:#183446;font:15px/1.75 system-ui,"PingFang SC",sans-serif}main{max-width:1140px;margin:auto;padding:45px 28px 80px}header{padding:38px;background:#153b48;color:#fff;border-radius:20px}.eyebrow{font:12px/1.5 ui-monospace,monospace;letter-spacing:1.4px;color:#087f79}header .eyebrow{color:#96dace}h1{font-size:36px;line-height:1.3;margin:18px 0}header p{color:#c7dbde;max-width:810px}h2{font-size:24px;margin:12px 0}h3{font-size:18px}section{padding:28px 32px;border:1px solid #dae5e8;border-radius:16px;background:#fff;margin-top:22px}nav{display:flex;gap:10px;flex-wrap:wrap;margin:24px 0}a{color:#087f79;text-underline-offset:4px}nav a{background:#fff;border:1px solid #d8e4e5;border-radius:8px;padding:8px 12px;text-decoration:none;font-size:13px}.pills{display:flex;gap:9px;flex-wrap:wrap;font-size:12px}.pills b{background:#ecf3f4;padding:6px 10px;border-radius:6px}.reason{color:#647e89;font-size:13px}.scroll{overflow:auto}table{border-collapse:collapse;width:100%;font-size:13px;text-align:left}th,td{padding:11px 10px;border-bottom:1px solid #e2eaed;white-space:nowrap}th{background:#f3f7f8}svg{width:100%;min-width:500px;display:block;margin:24px 0;font:13px system-ui;color:#3c5865}pre{font:12px/1.65 ui-monospace,monospace;background:#f2f6f7;max-height:520px;overflow:auto;padding:16px;border-radius:8px}details{margin-top:20px}summary{cursor:pointer;color:#087f79}.satisfied{color:#087f79}.violated{color:#bd453e}.unknown{color:#986b14}.callout{border-left:4px solid #d2a351;padding-left:16px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:26px}li{margin:9px 0}footer{font-size:12px;color:#687f88;margin-top:26px}@media(max-width:700px){main{padding:18px 12px}.grid{grid-template-columns:1fr}header,section{padding:22px}h1{font-size:28px}section{overflow:auto}}@media print{body{background:white}main{padding:0}header{background:white;color:#183446;border:1px solid #ddd}header p{color:#183446}nav{display:none}section{break-inside:avoid}details{display:none}}
</style><main><header><div class="eyebrow">LC CONTROL / P0′—P1′ / READ-ONLY</div><h1>先看清每条支路，<br>再决定是否进入闭环。</h1><p>压力驱动静态水力分析、误差带约束判断、参数可辨识性筛查和留出校核。以下结果由当前代码重新计算，配置与原始结果可下载；没有硬件通信或阀门命令。</p></header>
<nav><a href="#scope">本轮交付</a><a href="#base">共享总管</a><a href="#lower_pressure">压差变化</a><a href="#high_resistance">支路耦合</a><a href="#missing_uncertainty">不确定性</a><a href="#isolated">失败处理</a><a href="#total_only">可辨识性</a><a href="#benchmark">性能基准</a></nav>
<section id="scope"><div class="eyebrow">DELIVERY / BOUNDARIES</div><h2>当前软件能做什么</h2><div class="grid"><div><h3>已实现</h3><ul><li>0.2 配置：资产、水力图、点表引用与控制域分开；保留流体和换热耦合占位。</li><li>根据压差与阻力求每条支路流量；支持静态阀特性、手动平衡阀、止回阀和有特性参数的压力无关阀。</li><li>CLI 与本机工作台接入真实分析函数，保存配置哈希和审计记录。</li><li>保留 0.1 原有执行路径，0.2 的所有设备运行入口拒绝执行。</li></ul></div><div><h3>本轮边界</h3><ul><li>全部数据为合成，不是厂家曲线或现场校核。</li><li>没有动态阀控、在线辨识、跨任务执行账本或资源租约；P2/P3 仍待实施。</li><li>无泵能力包络，不给降额、N+1、节能或芯片热安全结论。</li><li>64 个未知压力、256 元件是首版支持上限，不承诺任意规模。</li></ul></div></div><p class="callout">满足：整个声明误差带都在限值内。违反最小流量：区间上界仍低于最小值。跨限、缺少误差声明或无当前解：unknown。误差区间仅覆盖固定物性/阀位下的声明阻力与边界误差。</p><p>核心场景：1 CDU 二次侧压力边界 → 共享供回总管 → 2 机柜 → 4 节点；8 压力节点、10 等效阻力。机柜流量包含其节点流量，不重复合计。水力分析不计算节点温度。</p><p><a href="../../docs/边缘液冷智控产品审查与使用手册.html#section-8">使用手册</a> · <a href="../../examples/hydraulic_parallel_v02.json">场景模板</a> · <a href="results.json">全部结果 JSON</a></p><pre>python3 -m lc_control analyze examples/hydraulic_parallel_v02.json
python3 tools/build_hydraulic_report.py
python3 -m lc_control serve --port 8765</pre></section>
__CARDS__<section id="benchmark"><div class="eyebrow">MEASURED / SYNTHETIC</div><h2>真实运行的规模基准</h2><p>11 个案例，每个 10 次。每次重建求解器状态，但复用 Python 进程。计时仅包含名义求解与模型编译，不含区间/可辨识性/校核。p95 是本机小样本经验值，不能当作生产实时性保证。</p><p class="reason">__ENV__</p><div class="scroll"><table><thead><tr><th>案例</th><th>元件 / 未知压力</th><th>计时范围</th><th>p95 / 最大 ms</th><th>结果</th></tr></thead><tbody>__TIMING__</tbody></table></div><p><a href="benchmark.json">原始样本、代码哈希、残差与全部环境信息</a></p></section><footer>生成时间 __DATE__ · 标准库软件 · 分析路径无硬件写入 · 估计 ≠ 实测，局部筛查通过 ≠ 现场准入。</footer></main></html>'''
    page = page.replace("__CARDS__", "".join(cards)).replace("__TIMING__", timing)
    page = page.replace("__ENV__", esc(benchmark["environment"]["platform"] + " / " + benchmark["environment"]["python"]))
    page = page.replace("__DATE__", esc(datetime.now(timezone.utc).isoformat()))
    (OUT / "index.html").write_text(page, encoding="utf-8")
    print(OUT / "index.html")
    if not benchmark["all_expectations_met"]:
        raise SystemExit("benchmark_expectation_failed; see raw report")


if __name__ == "__main__":
    main()
