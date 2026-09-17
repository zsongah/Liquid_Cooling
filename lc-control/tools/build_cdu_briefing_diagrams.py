"""绘制参考 CDU 的功能物理图和实际/现场两条信息链。非施工 P&ID。"""
from pathlib import Path
import math
import build_architecture as a

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/cdu-briefing-20260916'
BLUE='#277da8';HOT='#c97651';CTRL='#725aa5';INK=a.INK


def circle(d,x,y,r,color='white',stroke=BLUE):
    d.draw.ellipse((x-r,y-r,x+r,y+r),fill=color,outline=stroke,width=3)
    d.svg.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}" stroke="{stroke}" stroke-width="3"/>')


def line(d,points,color,width=4):
    d.draw.line(points,fill=color,width=width,joint='curve')
    d.svg.append('<polyline points="'+' '.join(f'{x},{y}' for x,y in points)+f'" fill="none" stroke="{color}" stroke-width="{width}"/>')


def physical():
    d=a.Diagram(1800,1090,'参考液对液 CDU 的物理功能边界与测量位置')
    d.text(55,35,'物理场景：共享设施水，单组 CDU 监督控制',43,bold=True)
    d.text(58,100,'功能示意 / 基于 FMU 的 HEX、CDUP、阀门及三条冷却通道；不是厂家施工图',24,a.MUTED)
    d.box(45,175,410,695,a.LIGHT)
    d.text(75,200,'共享冷源与设施水侧',30,bold=True)
    d.box(90,265,150,155,'white');d.text(108,290,'冷却塔',25,bold=True)
    circle(d,165,365,28,stroke=a.MUTED)
    for angle in [0,2.094,4.188]:
        line(d,[(165,365),(165+22*math.cos(angle),365+22*math.sin(angle))],a.MUTED,5)
    d.box(265,265,145,155,'white');d.lines(283,292,['泵组 /','设施水环路'],23,38,maxw=120)
    d.lines(75,720,['边界固定：湿球 20°C','塔目标 25.56°C','其他四组 CDU 保持固定目标'],23,43,maxw=355)
    d.box(595,175,455,695,a.SEA,'#9bcfc7')
    d.text(625,200,'受控组：CDU 01',31,bold=True)
    d.box(705,330,230,235,'white',BLUE)
    d.text(735,345,'换热器 HEX',25,bold=True)
    for x in range(735,903,24):line(d,[(x,408),(x+18,510)],a.MUTED,3)
    d.text(724,525,'两侧液体隔离换热',20,a.MUTED)
    d.arrow([(410,455),(705,455)],BLUE,7)
    d.text(475,418,'设施供水',22,BLUE)
    d.arrow([(705,535),(550,535),(550,610),(410,610)],HOT,7)
    d.text(470,625,'设施回水',22,HOT)
    d.box(1260,175,495,695,a.SKY)
    d.text(1290,200,'机柜聚合模型 / RACK_01',29,bold=True)
    d.text(1290,248,'3 条液冷支路；不代表 3 台真实服务器',21,a.MUTED,maxw=440)
    # 二次侧冷液向右，热液向左；泵作为 CDU 内部功能组件画出，不断言 OEM 管序。
    d.arrow([(935,385),(1310,385)],BLUE,7)
    d.arrow([(1700,680),(1120,680),(1120,555),(935,555)],HOT,7)
    d.text(1080,337,'二次供液',23,BLUE)
    d.text(1120,762,'二次回液',23,HOT)
    for x,symbol,desc in [(1000,'T','Ts'),(1090,'P','Ps'),(1180,'F','m')]:
        circle(d,x,385,23);d.text(x-9,370,symbol,23,BLUE,bold=True);d.text(x-18,423,desc,20,a.MUTED)
    for x,symbol,desc in [(1200,'T','Tr'),(1120,'P','Pr')]:
        circle(d,x,680,23,stroke=HOT);d.text(x-9,665,symbol,23,HOT,bold=True);d.text(x-18,727,desc,20,a.MUTED)
    # 供回集管及三个支路，阀门均固定，热源输入是实验扰动。
    line(d,[(1310,385),(1310,590)],BLUE,5);line(d,[(1700,410),(1700,680)],HOT,5)
    for i,y in enumerate([410,500,590]):
        d.arrow([(1310,y),(1380,y)],BLUE,4)
        d.box(1380,y-26,78,52,'white',BLUE,5);d.text(1397,y-13,'阀',23,BLUE)
        d.box(1510,y-34,155,68,'white',HOT,7);d.text(1525,y-23,'通道 '+str(i+1),23,bold=True)
        d.text(1520,y+6,'热负荷输入',17,HOT)
        line(d,[(1458,y),(1510,y)],BLUE,4);d.arrow([(1665,y),(1700,y)],HOT,4)
    d.text(1315,785,'阀门固定：0.33 / 0.33 / 0.34',22,a.MUTED)
    d.box(635,630,165,150,'white',CTRL)
    circle(d,718,678,27,stroke=CTRL);d.arrow([(706,690),(733,678),(706,665)],CTRL,3)
    d.text(658,727,'循环泵 CDUP',23,bold=True)
    d.box(835,630,170,150,'white',CTRL);d.lines(853,658,['本地 PID','＋二阶滤波'],22,36,maxw=140)
    d.arrow([(835,690),(800,690)],CTRL,3)
    d.text(647,802,'软件下发压差目标，本地控制产生泵驱动量',18,CTRL,maxw=380)
    d.box(45,905,1710,135,'#f8fafb')
    d.lines(75,928,['测量：Ts / Tr 温度、m 质量流量、Ps−Pr 压差、CDU 泵电功率；功率计为模型汇总量，未画成独立实物仪表。',
                  '图中省略设施侧内部阀门/管阻及辅件；泵位置、仪表数量与回路布置是功能表达，需按真实 CDU 图纸逐项确认。'],23,45,maxw=1650)
    d.save('physical_components')


def information():
    d=a.Diagram(1800,1240,'已验证 FMU 信息链与未来真实 CDU 网络连接')
    d.text(55,35,'信息流：读实际量 → 算需求 → 下发压差 → 看实际响应',39,bold=True)
    d.text(58,103,'上排是本次实际验证路径；下排是已实现驱动面向现场的接入方式，尚无厂家实机验收',23,a.MUTED)
    xs=[55,485,915,1345];w=390
    for x,title,lines in [(xs[0],'① FMU 过程模型',['Ts / Tr / m / Δp / 泵功率','压差输入回读','Master 每 15 s 推进模型']),
                         (xs[1],'② SustainFmuAdapter',['K / kg/s / kPa / W_e','封装 Snapshot 与时间戳','模式/owner 是实验调度状态']),
                         (xs[2],'③ Engine + FlowPolicy',['每 60 s 采集和决策','热需求 → 流量 → 压差','模型校准检查 / 状态记录']),
                         (xs[3],'④ 安全命令网关',['归属、单位、有效期、边界','先记写意图，再执行动作','回读确认；不等同实际跟上'])]:
        d.card(x,195,w,255,title,lines,a.SEA,title_size=27,body_size=23)
    for x in xs[:-1]:d.arrow([(x+w,323),(x+430,323)],a.TEAL,4)
    d.arrow([(1540,450),(1540,525),(1090,525)],CTRL,4)
    d.text(1170,475,'ControlRequest: cdu.dp_sp',22,CTRL)
    d.box(610,485,480,115,'white',CTRL)
    d.text(635,505,'Adapter.write → Master.write_dp',24,CTRL,bold=True)
    d.text(635,552,'kPa → psi → model.set(第 1 组 dp 输入)',22,a.MUTED)
    d.arrow([(610,540),(250,540),(250,450)],CTRL,4)
    d.text(80,570,'下一通信步改变过程响应',21,CTRL)
    d.text(80,625,'内部执行：压差误差 → 本地 PID / 滤波 → 泵驱动 → 流量与温度响应',25,bold=True)
    d.box(55,700,1680,235,a.LIGHT)
    d.text(80,725,'真实 CDU 接入方式（本次实验未走这条网口链路）',28,bold=True)
    items=[(80,'边缘计算机','Engine / FlowPolicy'),(495,'ModbusCDUAdapter','点表 / 编码 / 单位换算'),(910,'以太网 / 工业网络','Modbus TCP 请求与响应'),(1325,'CDU 本地 PLC/DDC','寄存器 / 本地控制 / 保护')]
    for x,title,detail in items:
        d.box(x,790,365,103,'white');d.text(x+16,808,title,24,bold=True,maxw=335);d.text(x+16,855,detail,20,a.MUTED,maxw=335)
    for x,_,_ in items[:-1]:d.arrow([(x+365,842),(x+415,842)],BLUE,3,double=True)
    d.box(55,978,1680,200,'#fff6e8')
    d.text(80,1000,'必须区分的三个事实',28,bold=True)
    d.lines(85,1047,['• 本次闭环使用 PyFMI / FMI 2.0 Co-Simulation；没有 TCP 报文，也没有真实 OEM 告警或失联看门狗。',
                   '• 实机驱动已具备读 FC01/02/03/04、写 FC06/16；型号、点表、读写权限与交接语义仍需现场绑定。',
                   '• SQLite 保存观测、决策、校准、模型与故障；命令日志保留写意图和回执，供审计与效果复核。'],23,39,maxw=1610)
    d.save('information_flow')


if __name__=='__main__':
    OUT.mkdir(parents=True,exist_ok=True);a.OUT=OUT
    physical();information()
