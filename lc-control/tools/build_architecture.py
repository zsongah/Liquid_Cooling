"""生成与当前实现对应的两张工程框架图（SVG + PNG）及离线查看页。

图形由相同坐标/文字同时绘制为矢量和位图，避免中文文字生成错误。
仅图形构建需要 Pillow 和中文字体；不增加控制软件的运行依赖。
0.1 控制与 0.2 只读分析使用独立图带，禁止把估计的支路流量画成可执行控制。
变更工作台、预测接入、Engine / Adapter / 网关行为时，同步更新图中文字并重新生成。
"""
import html
import math
import re
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs/assets"
VERSION = re.search(r'^version\s*=\s*"([^"]+)"',
                    (ROOT / "pyproject.toml").read_text(), re.M).group(1)
# 图页与手册共用核对日期；旧实验图不因重新构建而被标成新版验证结果。
REVIEWED = re.search(r'核对日期：(\d{4}-\d{2}-\d{2})',
                    (ROOT / "docs/边缘液冷智控产品审查与使用手册.md").read_text()).group(1)
INK, MUTED = "#102F43", "#526C7D"
TEAL, BLUE, AMBER = "#087F80", "#2866A4", "#A67120"
LINE, LIGHT, SEA, SKY, SAND = "#CFDFE5", "#F5F8FA", "#EAF7F5", "#EFF5FC", "#FFF6E7"
FONTS = {True: "/System/Library/Fonts/STHeiti Medium.ttc",
         False: "/System/Library/Fonts/STHeiti Light.ttc"}


class Diagram:
    """小型绘图器：文本宽度校验，SVG/PNG 使用同一几何布局。"""
    def __init__(self, width, height, title):
        self.width, self.height = width, height
        self.image = Image.new("RGB", (width, height), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
                    f'height="{height}" viewBox="0 0 {width} {height}" role="img">',
                    '<title>' + html.escape(title) + '</title>',
                    '<rect width="100%" height="100%" fill="white"/>']

    def box(self, x, y, w, h, fill="white", stroke=LINE, radius=16):
        self.draw.rounded_rectangle((x, y, x+w, y+h), radius, fill, stroke, 2)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" '
                        f'rx="{radius}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')

    def text(self, x, y, value, size=26, color=INK, bold=False, maxw=None):
        font = ImageFont.truetype(FONTS[bold], size)
        if maxw is not None and self.draw.textlength(value, font=font) > maxw:
            raise ValueError("文字超出框宽：" + value)
        self.draw.text((x, y), value, font=font, fill=color, anchor="lt")
        self.svg.append(f'<text x="{x}" y="{y}" dominant-baseline="hanging" '
                        f'font-family="Heiti SC, PingFang SC, Noto Sans CJK SC, sans-serif" '
                        f'font-size="{size}" font-weight="{600 if bold else 400}" '
                        f'fill="{color}">{html.escape(value)}</text>')

    def lines(self, x, y, values, size=25, gap=39, color=MUTED, maxw=None):
        for i, value in enumerate(values):
            self.text(x, y+i*gap, value, size, color, maxw=maxw)

    def arrow(self, points, color=TEAL, width=4, double=False):
        self.draw.line(points, fill=color, width=width, joint="curve")
        coords = " ".join(f"{x},{y}" for x, y in points)
        self.svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" '
                        f'stroke-width="{width}" stroke-linejoin="round"/>')
        ends = [(points[-2], points[-1])]
        if double:
            ends.append((points[1], points[0]))
        for a, b in ends:
            angle = math.atan2(b[1]-a[1], b[0]-a[0])
            triangle = [b, (b[0]-14*math.cos(angle)+6*math.sin(angle),
                            b[1]-14*math.sin(angle)-6*math.cos(angle)),
                        (b[0]-14*math.cos(angle)-6*math.sin(angle),
                         b[1]-14*math.sin(angle)+6*math.cos(angle))]
            self.draw.polygon(triangle, fill=color)
            self.svg.append('<polygon points="' + ' '.join(f'{x},{y}' for x,y in triangle)
                            + f'" fill="{color}"/>')

    def card(self, x, y, w, h, title, values, fill="white", accent=TEAL,
             title_size=29, body_size=24):
        self.box(x, y, w, h, fill)
        self.box(x+20, y+23, 5, 28, accent, accent, 2)
        self.text(x+40, y+24, title, title_size, bold=True, maxw=w-60)
        self.lines(x+24, y+77, values, body_size, 37, maxw=w-48)

    def save(self, stem):
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / (stem+'.svg')).write_text('\n'.join(self.svg+['</svg>']), encoding="utf-8")
        self.image.save(OUT / (stem+'.png'))
        print(OUT / (stem+'.png'))


def overview():
    d = Diagram(1800, 1580, "AIDC 液冷智控软件：当前实施框架")
    d.text(55, 38, "AIDC 液冷智控软件", 53, bold=True)
    d.text(58, 108, "当前实施框架  /  配置驱动 · 单 CDU 执行 · 多支路只读水力分析", 27, MUTED)
    d.text(1445, 55, "v"+VERSION+"  /  "+REVIEWED.replace('-', '.'), 22, TEAL)

    d.box(55, 172, 1190, 152, SKY)
    d.text(80, 194, "上游输入", 30, bold=True)
    for x, title, body in [(300, "设备与物理拓扑", "点表 · 回路 · 机柜 / 节点"),
                           (620, "工程运行边界", "温压/流量限值 · 控制归属"),
                           (965, "可选功率预测", "HTTP / 文件 · 时效校验")]:
        d.text(x, 204, title, 28, bold=True)
        d.text(x, 254, body, 23, MUTED, maxw=285)
    d.arrow([(650, 324), (650, 373)])
    d.text(675, 335, "配置 / 需求", 22, TEAL)

    d.box(55, 385, 1190, 433, SEA, "#9CCFC9")
    d.text(81, 412, "Schema 0.1 · 边缘控制核心", 33, bold=True)
    d.text(848, 423, "Web / CLI → Engine", 25, TEAL)
    cards = [
        (80, 478, "配置与工作台", ["草稿 / 发布 / 拓扑界面", "运行趋势 / 决策回放"]),
        (465, 478, "采集与适配", ["Modbus TCP 标准量映射", "温压流 / 数据质量 / 状态"]),
        (850, 478, "需求与控制", ["预测按域聚合 / 前馈", "FlowPolicy 流量 / 压差"]),
        (80, 647, "命令网关", ["ControlService 限幅限速", "审计 / 回读 / 异常暂停"]),
        (465, 647, "有界校准", ["流量—压差增益修正", "合格样本 / 候选验证"]),
        (850, 647, "记录与追溯", ["周期 / 命令 / 模型版本", "预测曲线归档 / 内容哈希"]),
    ]
    for x,y,title,values in cards:
        d.card(x,y,370,150,title,values,body_size=24)
    d.arrow([(470,818),(470,896)])
    d.text(292,843,"流量 / 压差目标",24,TEAL)
    d.arrow([(855,896),(855,818)],BLUE)
    d.text(887,843,"测量 / 目标回读 / 状态",24,BLUE)

    d.box(55,909,1190,157,SKY)
    d.text(82,935,"下游：CDU 本地控制器",32,bold=True)
    d.text(82,991,"泵阀内环 · 设备保护 · 心跳超时接管",25,MUTED)
    d.arrow([(695,986),(767,986)])
    d.text(805,940,"液冷回路与机柜",30,bold=True)
    d.text(805,992,"真实响应由设备与物理回路产生",23,MUTED)

    d.box(1290,172,455,894,SAND,"#E4CFAB")
    d.text(1318,198,"后续扩展",33,bold=True)
    d.text(1318,250,"规划能力 · 尚未实现",25,AMBER)
    future = [
        ("系统总能耗优化",["CDU 与冷源联合目标", "设备组合 / 负荷分配"]),
        ("多 CDU 与环境协调",["共享水路统一调度", "支路阀分配 / 冷源协调"]),
        ("更完整模型与诊断",["机柜 / 芯片热模型", "泄漏预测 / 冷却液健康"]),
        ("接口与产品化运维",["BMS / iCooling · 多协议", "多人权限 / 留存 / 恢复"]),
    ]
    for i,(title,values) in enumerate(future):
        d.card(1313,306+i*180,408,160,title,values,accent=AMBER,title_size=28)

    d.box(55,1104,1690,232,SKY,"#B4CAE3")
    d.text(80,1126,"Schema 0.2 · 只读静态水力分析",30,bold=True)
    d.text(880,1131,"结构合法 ≠ 分析就绪 ≠ 现场可执行",25,BLUE)
    d.card(80,1174,510,140,"拓扑与工程证据",["资产树 / 液路 / 点表引用分离", "明确物性、压力边界与阻力"],accent=BLUE,title_size=27,body_size=23)
    d.card(646,1174,510,140,"静态水力与可信度",["压力驱动求流 / 误差区间", "参数辨识检查 / 留出核对"],accent=BLUE,title_size=27,body_size=23)
    d.card(1212,1174,510,140,"支路需求核对",["估计流量 / 满足、违反或未知", "不创建控制任务 / 不写设备"],accent=BLUE,title_size=27,body_size=23)
    d.arrow([(591,1244),(644,1244)],BLUE)
    d.arrow([(1157,1244),(1210,1244)],BLUE)
    d.box(55,1370,1690,123,LIGHT)
    d.text(80,1391,"开发验证载体与证据范围",28,bold=True)
    d.text(80,1446,"静态水力合成验算",25,TEAL)
    d.text(454,1446,"聚合热模型",25,TEAL)
    d.text(791,1446,"本机 Modbus TCP",25,TEAL)
    d.text(1234,1446,"Sustain-LC FMU 闭环",25,TEAL)
    d.text(80,1524,"0.1 每域控制一台 CDU；0.2 可描述共享域，只做分析。尚无实机验收或全站节能证明。",25,MUTED)
    d.save("aidc-implementation-overview")


def detail():
    d = Diagram(2440, 1980, "AIDC 液冷智控软件：控制通信与只读分析链路")
    d.text(55,40,"从工作台到 CDU：配置、预测与控制如何衔接",53,bold=True)
    d.text(58,112,"Schema 0.1：受控设备通信；Schema 0.2：独立只读分析，不进入设备执行链",28,MUTED)
    d.text(2080,58,"v"+VERSION+"  /  "+REVIEWED.replace('-', '.'),22,TEAL)

    d.box(55,180,1820,1037,LIGHT)
    d.text(80,202,"边缘计算机 / Schema 0.1 控制路径",31,bold=True)
    d.text(1115,210,"绿色：候选与下发     蓝色：采集与回读",25,MUTED)

    d.box(85,263,1760,104,"white")
    d.text(106,283,"启动与输入",25,bold=True)
    d.text(300,283,"工作台 → 同源 HTTP → Workbench：发布配置 / 独立域任务 → Engine",27,TEAL)
    d.text(300,326,"预测 API：Workbench 保存原曲线；forecasting 按域聚合 / 校验 → 每周期前馈；CLI 可独立启动",24,MUTED)

    # 读路径单独走顶端，避免与控制方向混在同一箭头上。
    d.arrow([(1470,470),(1470,408),(230,408),(230,470)],BLUE)
    d.text(290,377,"① Snapshot：温度 / 流量 / 实际压差 / 时间与质量 / 设备状态 / 目标回读",25,BLUE)

    d.card(85,470,320,307,"Engine.tick()",[
        "一个采集与决策周期",
        "read → 保存采样",
        "control 模式检查心跳",
        "预测物化 / 校准 / 决策",
        "submit → 保存决策",
        "异常处理 / 退出释放"], title_size=29,body_size=24)
    d.card(480,470,320,307,"FlowPolicy",[
        "② 热负荷 + 按域预测",
        "计算需求流量",
        "输出流量或压差目标",
        "增益校准 / 温度风险",
        "不直接操作寄存器",
        "当前自动策略不调供温"],title_size=29,body_size=23)
    d.card(875,470,350,307,"ControlService",[
        "③ 校验 ControlRequest",
        "重新读取设备状态",
        "归属 / 模式 / 期限",
        "边界 / 幅度 / 间隔",
        "先落审计 → 再下发",
        "回读 → 形成回执"],title_size=29,body_size=24)
    d.card(1300,470,340,307,"ModbusCDUAdapter",[
        "④ 标准量 ↔ OEM 点位",
        "地址 / 类型 / 单位",
        "编码与解码 / 状态枚举",
        "ModbusTCPClient",
        "读取 FC01/02/03/04",
        "写寄存器 FC06/16"],title_size=27,body_size=24)

    for x1,x2 in [(405,480),(800,875),(1225,1300)]:
        d.arrow([(x1,544),(x2,544)])
    d.arrow([(1300,714),(1225,714)],BLUE)

    d.box(1700,470,145,307,SKY)
    d.text(1720,497,"网口",32,bold=True)
    d.lines(1716,567,["操作系统","TCP/IP","网卡 / 网口"],23,48,maxw=123)
    d.text(1716,726,"传输载体",22,BLUE)

    d.arrow([(1640,544),(1700,544)])
    d.arrow([(1700,714),(1640,714)],BLUE)
    d.arrow([(1845,544),(1950,544)])
    d.arrow([(1950,714),(1845,714)],BLUE)

    d.box(1950,263,435,514,SKY,"#B4CAE3")
    d.text(1976,290,"CDU 本地控制器",34,bold=True)
    d.text(1976,345,"OEM PLC / DDC",26,BLUE)
    d.lines(1976,404,["通信端口 / 寄存器点表",
                        "接收已开放的流量或压差目标",
                        "实际测量 / 状态 / 目标回读"],25,44,maxw=383)
    d.box(1973,567,389,183,"white","#B4CAE3")
    d.text(1994,590,"设备本地职责",29,bold=True)
    d.lines(1994,641,["泵阀快速内环 / 设备联锁",
                        "独立看门狗 / 失联接管"],25,43,maxw=347)

    d.text(1646,817,"⑤ TCP 报文 / 以太网",24,MUTED)
    d.text(1646,855,"Modbus TCP",22,MUTED)
    d.text(1646,890,"IP / port / Unit ID",22,MUTED)

    # 网关回执返还 Engine；目标读取由 Adapter 到网关的反向蓝箭头表示。
    d.arrow([(1050,777),(1050,866),(240,866),(240,777)],BLUE)
    d.text(365,828,"⑥ ControlReceipt 返回 Engine，保存决策与结果",24,BLUE)
    d.text(103,909,"箭头表示数据流，调用由 Engine 编排；heartbeat/submit 会复查状态，写后另有回读。",24,MUTED)

    d.arrow([(2098,777),(2098,857)])
    d.arrow([(2264,857),(2264,777)],BLUE)
    d.box(1950,869,435,180,"white","#B4CAE3")
    d.text(1976,896,"泵 / 阀 / 液冷回路 / 机柜",27,bold=True)
    d.lines(1976,951,["执行目标后产生真实物理响应",
                       "温度、流量、压差传回控制器"],24,39,maxw=383)
    d.text(1954,1080,"目标回读确认 ≠ 实际运行达标",26,BLUE)
    d.lines(1954,1125,["还需检查实际过程响应；",
                       "保护与失联接管须逐型号验收。"],24,38,maxw=428)

    d.text(86,968,"持久化与模式边界",27,bold=True)
    d.card(85,1018,535,165,"runtime.sqlite",[
        "Engine：采样 / 校准 / 模型 / 决策 / 故障",
        "API → 工作台趋势 / 决策回放"],title_size=27,body_size=23)
    d.card(647,1018,590,165,"commands.jsonl",[
        "网关：dispatch_intent 先落盘同步，再写设备",
        "随后记录回执；结果不确定时不自动重发"],title_size=27,body_size=23)
    d.card(1264,1018,580,165,"monitor / shadow / control",[
        "监测 / 只生成候选 / 满足条件才执行",
        "异常 → 暂停优化 → 尝试释放到本地"],title_size=27,body_size=23)

    d.box(55,1255,2330,266,SEA,"#9CCFC9")
    d.text(82,1280,"无硬件时：替换下游载体，复用控制核心",32,bold=True)
    d.text(1240,1287,"专用入口 tools/run_sustain_fmu.py · 不经过 Modbus 网口",26,TEAL)
    stages=[(85,460,"Engine + FlowPolicy","FmuLaboratoryGate 实验网关"),
            (620,415,"SustainFmuAdapter","标准观测 / kPa ↔ psi"),
            (1110,430,"SustainFmuMaster","独占实例 / 时钟 / 扰动"),
            (1615,735,"PyFMI / FMI 2.0 → Sustain-LC FMU","五组共享回路；当前算法只调第 1 组压差")]
    for x,w,title,body in stages:
        d.box(x,1347,w,99,"white")
        d.text(x+19,1361,title,27,bold=True,maxw=w-38)
        d.text(x+19,1405,body,23,MUTED,maxw=w-38)
    for x1,x2 in [(545,620),(1035,1110),(1540,1615)]:
        d.arrow([(x1,1397),(x2,1397)],double=True)
    d.text(86,1471,"实验通信步长 15 s / 控制周期 60 s；固定邻机与冷源边界。仿真网关许可不能用于现场共享水路控制。",25,MUTED)
    d.box(55,1560,2330,310,SKY,"#B4CAE3")
    d.text(82,1585,"Schema 0.2：独立的只读工程分析路径",32,bold=True)
    d.text(86,1638,"高级 JSON 草稿 → 校验 / 发布 → CLI analyze 或工作台只读分析；共享域可描述，现场执行仍被门控阻止",25,MUTED)
    analysis_stages = [
        (85,700,"analysis_config · 工程描述",["资产树 / fluid_circuits / junctions / elements", "points 引用设备点表；不复制寄存器配置"]),
        (860,700,"hydraulics + hydraulic_evidence",["静态压力与流量 / 保守误差带", "可辨识性与独立数据核对：证据不足即未知"]),
        (1635,715,"工作台 / analysis_report",["机柜与节点支路流量估计 / 需求状态", "数值收敛、需求满足、模型证据分别展示"]),
    ]
    for x,w,title,values in analysis_stages:
        d.card(x,1684,w,140,title,values,accent=BLUE,title_size=28,body_size=24)
    d.arrow([(785,1750),(858,1750)],BLUE)
    d.arrow([(1560,1750),(1633,1750)],BLUE)
    d.text(86,1839,"该链路不创建 Adapter、不调用 ControlService、不发送设备命令；估计值不是实测支路遥测。",25,BLUE)
    d.text(61,1922,"图示对应当前实现。0.2 分析没有支路闭环或冷源联控；历史 FMU 实验仍按其原版本与固定边界解读。",27,MUTED)
    d.save("aidc-control-dataflow")


def gallery():
    page = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AIDC 液冷智控 · 实施框架与通信链路</title><style>
*{box-sizing:border-box}body{margin:0;background:#f2f6f7;color:#102f43;
font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
header,section{max-width:1580px;margin:26px auto;padding:24px 30px;background:white;border-radius:12px}
h1{font-size:30px;margin:0}h2{font-size:22px;margin:0 0 8px}p{color:#526c7d;margin:8px 0}
a{color:#087f80;text-underline-offset:4px}nav{display:flex;gap:25px;flex-wrap:wrap}
img{display:block;width:100%;height:auto;margin-top:20px;border:1px solid #cfdfE5;border-radius:6px}
.links{display:flex;gap:24px}footer{font-size:13px;color:#526c7d} @media print{header,.links{display:none}
section{page-break-after:always;margin:0;padding:0}img{border:0} @page{size:A3 landscape;margin:10mm}}
</style></head><body><header><h1>AIDC 液冷智控：当前实施框架与通信链路</h1>
<p>软件 v__VERSION__ · 核对日期 __REVIEWED__。图示覆盖本机工作台、Schema 0.1 控制路径与新增 Schema 0.2 只读水力分析。</p>
<p>Schema 0.1 由 Engine 调用设备适配器与命令网关，支持独立 CDU 域和功率预测前馈。
Schema 0.2 按显式液路、物性、压力和阻力估计支路流量，检查误差范围、需求与模型证据，不创建适配器或发送设备命令。
共享水路调度、支路阀闭环分配和全站能耗优化仍未实现。</p>
<p>预测接入与使用契约见完整手册第 8.4 节；当前实现边界见第 13.1 节。Sustain FMU 新实验仍走专用 Master，不由工作台启动。</p>
<nav><a href="#overview">图 1 · 整体框架</a><a href="#detail">图 2 · 通信与代码</a>
<a href="边缘液冷智控产品审查与使用手册.html">完整使用手册</a>
<a href="../outputs/hydraulic-analysis/index.html">只读水力分析报告</a></nav></header>
__SECTIONS__</body></html>'''
    sections=[]
    for key,stem,title,note in [
        ("overview","aidc-implementation-overview","图 1 · 产品当前实施框架",
         "区分上游输入、0.1 边缘控制、CDU 本地职责、0.2 只读水力分析与后续扩展。"),
        ("detail","aidc-control-dataflow","图 2 · 工作台、Engine、Adapter、网口与 CDU 的信息链路",
         "上半部分是 0.1 控制与通信，蓝色回路表示采集和回读；底部独立蓝色带是 0.2 只读分析。FMU 使用专用实验入口。")]:
        sections.append(f'<section id="{key}"><h2>{title}</h2><p>{note}</p>'
                        f'<div class="links"><a href="assets/{stem}.png" download>下载 PNG 图片</a>'
                        f'<a href="assets/{stem}.svg" target="_blank">打开 SVG 矢量图 / 放大</a></div>'
                        f'<a href="assets/{stem}.svg" target="_blank">'
                        f'<img src="assets/{stem}.png" alt="{title}"></a></section>')
    (ROOT/'docs/实施框架与通信链路.html').write_text(
        page.replace('__VERSION__',VERSION).replace('__REVIEWED__',REVIEWED)
            .replace('__SECTIONS__',''.join(sections)),encoding='utf-8')


if __name__ == "__main__":
    overview()
    detail()
    gallery()
