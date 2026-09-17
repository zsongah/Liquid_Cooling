"""基于已保存的同条件证据生成汇报版单文件 HTML；不重跑/择删实验。

选定长阶段 Kp=2 案例以展示收敛，并同时显示原算法、模型 Kp=0、固定压差比较。
将物理功能图、通信链、曲线、参数与配置内嵌；没有厂家实机或节能达标声明。
"""
import argparse
import hashlib
import html
import json
import math
from pathlib import Path
from fmu_correction_report import collect
from fmu_diagnostics_report import plot_env, style, save_figure, save
from build_fmu_validation_report import embed

ROOT=Path(__file__).resolve().parents[1]
SOURCE=ROOT/'outputs/fmu-validation-20260915'


def charts(runs,out):
    plt=plot_env();fig,axes=plt.subplots(3,2,figsize=(14,13),constrained_layout=True)
    r=runs['corrected'];rows=r['trace'];t=[x['elapsed_s']/60 for x in rows]
    ds=[d for _,d in r['decisions']];tx=[(at-r['warmup'])/60 for at,_ in r['decisions']]+[180];ds=ds+[ds[-1]]
    axes[0,0].step([0]+t,[rows[0]['blade_input_w']/1000]+[x['blade_input_w']/1000 for x in rows],where='pre',color='#6c8290',lw=2)
    style(axes[0,0],'A. FMU disturbance per heat source','kW / source')
    axes[0,1].step(tx,[d['target_flow_kg_s'] for d in ds],where='post',color='#b58133',label='Demand',lw=1.4)
    axes[0,1].plot(t,[x['g1_flow_kg_s'] for x in rows],color='#098574',label='Measured',lw=2)
    style(axes[0,1],'B. Flow demand and actual response','kg/s')
    axes[1,0].step([0]+t,[r['first']['setpoints']['cdu.dp_sp']['value']]+[x['g1_dp_input_psi']*6.89475728 for x in rows],where='pre',color='#b58133',label='Applied DP command')
    axes[1,0].step(tx,[d['equilibrium_control_target'] for d in ds],where='post',color='#90a0a5',ls=':',label='Model equilibrium DP')
    axes[1,0].plot(t,[x['g1_dp_kpa'] for x in rows],color='#098574',label='Measured DP',lw=2)
    style(axes[1,0],'C. DP command, equilibrium target and measurement','kPa')
    for key,label,color in [('g1_supply_degC','Supply','#277da8'),('g1_return_degC','Return','#c97651')]:
        axes[1,1].plot(t,[x[key] for x in rows],label=label,color=color)
    axes[1,1].axhline(42,color='#af5656',ls=':',label='Return soft limit 42 C')
    style(axes[1,1],'D. Secondary supply and return temperatures','degC')
    for v,label,color in [('baseline','Fixed DP baseline','#87939e'),('legacy','Original algorithm','#c97651'),('corrected','Selected Kp=2','#098574')]:
        run=runs[v];data=run['trace'];tt=[x['elapsed_s']/60 for x in data]
        axes[2,0].plot(tt,[x['g1_pump_w']/1000 for x in data],label=label,color=color)
        prior=run['first']['observations']['cdu.electric_power']['value'];energy=[0.]
        dt=run['summary']['experiment']['communication_step_s']
        for x in data:
            energy.append(energy[-1]+(prior+x['g1_pump_w'])/2*dt/3600000);prior=x['g1_pump_w']
        assert math.isclose(energy[-1],run['metrics']['pump_kwh'],abs_tol=1e-8)
        axes[2,1].plot([0]+tt,energy,label=label,color=color)
    style(axes[2,0],'E. CDU 1 pump electrical power','kW')
    style(axes[2,1],'F. Cumulative CDU 1 pump energy','kWh')
    for ax in axes.flat:
        ax.axvline(60,color='#9aabb3',alpha=.3,lw=.8);ax.axvline(120,color='#9aabb3',alpha=.3,lw=.8)
        if ax!=axes[0,0]:ax.legend(fontsize=8,loc='best')
    save_figure(fig,out,'selected_results',plt)


def table(headers,rows):
    return '<div class="table"><table><thead><tr>'+''.join('<th>'+x+'</th>' for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+str(x)+'</td>' for x in r)+'</tr>' for r in rows)+'</tbody></table></div>'


def build(out):
    out.mkdir(parents=True,exist_ok=True)
    runs=collect(SOURCE/'reference/long');sel=runs['corrected'];s=sel['scene'];p=s['devices']['CDU_01']['policy'];m=sel['metrics']
    charts(runs,out)
    save(out/'selected_scene.json',s)
    flow_gain=100*(1-m['flow_mae_kg_s']/runs['legacy']['metrics']['flow_mae_kg_s'])
    pump_gain=100*(1-m['pump_kwh']/runs['legacy']['metrics']['pump_kwh'])
    abovebaseline=100*(m['pump_kwh']/runs['baseline']['metrics']['pump_kwh']-1)
    vsmodel=100*(m['pump_kwh']/runs['model_only']['metrics']['pump_kwh']-1)
    obs=table(['信息','标准变量 / 单位','来源与作用'],[
        ['二次供液温度 Ts','cdu.sec_supply_temp / K','T_sec_s；计算温差、可用回温裕量，做 15–45°C 范围守护。图表显示 °C。'],
        ['二次回液温度 Tr','cdu.sec_return_temp / K','T_sec_r；计算热需求、回温风险，做 15–55°C 范围守护；42°C 是策略软目标。'],
        ['二次质量流量 m','cdu.sec_flow / kg/s','m_flow_sec；计算 Q̂、跟踪误差和水力校准。范围守护 1–30 kg/s。'],
        ['二次实际压差 Δp','cdu.sec_dp / kPa','(p_sec_s−p_sec_r)/1000；水力校准、外环 P 反馈及 1–400 kPa 范围守护。不是泵转速。'],
        ['CDU 泵电功率','cdu.electric_power / W_e','W_flow_CDUP；记录与能耗评价，当前 FlowPolicy 不以此求解最小电耗。'],
        ['当前压差设定值回读','cdu.dp_sp / kPa','FMU dp 输入 ×6.89475728；作为限速基点与写入确认，不能冒充实际压差。'],
        ['时间 / 质量 / 模式 / owner','Snapshot 元数据','时间来自 FMU 时钟；质量经有限值/单位/新鲜度检查。mode、owner 为实验状态，alarms 为空元组，非真实 CDU 告警采集。'],
        ['邻机、冷却通道与冷源功率','Master 独立实验记录','五组回液/流量/压差、通道液温及泵/风机功率，用于全模型包络和报告；未全部送入单 CDU 优化算法。']])
    controls=table(['项目','本次处理方式','边界'],[
        ['第 1 组压差设定值','唯一在线调整的执行目标：cdu.dp_sp','172.369–262.001 kPa；每次最多 ±3 kPa；两次动作至少间隔 60 s。'],
        ['需求流量','算法中间目标，范围 8–18 kg/s','本次没有向 FMU 下发直接流量指令。'],
        ['循环泵驱动','由 FMU 本地 PID/滤波产生','上层软件未直接写泵转速、变频频率或电功率。'],
        ['供液温度设定、支路阀门','实验开始统一设定后保持','28°C；0.33/0.33/0.34。温度/阀门内环如何动作由 FMU 决定。'],
        ['其余 4 组 CDU、冷却塔目标','保持固定设定/边界','没有冷源组合优化、多 CDU 协同优化或风液联合调度。'],
        ['机柜热源输入','试验编排器提供扰动','60→75→60 kW/热源，不是算法控制 IT，也未给算法未来负荷。']])
    pars=table(['配置参数','本案例取值','作用与调整影响'],[
        ['cp_j_kg_k','4180 J/(kg·K)','估算液冷热负荷 Q̂。是示例介质参数，实液种类/浓度需校准；非已验证厂家冷却液参数。'],
        ['target_delta_k','8.5 K','目标供回液温差。降低通常提高流量需求，可能增加泵耗；不是芯片结温约束。'],
        ['return_soft_limit_k','315.15 K = 42°C','与供液温度一起决定可用温差；高于此值标示软目标风险。设备保护边界另设。'],
        ['minimum / maximum_flow_kg_s','8 / 18 kg/s','算法需求目标限幅；不代表该压差范围内物理可达，模型推算约 11.14–16.94 kg/s。'],
        ['feedback_flow_per_k','0.08 kg/(s·K)','温差超过可用温差时增加流量需求的系数，只增加正向补偿。'],
        ['hydraulic_model.exponent','1.0','固定模型形式 m=g·Δpⁿ；本参考模型的局部线性辨识结果，不能通用于所有 CDU。'],
        ['hydraulic_model.gain','0.0646448823 kg/(s·kPa)','流量与压差映射系数；初值来自事前 hold 辨识。偏大将低估所需压差。'],
        ['hydraulic_model.gain_bounds','[0.04, 0.09]','约束允许发布的水力增益；不是自动学习的保护边界。'],
        ['dp_feedback_gain / Kp','2.0（无量纲）','外环补偿 Δp_eq−Δp_actual；本地执行滞后时增加驱动力，接近后消退。过大可能增加滞后后的超调和泵耗；其他工况 Kp=0/1 更优。'],
        ['calibration_max_dp_error_kpa','0.5 kPa','当前设定值与实际压差未靠近时清空训练窗口、冻结晋升，避免把慢过渡当成稳态。'],
        ['adaptive_enabled','true；实际晋升 0 次','功能开放不代表本轮学习生效。该案例性能来自事前模型辨识和有界反馈。'],
        ['thermal_tau_s / horizon_s','300 s / 60 s','用于一阶回温风险预测；不是 FMU 内环时间常数，也不是已实现 MPC。由同一温差/流量反算热量时可退化为持值预测。'],
        ['max_liquid_load_w','2,000,000 W_th','估算热负荷输入的合理性上界，不是 CDU 标称制冷能力。'],
        ['deadband','0.2 kPa','候选与当前目标差小于此值时不发命令，减少微小写入。'],
        ['max_data_age_s / command_ttl_s','20 s / 15 s','观测最大允许年龄、命令有效期。过期或异常数据不能正常执行。TTL 不代表物理量须在 15 s 达标。'],
        ['control interval / communication step','60 s / 15 s','60 s 为外环决策周期；15 s 为 FMU 推进/记录步长，不是设备响应速度。'],
        ['controls.minimum / maximum / max_step','172.369 / 262.001 / 3 kPa','目标范围与每步变化边界，网关重复检查；在线学习不能放宽。'],
        ['本地 PID k / Ti / Td','0.1 / 100 s / 30 s','FMU 内部参数，保持不变；与外环 Kp、水力 gain 是不同的三个参数。'],
        ['本地滤波 f_cut / order','0.001 Hz / 2','泵控制路径二阶滤波。阶跃辨识实际 t90 约 40 min，决定快速负荷跟踪的限制。']])
    comparison=[]
    for key,label in [('baseline','固定压差基线'),('legacy','原在线算法'),('model_only','线性模型 Kp=0'),('corrected','本汇报：线性模型 Kp=2')]:
        mm=runs[key]['metrics'];fmt=lambda x:'—' if x is None else f'{x:.3f}'
        comparison.append([label]+[fmt(mm[k]) for k in ['flow_mae_kg_s','flow_tail_mae_kg_s','pump_kwh','represented_cooling_kwh','peak_return_degC']])
    comp=table(['同工况策略','全程流量 MAE<br>kg/s','阶段末 10 min MAE<br>kg/s','CDU 1 泵<br>kWh','模型所列冷却<br>kWh','最高回液<br>°C'],comparison)
    physical=embed(out/'physical_components.svg','CDU 物理功能图：设施水、换热器、泵与三条机柜液冷支路')
    info=embed(out/'information_flow.svg','实际 FMU 数据与控制链，以及真实设备的 Modbus TCP 接入路径')
    plots=embed(out/'selected_results.svg','选定工况的六项运行结果')
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>CDU 监督控制软件 · 阶段验证汇报</title>
<style>*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f3f6f7;color:#153448;font:16px/1.85 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1260px;margin:auto;padding:48px 24px}header{padding:28px 0 12px}.eyebrow{font-size:13px;letter-spacing:2px;color:#098574}h1{font-size:43px;line-height:1.3;margin:16px 0}h2{font-size:27px;margin:0 0 15px}h3{font-size:19px}p{margin:12px 0}.lead{max-width:1000px;font-size:20px;color:#445f70}.muted{font-size:14px;color:#607884}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin:24px 0}.card{background:white;padding:23px;border:1px solid #d4e3e5;border-radius:12px}.card b{display:block;font-size:34px;line-height:1.7;color:#098574}section{margin:24px 0;padding:28px;background:white;border:1px solid #d4e3e5;border-radius:14px;scroll-margin-top:15px}img{width:100%;height:auto;display:block;margin:18px 0}.note{padding:16px 20px;border-left:4px solid #ba8940;background:#fff6e7}.success{border-color:#098574;background:#eaf6f1}.tag{font-size:13px;color:#098574}nav{display:flex;gap:9px;flex-wrap:wrap;margin:25px 0}nav a{padding:7px 12px;border-radius:6px;background:#e7f0f0;text-decoration:none}a{color:#087d77}.table{overflow:auto}table{width:100%;border-collapse:collapse;min-width:800px;font-size:14px}th,td{padding:12px;vertical-align:top;text-align:left;border-bottom:1px solid #d7e4e7}th{background:#f0f6f6}td:first-child{min-width:165px;font-weight:600}pre{background:#f3f7f8;border-radius:8px;padding:20px;font-size:13px;white-space:pre-wrap;overflow-wrap:anywhere}details{border:1px solid #d6e3e5;border-radius:8px;padding:13px 17px;margin:18px 0}summary{cursor:pointer;font-weight:600}li{margin:8px 0}.two{display:grid;grid-template-columns:1fr 1fr;gap:24px}.step{padding:18px 22px;background:#eff6f6;border-radius:10px}button{font:inherit;padding:8px 17px;border:1px solid #c5d7da;border-radius:7px;background:white;color:#236456;cursor:pointer}@media(max-width:700px){main{padding:22px 12px}h1{font-size:30px}.cards,.two{grid-template-columns:1fr}section{padding:18px}.lead{font-size:17px}}@media print{body{background:white}main{padding:0}button,nav{display:none}section{break-inside:auto;border-radius:0}details>*{display:block}.card{break-inside:avoid}}</style></head><body><main>
<header><div class="eyebrow">AIDC LIQUID COOLING / 阶段验证汇报 / 2026.09.16</div><h1>把 CDU 接入可审计的监督控制闭环</h1>
<p class="lead">已在 Sustain-LC 参考 FMU 上跑通“采集—热需求计算—压差调节—响应反馈”。选取长负荷阶段案例，展示软件的控制边界、收敛效果与后续落地条件。</p>
<p class="muted">软件 v0.4.1 · 液对液 CDU 场景 · 单组压差控制 · 仿真验证，尚无厂家实机验收</p></header>
<div class="cards"><div class="card">阶段末流量误差<b>0.030 kg/s</b>三个阶段各末 10 min 的平均绝对误差</div><div class="card">最高回液温度<b>40.83°C</b>本案例评估期未超过 42°C 软目标</div><div class="card">压差目标确认<b>168 次</b>720 个过程采样；无拒绝，FMI 状态均为 0</div></div>
<div class="note">本页选取的是收敛表现较好的工况，不是全工况最优参数证明。泵耗相对原算法降低 __PUMP__%，相对固定压差基线仍高 __BASE__%；当前尚未验证系统节能目标。</div>
<nav><a href="#case">案例选择</a><a href="#physical">物理场景</a><a href="#flow">信息链路</a><a href="#io">输入与控制范围</a><a href="#algorithm">算法与参数</a><a href="#results">结果证据</a><a href="#delivery">落地计划</a></nav>
<button onclick="window.print()">打印 / 保存 PDF</button>
<section id="case"><span class="tag">01 / 向上级说明的核心结论</span><h2>已验证可运行闭环，下一步是模型物理核对与设备台架</h2>
<div class="two"><div><h3>为什么选这一组</h3><p>选定 reference/long/corrected：每个热源依次 60 → 75 → 60 kW，每阶段 60 min，共评估 180 min，预热另计 60 min。采用线性水力模型、外环 Kp=2。</p><p>该案例能观察到需求增加、压差抬升、实际流量逼近，再到需求下降后平稳回落的完整过程。它避免用单个静态点宣称闭环成功。</p></div><div><h3>这组能说明什么</h3><p>同一工况下，相比原在线算法，全程流量 MAE 改善约 __FLOW__%，泵电量下降约 __PUMP__%，温度峰值基本相当；输入、决策、写入与实际响应均可追溯。</p><p>相较线性模型 Kp=0，本配置用约 __MODEL__% 的额外 CDU 泵电量换取更小末段误差；固定压差基线仍更省电。选择理由是收敛展示，而不是挑选节能率最高的结果。</p></div></div>
<p class="muted">总测试中，小幅波动与 30 min 工况更适合 Kp=0，5 min 工况 Kp=1 略优。所有不利结果仍保留在 <a href="../fmu-validation-20260915/index.html">29 组完整验证总览</a>。</p></section>
<section id="physical"><span class="tag">02 / 软件面对的被控对象</span><h2>液对液 CDU：隔离换热，驱动二次侧液体循环</h2>
<p>参考模型包含五组 computeBlock，每组一个 CDU 和一个机柜聚合模型；各机柜模型有三个热源输入与三条冷却通道。五组共享设施水/冷却塔侧边界，本次只有第 1 组压差由上层算法调整。</p>
__PHYSICAL__
<div class="two"><div><h3>物理部件与职责</h3><ul><li><strong>换热器 HEX：</strong>将机柜二次侧吸收的热量传给设施水侧；两侧流体经换热界面交换热量。</li><li><strong>循环泵 CDUP：</strong>提供循环驱动力；本地控制将压差误差转换为泵驱动信号。</li><li><strong>阀门与管阻：</strong>影响流量分配和所需压差。机柜三支路阀门固定，CDU 内部控制部件由 FMU 自身运行。</li><li><strong>冷却通道：</strong>代表与 IT 热源交换热量的液体通道，不把通道液温称为芯片结温。</li></ul></div><div><h3>固定条件</h3><ul><li>所有 CDU 初始压差 27.5 psi ≈189.606 kPa。</li><li>供液温度设定 28°C；实际供液温度会随动态变化。</li><li>支路开度固定 0.33 / 0.33 / 0.34。</li><li>其余四组每个外部热源固定 60 kW。</li><li>湿球 293.15 K=20°C；冷却塔目标约 25.56°C。</li></ul></div></div>
<p class="muted">FMU 元数据可见 HEX、CDUP、valveCDU、press_CDU 等对象。图是按已确认功能整理的物理示意，省略辅件；没有宣称某厂家具体 BOM、额定功率、机柜密度或泵冗余数量。ComputePowerBlade 的总和到 CDU 瞬时热负荷尺度尚未核对，不能据此给设备标称 180/225 kW 容量。</p></section>
<section id="flow"><span class="tag">03 / 信息如何传输</span><h2>统一数据契约，使算法与通信协议分离</h2>
__INFO__
<ol><li><strong>过程推进：</strong>实验 Master 先写入本阶段热源扰动，再调用 do_step 推进 15 s，读取五组过程状态。一个 Master 独占整个 FMU，避免各 CDU 独立推进共享模型。</li>
<li><strong>采集与标准化：</strong>SustainFmuAdapter 将第 1 组温压流和功率封装成 Reading/Snapshot，带单位和模型时间。Engine 每 60 s 执行一次控制周期，先记录观测并检查运行资格。</li>
<li><strong>计算与审核：</strong>FlowPolicy 检查校准资格、计算候选；网关校验控制归属、有效期、单位、上下限、变化幅度与最短间隔，写入前再读状态并先落盘写意图。</li>
<li><strong>执行与反馈：</strong>Adapter.write 将标准压差指令交给 Master，kPa 换算为 psi 后写第 1 组输入；读取设定值确认。本地 PID/滤波在后续模型推进中作用到泵，下一轮再观察实际流量、压差和温度。</li>
<li><strong>现场替换：</strong>真实 CDU 使用 ModbusCDUAdapter 通过以太网访问 OEM 点表。算法仍接收 Snapshot、输出 ControlRequest；寄存器地址、缩放、字节序、模式和权限来自具体设备适配包。</li></ol>
<p>当前实机通信驱动已实现，但本案例没有经过网口或真实 PLC。未来接入时需验证远程目标是否真正生效、本地保护优先级、控制权归属、失联接管与心跳；不能把 FMU 的“恢复固定目标”当成 OEM 看门狗已验证。</p></section>
<section id="io"><span class="tag">04 / 可审计的输入与输出边界</span><h2>从 CDU 读取什么，算法实际控制什么</h2>
__OBS__<h3>唯一在线执行量：第 1 组二次侧压差设定值</h3>__CONTROLS__
<p class="muted">接口字段不仅有数值，还包括单位、时间戳、质量、来源；命令还包括设备 ID、拓扑版本、owner、命令 ID 和截止时间。多厂商通用性来自这层契约，不意味着只填 IP 即可安全控制任意 CDU。</p></section>
<section id="algorithm"><span class="tag">05 / 控制原理与参数说明</span><h2>用热需求确定流量，再通过压差让本地控制执行</h2>
<pre>Q̂ = cp × m_actual × (Tr − Ts)
ΔT_available = min(8.5 K, 42°C − Ts)
m_demand = Q̂ / (cp × ΔT_available)
           + 0.08 × max(0, Tr − Ts − ΔT_available)
m* = clamp(m_demand, 8, 18) kg/s
Δp_eq = (m* / gain)^(1 / exponent)
Δp_candidate = Δp_eq + 2 × (Δp_eq − Δp_actual)
→ 压差范围限制 → 每步 ±3 kPa 限制 → 死区/网关检查 → 下发</pre>
<p>本例没有独立 IT 功率测量或未来预测，热负荷由当前流量与温差反算。外环采用模型换算和有界 P 反馈，没有积分累积；控制器不是全站能耗优化器。外部机柜预测接口已有软件入口，本案例未使用。</p>
__PARAMS__
<details><summary>在线校准何时允许生效</summary><p>样本需满足质量、新鲜度、无告警、准稳态、非执行器边界以及足够压差变化；本例还要求实际压差靠近当前设定值 0.5 kPa。最近最多 60 个合格样本，最后 8 个留作后段验证；至少 20 个样本后每 10 次合格采样尝试候选，单次 gain 变化不超过 ±2%，验证 MSE 至少改善 2% 才可晋升。</p><p><strong>本案例 adaptive_enabled=true，但实际晋升次数为 0。</strong>因此不能把曲线改善宣传为在线学习收益。学习不会改变保护限制，模型恢复按完整场景哈希绑定。</p></details>
<details><summary>下载与查看本次实际配置</summary><p><a href="selected_scene.json" download>下载 selected_scene.json</a>；这是选定运行的原始场景快照，未改为其他参数。</p><pre>__SCENE__</pre></details></section>
<section id="results"><span class="tag">06 / 选定案例的全部关键证据</span><h2>过程收敛改善，能效目标仍需继续验证</h2>
__PLOTS__
<p>A：每个外部热源的扰动；B：需求流量与实测；C：模型稳态压差、实际下发和实际响应；D：供回液温度；E/F：CDU 1 泵功率和累计电量。边界线为 60/120 min 负荷切换，预热不进入本评估窗口。</p>
__COMPARISON__
<p>全程 MAE 包含三个阶段全部评估样本；末段 MAE 是每阶段最后 10 min 的合并平均值，不是全程精度。采样先于同时间点决策，误差使用严格更早的目标配对。目标回读确认 168 次不代表 168 次都已达到实际压差目标。</p>
<div class="note">电量只能在同一负荷、初态和时域内比较。模型所列冷却包括 5 台 CDU 泵、一次泵、塔泵和塔风机，未包含全部 AIDC 用电；比原算法下降不代表比固定压差基线节能。已有其他工况证明 Kp=2 并非普遍最优。</div></section>
<section id="delivery"><span class="tag">07 / 对产品阶段的判断</span><h2>可进入设备适配准备阶段，尚不具备现场节能验收结论</h2>
<div class="two"><div class="step"><h3>已经具备</h3><ul><li>单 CDU 监督控制核心、统一量纲与能力声明。</li><li>Modbus TCP 读写驱动、本机协议联调。</li><li>FMU 闭环、命令回读、日志审计与有界模型校准流程。</li><li>按工况比较跟踪、温度和电量的验证工具。</li></ul></div><div class="step"><h3>下一阶段交付</h3><ul><li>核对 FMU 负绝对压力、初始化与热源尺度；不能用它代替实机物理标定。</li><li>选定一款 CDU 型号/固件，取得点表、控制权限、工程限值和本地保护说明。</li><li>从监测到影子计算，再到受限控制，完成阶跃、失联、回退和长期测试。</li><li>在同服务水平下验证节能；再扩展供温、预测前馈及冷源协同。</li></ul></div></div>
<p class="muted">本例最小绝对压力约 −0.934 MPa，说明模型物理一致性尚待核对。软件配置包络检查通过不等于硬件安全认证；通道液温不是 GPU 结温。FMU 内环阶跃 t90 约 40 min，5–10 min 快速负荷跟踪尚未解决。</p></section>
<section><h2>附：复核入口与来源</h2><p><a href="../fmu-validation-20260915/index.html">29 组实验总览</a> · <a href="../fmu-validation-20260915/reference/long/corrected/summary.json">选定运行原始摘要</a> · <a href="../fmu-validation-20260915/reference/long/corrected/trace.csv">原始轨迹 CSV</a> · <a href="briefing_evidence.json">本汇报取数与哈希</a> · <a href="../../docs/边缘液冷智控产品审查与使用手册.html">完整产品手册</a></p>
<p>物理和数据链依据：lc_control/sustain_fmu.py、policy.py、engine.py、runtime.py、modbus.py；FMU modelDescription.xml 中已核对的组件/变量。汇报未新增仿真，未修改默认运行配置，未删减原始对照。</p><p class="muted">本 HTML 内嵌 2 张原理图、6 项曲线与完整场景，单独复制可阅读；下载原始数据与源码链接需要保留项目文件。图纸仅供说明产品边界。</p></section>
</main></body></html>'''
    for k,v in {'__PUMP__':f'{pump_gain:.1f}','__BASE__':f'{abovebaseline:.1f}','__MODEL__':f'{vsmodel:.2f}',
                '__FLOW__':f'{flow_gain:.1f}','__PHYSICAL__':physical,'__INFO__':info,'__OBS__':obs,
                '__CONTROLS__':controls,'__PARAMS__':pars,'__SCENE__':html.escape(json.dumps(s,ensure_ascii=False,indent=2)),
                '__PLOTS__':plots,'__COMPARISON__':comp}.items():page=page.replace(k,v)
    (out/'index.html').write_text(page,encoding='utf-8')
    save(out/'briefing_evidence.json',{'source_run':'fmu-validation-20260915/reference/long/corrected',
        'selection_reason':'Convergence demonstration in 60-minute phases; not globally optimal and not an energy-savings acceptance.',
        'selected_metrics':m,'flow_mae_improvement_vs_legacy_pct':flow_gain,'pump_reduction_vs_legacy_pct':pump_gain,
        'pump_increase_vs_fixed_baseline_pct':abovebaseline,'pump_increase_vs_model_only_pct':vsmodel,
        'source_sha256':{str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest()
         for f in [SOURCE/'reference/long/corrected'/n for n in ['scene.json','summary.json','trace.csv','runtime.sqlite']]},
        'fmu_sha256':sel['summary']['contract']['fmu_sha256'],'no_new_simulation':True})
    print(out/'index.html')


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    build(Path(p.parse_args().output))
