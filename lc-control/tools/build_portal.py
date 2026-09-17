"""生成离线文档入口；正文统一在手册，避免复制功能和指标后版本漂移。

此工具只写 index.html，不启动控制器、不查询设备、不重跑实验。
实验链接是明确选定的已保存基准；更换基准时同步修改手册第 10 节。
outputs/ 不入库，因此只对本地实际存在的产物生成链接，缺失项标注“未随仓库发布”，
避免克隆后出现指向不存在文件的死链。
"""
import html
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
MANUAL = "docs/" + quote("边缘液冷智控产品审查与使用手册.html")


def _evidence_item(title, url):
    """产物存在才生成链接；缺失时降级为带说明的纯文本，克隆后不产生死链。"""
    if (ROOT / url).is_file():
        return '<li><a href="' + html.escape(url, quote=True) + '">' + html.escape(title) + '</a></li>'
    return ('<li><span class="missing">' + html.escape(title) +
            '</span><small>未随仓库发布 · 需本地生成</small></li>')


def main():
    version = re.search(r'^version\s*=\s*"([^"]+)"',
                        (ROOT / "pyproject.toml").read_text(), re.M).group(1)
    chapters = [
        ("01", "产品与框架", "定位、现有能力、上下游与扩展边界", 2),
        ("02", "输入与算法", "设备拓扑、点表、控制、校准与功率预测", 3),
        ("03", "设备与部署", "厂家接口依据、现场联调与边缘运行", 7),
        ("04", "FMU 与验证", "实际闭环步骤、单位、结果与模型限制", 9),
        ("05", "代码与恢复", "逐文件职责、数据、命令审计与故障处置", 11),
        ("06", "后续开发", "真实设备验收、运维平台与系统能效优化", 13),
    ]
    cards = "".join(
        '<a class="card" href="' + MANUAL + '#section-' + str(section) + '">'
        '<small>' + number + '</small><h2>' + title + '</h2><p>' + detail + '</p></a>'
        for number, title, detail, section in chapters)
    results = [
        ("v0.7.0 · 多支路只读水力分析与证据报告", "outputs/hydraulic-analysis/index.html"),
        ("向上级汇报 · CDU 场景、控制链与选定结果", "outputs/cdu-briefing-20260916/index.html"),
        ("FMU 统一验证报告 · 29 组实验与核心配置", "outputs/fmu-validation-20260915/index.html"),
        ("合成液对液仿真", "outputs/no-hardware-l2l-20260914/report.html"),
        ("合成液对气仿真", "outputs/no-hardware-l2a-20260914/report.html"),
        ("本机 TCP 通信报告", "outputs/wire-demo/report.html"),
        ("历史收尾回归 · v0.6.0 / 2026-09-17", "outputs/test-results-release-closure-20260917.txt"),
        ("前端交互回归 · JSON 编辑与历史记录隔离", "outputs/test-results-frontend-closure-20260917.txt"),
        ("预测、拓扑与控制核心回归 · 2026-09-16", "outputs/test-results-console-v060-20260916.txt"),
        ("历史界面与后台闭环验收 · v0.6.0", "outputs/console-v060-acceptance-20260916/verification.json"),
        ("历史工作台回归测试 · v0.5.0", "outputs/test-results-workbench-20260916.txt"),
        ("历史 FMU 跟踪修正测试 · v0.4.1", "outputs/test-results-tracking-correction.txt"),
    ]
    evidence = "".join(_evidence_item(title, url) for title, url in results)
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LC Control · 产品与使用入口</title><style>
:root{color-scheme:light;--ink:#173940;--muted:#597078;--teal:#087d7e;--line:#d7e5e5}
*{box-sizing:border-box}body{margin:0;background:#f3f7f6;color:var(--ink);
font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1120px;margin:auto;padding:54px 28px}header{margin-bottom:32px}
.brand{color:var(--teal);font-size:12px;letter-spacing:2px}h1{font-size:42px;
line-height:1.35;letter-spacing:-1px;margin:18px 0}header p{max-width:770px;color:var(--muted)}
a{color:var(--teal);text-underline-offset:4px}.primary{display:inline-block;background:var(--teal);
color:white;padding:10px 22px;border-radius:7px;text-decoration:none;margin:14px 16px 0 0}
.grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{display:block;
background:white;border:1px solid var(--line);border-radius:10px;padding:23px;text-decoration:none;
color:var(--ink)}.card:hover{border-color:var(--teal);background:#fcfffe}.card small{color:var(--teal)}
h2{font-size:21px;line-height:1.5;margin:8px 0}.card p{font-size:14px;color:var(--muted);margin:8px 0 0}
section{background:white;border:1px solid var(--line);padding:24px;border-radius:10px;margin-top:24px}
section p,footer{font-size:13px;color:var(--muted)}ul{padding-left:22px;columns:2;column-gap:36px}
li{padding:6px 0;break-inside:avoid}.missing{color:var(--muted)}li small{display:block;color:#8a9ea4;font-size:12px}footer{margin-top:24px}a:focus-visible{outline:3px solid #c8993c;
outline-offset:4px}@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}
main{padding:32px 20px}h1{font-size:34px}}@media(max-width:520px){.grid{grid-template-columns:1fr}
ul{columns:1}.card{padding:18px}h1{font-size:30px}}
</style></head><body><main><header><div class="brand">LC CONTROL / v__VERSION__</div>
<h1>AIDC 液冷智控软件</h1><p>一份手册，了解产品、配置、算法、设备接入与开发验证。
Schema 0.1 运行单 CDU 控制；Schema 0.2 新增多支路只读水力分析。
完整内容集中维护，以下入口直接定位对应章节。</p>
<a class="primary" href="__MANUAL__">打开完整使用手册</a>
<a class="primary" href="docs/实施框架与通信链路.html">查看实施框架双图</a><a href="README.md">快速启动</a>
</header><section style="margin-bottom:24px"><h2>边缘工作台 · 实际交互界面</h2>
<p>在 lc-control 目录运行 <code>python3 -m lc_control serve</code>，然后打开本机工作台。
可查看配置、读取已保存运行记录、编辑并发布配置。Schema 0.1 可启动合成仿真；
Schema 0.2 可运行压力与阻力驱动的静态分析，输出估计流量与需求核对，不写设备。
本页是离线文档入口；工作台连接后台服务后才显示运行数据。</p>
<a class="primary" href="http://127.0.0.1:8765">打开本机工作台</a>
<p>只读分析从 <code>hydraulic_parallel_v02</code> 模板开始；原控制与仿真使用
<code>workbench_physical</code> 或 <code>workbench_independent_cdus</code>。
FMU 历史记录可查询，新实验仍由专用 Master 启动。</p>
</section><div class="grid">__CARDS__</div><section><h2>已保存的验证报告</h2>
<p>以下均为开发实验。结果口径、已知限制及现场验收缺口见手册第 10 节；页面不显示设备实时状态。为保持仓库轻量，<code>outputs/</code> 不入库：本地产物存在时才显示链接，否则标注“未随仓库发布 · 需本地生成”。</p>
<ul>__EVIDENCE__</ul></section><footer>软件版本来自 pyproject.toml。
手册 Markdown 是完整说明的唯一来源，HTML 与此入口由构建工具生成。</footer></main></body></html>'''
    for key, value in {"__VERSION__": html.escape(version), "__MANUAL__": MANUAL,
                       "__CARDS__": cards, "__EVIDENCE__": evidence}.items():
        page = page.replace(key, value)
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(ROOT / "index.html")


if __name__ == "__main__":
    main()
