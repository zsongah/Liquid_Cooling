"""把已存储的运行会话转成离线 HTML 报告。

页面不具有实时数据链路或设备写入能力。所有动态文本先 HTML 转义。
采样、设定值确认、命令拒绝、参数晋升和软件异常分开统计。
曲线来自同目录 SQLite，比较来自 comparison.json，均保留来源和边界。"""
import html
import json
from pathlib import Path
from .storage import Store


def render_report(directory):
    """读取输出目录最近会话生成 report.html。只展示已保存数据，不触发通信和控制。"""
    directory = Path(directory)
    store = Store(directory / "runtime.sqlite")
    session = store.latest("session")
    def rows(kind):
        return [r for r in store.rows(kind) if r.get("session") == session["session"]]
    telemetry, decisions, calibrations, faults = [rows(k) for k in ("telemetry", "decision", "calibration", "fault")]
    store.close()
    def escape(x):
        return html.escape(str(x))
    def chart(name, unit, offset=0):
        series = [(r["at"], r["observations"][name]["value"] - offset)
                  for r in telemetry if name in r["observations"]]
        if not series:
            return '<p class="muted">未配置此测点</p>'
        xs, ys = zip(*series)
        low, high = min(ys), max(ys)
        span = max(high - low, 0.1)
        points = " ".join('%.1f,%.1f' % (20 + 640 * (x - min(xs)) / max(max(xs) - min(xs), 1),
                                       130 - 100 * (y - low) / span) for x, y in series)
        return '<div class="muted">%.2f → %.2f %s · 范围 %.2f–%.2f</div><svg viewBox="0 0 680 150" role="img" aria-label="%s"><path d="M20 130 H660" stroke="#cbd5e1"/><polyline points="%s" fill="none" stroke="#087f8c" stroke-width="2"/></svg>' % (ys[0], ys[-1], escape(unit), low, high, escape(name), points)
    cards = ''.join('<section><h2>%s</h2>%s</section>' % (title, chart(name, unit, offset)) for title, name, unit, offset in (
        ('二次侧供液温度', 'cdu.sec_supply_temp', '°C', 273.15),
        ('二次侧回液温度', 'cdu.sec_return_temp', '°C', 273.15),
        ('二次侧质量流量', 'cdu.sec_flow', 'kg/s', 0),
        ('二次侧压差', 'cdu.sec_dp', 'kPa', 0),
        ('液冷热负荷', 'cdu.liquid_load', 'W_th', 0),
        ('CDU 电功率', 'cdu.electric_power', 'W_e', 0)))
    promotions = sum(c["status"] == "promoted" for c in calibrations)
    confirmed = sum(bool(d.get("receipt")) and d["receipt"]["status"] == "setpoint_confirmed" for d in decisions)
    rejected = sum(bool(d.get("receipt")) and d["receipt"]["status"] == "rejected" for d in decisions)
    details = {"最后控制决策": decisions[-1] if decisions else None,
               "最近校准记录": calibrations[-5:], "故障记录": faults[-10:],
               "运行信息": {k: v for k, v in session.items() if k != "scene"}}
    summary_path = directory / "comparison.json"
    comparison = json.loads(summary_path.read_text()) if summary_path.exists() else None
    compare_section = '<section><h2>同负荷仿真比较</h2><pre>%s</pre></section>' % escape(json.dumps(comparison, ensure_ascii=False, indent=2)) if comparison else ''
    source_note = ('数据来自 Sustain-LC FMU 的新闭环实验；邻机和冷源目标固定，不能视为现场验证'
                   if any(p.get('adapter') == 'sustain_fmu' for p in session['scene']['devices'].values())
                   else '数据来自合成热工仿真，仅用于软件开发验证' if session['deployment'] == 'simulation'
                   else '数据通过 Modbus TCP 采集；来源可能为协议模拟器或现场设备，请核对运行配置')
    doc = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>液冷控制运行报告</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f6f8;color:#182c3a;font:16px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}main{max-width:1180px;margin:auto;padding:40px 24px}h1{font-size:34px;margin:10px 0}h2{font-size:18px;margin:0 0 12px}section{background:white;border:1px solid #dde6eb;border-radius:14px;padding:22px;margin-bottom:18px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}.grid section{margin:0}.muted{color:#607685;font-size:14px}.badge{color:#06747d;font-weight:700}.notice{border-left:4px solid #c48932;padding:14px 20px;background:#fff8ea;margin:22px 0}pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}svg{width:100%%;display:block}.stats{display:flex;gap:28px;flex-wrap:wrap;margin:24px 0}.stats b{font-size:27px;display:block}details{margin-top:22px}summary{cursor:pointer}@media(max-width:700px){.grid{grid-template-columns:1fr}main{padding:24px 16px}h1{font-size:27px}}</style><main>
<div class="badge">LC CONTROL · 运行记录</div><h1>液冷控制闭环报告</h1><p>采集 → 状态检查 → 热需求计算 → 有界控制 → 设定值回读 → 数据校准</p>
<div class="notice">%s。设定值确认不代表热安全达标；本报告未验证机柜或芯片温度，也未计入一次侧和机房空调能耗。</div>
<div class="stats"><div><b>%s</b>采样记录</div><div><b>%s</b>设定值确认</div><div><b>%s</b>参数更新</div><div><b>%s</b>软件故障事件</div><div><b>%s</b>命令拒绝</div></div>
<div class="grid">%s</div>%s<details><summary>查看决策、校准和异常详情</summary><pre>%s</pre></details><p class="muted">这是运行结束后生成的离线报告，不是实时控制面板。完整数据保存在同目录 runtime.sqlite，命令审计保存在 commands.jsonl。</p></main></html>''' % (
        source_note,
        len(telemetry), confirmed, promotions, len(faults), rejected, cards, compare_section,
        escape(json.dumps(details, ensure_ascii=False, indent=2)))
    path = directory / "report.html"
    path.write_text(doc, encoding="utf-8")
    return path
