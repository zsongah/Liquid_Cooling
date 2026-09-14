"""生成离线文档入口；正文统一在手册，避免复制功能和指标后版本漂移。

此工具只写 index.html，不启动控制器、不查询设备、不重跑实验。
实验链接是明确选定的已保存基准；更换基准时同步修改手册第 10 节。
"""
import html
import re
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
MANUAL = "docs/" + quote("边缘液冷智控产品审查与使用手册.html")


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
        ("实际 FMU 三策略闭环", "outputs/sustain-fmu-closed-loop-v2/index.html"),
        ("合成液对液仿真", "outputs/no-hardware-l2l-20260914/report.html"),
        ("合成液对气仿真", "outputs/no-hardware-l2a-20260914/report.html"),
        ("本机 TCP 通信报告", "outputs/wire-demo/report.html"),
        ("回归测试记录", "outputs/test-results-fmu.txt"),
    ]
    evidence = "".join('<li><a href="' + url + '">' + title + '</a></li>'
                       for title, url in results)
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
li{padding:6px 0;break-inside:avoid}footer{margin-top:24px}a:focus-visible{outline:3px solid #c8993c;
outline-offset:4px}@media(max-width:800px){.grid{grid-template-columns:repeat(2,1fr)}
main{padding:32px 20px}h1{font-size:34px}}@media(max-width:520px){.grid{grid-template-columns:1fr}
ul{columns:1}.card{padding:18px}h1{font-size:30px}}
</style></head><body><main><header><div class="brand">LC CONTROL / v__VERSION__</div>
<h1>AIDC 液冷智控软件</h1><p>一份手册，了解产品、配置、算法、设备接入与开发验证。
完整内容集中维护，以下入口直接定位对应章节。</p>
<a class="primary" href="__MANUAL__">打开完整使用手册</a><a href="README.md">快速启动</a>
</header><div class="grid">__CARDS__</div><section><h2>已保存的验证报告</h2>
<p>以下均为开发实验。结果口径、已知限制及现场验收缺口见手册第 10 节；页面不显示设备实时状态。</p>
<ul>__EVIDENCE__</ul></section><footer>软件版本来自 pyproject.toml。
手册 Markdown 是完整说明的唯一来源，HTML 与此入口由构建工具生成。</footer></main></body></html>'''
    for key, value in {"__VERSION__": html.escape(version), "__MANUAL__": MANUAL,
                       "__CARDS__": cards, "__EVIDENCE__": evidence}.items():
        page = page.replace(key, value)
    (ROOT / "index.html").write_text(page, encoding="utf-8")
    print(ROOT / "index.html")


if __name__ == "__main__":
    main()
