"""修正实验报告：读取真实轨迹/决策，不平移曲线、不修改目标或删去困难区间。"""
import argparse
import bisect
import csv
import html
import json
from pathlib import Path
import sqlite3
import statistics

from fmu_diagnostics_report import load_csv, plot_env, save, save_figure, style

VARIANTS=['baseline','legacy','gain_only','model_only','corrected']
NAMES={'baseline':'固定压差基线','legacy':'原在线算法','gain_only':'只修正 gain',
       'model_only':'修正水力模型','corrected':'水力模型＋有界反馈'}


def load_run(folder):
    summary=json.loads((folder/'summary.json').read_text())
    scene=json.loads((folder/'scene.json').read_text())
    if not summary['complete'] or summary['fmi_statuses']!=[0]:raise ValueError('incomplete_or_failed_fmu')
    trace=load_csv(folder/'trace.csv')
    dbpath=folder/'runtime.sqlite';wal=dbpath.with_name(dbpath.name+'-wal')
    if wal.exists() and wal.stat().st_size:raise ValueError('uncheckpointed_wal')
    with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
        decisions=[(t,json.loads(p)) for t,p in db.execute("SELECT at,payload FROM events WHERE kind='decision' ORDER BY id")]
        first=json.loads(db.execute("SELECT payload FROM events WHERE kind='telemetry' ORDER BY id LIMIT 1").fetchone()[0])
    warmup=scene['extensions']['sustain_fmu_experiment']['warmup_s']
    phase=scene['extensions']['sustain_fmu_experiment']['load_phase_s']
    ticks=[t for t,d in decisions];pairs=[]
    for r in trace:
        i=bisect.bisect_left(ticks,r['time_s'])-1
        if i<0:continue
        d=decisions[i][1]
        pairs.append({'elapsed_s':r['elapsed_s'],'demand_flow_kg_s':d['target_flow_kg_s'],
                      'actual_flow_kg_s':r['g1_flow_kg_s'],
                      'equilibrium_dp_kpa':d['equilibrium_control_target'],
                      'applied_dp_kpa':r['g1_dp_input_psi']*6.89475728,'actual_dp_kpa':r['g1_dp_kpa']})
    m={'pump_kwh':summary['energy']['g1_pump_kwh'],
       'represented_cooling_kwh':summary['energy']['represented_cooling_kwh'],
       'peak_return_degC':summary['g1_peak_return_degC'],
       'all_groups_peak_return_degC':summary['all_groups_peak_return_degC'],
       'all_channels_peak_degC':summary['all_channels_peak_degC'],
       'model_updates':summary['model_updates'],'confirmed':summary['confirmed'],
       'samples':len(trace),'min_absolute_pressure_pa':summary['min_absolute_pressure_pa'],
       'flow_mae_kg_s':None,'flow_tail_mae_kg_s':None,'applied_dp_mae_kpa':None}
    if pairs:
        m['flow_mae_kg_s']=statistics.mean(abs(r['demand_flow_kg_s']-r['actual_flow_kg_s']) for r in pairs)
        m['flow_tail_mae_kg_s']=statistics.mean(abs(r['demand_flow_kg_s']-r['actual_flow_kg_s']) for r in pairs
             if ((r['elapsed_s']-1e-9)%phase)>=phase-600)
        m['applied_dp_mae_kpa']=statistics.mean(abs(r['applied_dp_kpa']-r['actual_dp_kpa']) for r in pairs)
        m['equilibrium_dp_mae_kpa']=statistics.mean(abs(r['equilibrium_dp_kpa']-r['actual_dp_kpa']) for r in pairs)
        m['rate_limited_decisions']=sum('control_rate_limited' in d['warnings'] for t,d in decisions)
    return dict(summary=summary,scene=scene,trace=trace,decisions=decisions,first=first,warmup=warmup,
                pairs=pairs,metrics=m)


def collect(folder):
    runs={v:load_run(folder/v) for v in VARIANTS}
    reference=runs['baseline']
    for run in runs.values():
        # 消融只允许策略参数变化；环境、物理边界与设备控制限制必须一致。
        if run['scene']['extensions']!=reference['scene']['extensions']:raise ValueError('boundary_mismatch')
        for key in ['controls','guards','initial_setpoints','initial_observations']:
            if run['scene']['devices']['CDU_01'][key]!=reference['scene']['devices']['CDU_01'][key]:
                raise ValueError('device_constraint_mismatch:'+key)
        if [(r['time_s'],r['blade_input_w']) for r in run['trace']] != [(r['time_s'],r['blade_input_w']) for r in reference['trace']]:
            raise ValueError('time_or_load_mismatch')
    return runs


def tracking_figure(runs,output,name):
    plt=plot_env();fig,axes=plt.subplots(2,2,figsize=(14,9),constrained_layout=True)
    for col,v in enumerate(['legacy','corrected']):
        run=runs[v];t=[0]+[r['elapsed_s']/60 for r in run['trace']]
        tx=[(at-run['warmup'])/60 for at,d in run['decisions']]+[t[-1]]
        decisions=[d for _,d in run['decisions']];decisions=decisions+[decisions[-1]]
        def actual(key,initial):return [initial]+[r[key] for r in run['trace']]
        first=run['first']['observations']
        axes[0,col].step(tx,[d['target_flow_kg_s'] for d in decisions],where='post',color='#bb802b',label='Demand (same thermal rule)')
        axes[0,col].plot(t,actual('g1_flow_kg_s',first['cdu.sec_flow']['value']),color='#087f8c',label='Actual flow',lw=2)
        style(axes[0,col],'Original algorithm' if col==0 else 'Corrected model + bounded feedback','Mass flow (kg/s)')
        initial=run['first']['setpoints']['cdu.dp_sp']['value']
        axes[1,col].plot(t,actual('g1_dp_kpa',first['cdu.sec_dp']['value']),color='#087f8c',label='Measured DP',lw=2)
        axes[1,col].step(tx,[d['equilibrium_control_target'] for d in decisions],where='post',ls=':',color='#8b9aa7',label='Model equilibrium DP')
        # 轨迹记录的是本通信步真正应用的输入；决策不能冒充已经写入的设定值。
        axes[1,col].step(t,[initial]+[r['g1_dp_input_psi']*6.89475728 for r in run['trace']],where='pre',color='#bb802b',label='Applied DP command')
        style(axes[1,col],'DP: equilibrium target, command and response','Differential pressure (kPa)')
        for row in range(2):
            axes[row,col].legend(fontsize=8,loc='upper left')
            phase=run['summary']['experiment']['load_phase_s']/60
            for boundary in range(int(phase),int(t[-1]),int(phase)):axes[row,col].axvline(boundary,color='#8797a1',alpha=.2,lw=.8)
        for ax in axes[0]:ax.set_ylim(9,18)
        maximum=max(d['equilibrium_control_target'] for v in ['legacy','corrected'] for _,d in runs[v]['decisions'])
        for ax in axes[1]:ax.set_ylim(145,maximum+10)
    save_figure(fig,output,name,plt)


def table(runs,tail=False):
    rows=[]
    for v in VARIANTS:
        m=runs[v]['metrics']
        num=lambda x:'—' if x is None else f'{x:.3f}'
        rows.append('<tr><td>'+NAMES[v]+'</td>'+''.join('<td>'+num(m[k])+'</td>' for k in
            ['flow_mae_kg_s','flow_tail_mae_kg_s','applied_dp_mae_kpa','pump_kwh','represented_cooling_kwh','peak_return_degC'])+'</tr>')
    return '<div class="table"><table><thead><tr><th>策略</th><th>全程流量 MAE<br>kg/s</th><th>阶段末 10 min MAE<br>kg/s</th><th>命令－实际压差 MAE<br>kPa</th><th>CDU 1 泵<br>kWh</th><th>模型所列冷却<br>kWh</th><th>最高回液<br>°C</th></tr></thead><tbody>'+''.join(rows)+'</tbody></table></div>'


def build(short,long,diagnostics):
    a=collect(short);b=collect(long)
    metrics={'short':{v:r['metrics'] for v,r in a.items()},'long':{v:r['metrics'] for v,r in b.items()}}
    save(short/'correction_metrics.json',metrics)
    for tag,runs in [('short',a),('long',b)]:
        for v,run in runs.items():
            if not run['pairs']:continue
            with (short/(tag+'_'+v+'_aligned.csv')).open('w',newline='') as f:
                w=csv.DictWriter(f,fieldnames=run['pairs'][0]);w.writeheader();w.writerows(run['pairs'])
        tracking_figure(runs,short,tag+'_tracking')
    steps=json.loads((diagnostics/'step_metrics.json').read_text())
    manifest=json.loads((short/'manifest.json').read_text())
    improvement=lambda tag:100*(1-metrics[tag]['corrected']['flow_mae_kg_s']/metrics[tag]['legacy']['flow_mae_kg_s'])
    pumpchange=lambda tag:100*(metrics[tag]['corrected']['pump_kwh']/metrics[tag]['legacy']['pump_kwh']-1)
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FMU 跟踪修正：gain、模型与慢内环</title><style>
*{box-sizing:border-box}body{margin:0;background:#f3f7f8;color:#17333e;font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1280px;margin:auto;padding:36px 24px}h1{font-size:34px;line-height:1.4}h2{font-size:23px}h3{font-size:18px}
section{padding:25px;margin:22px 0;background:white;border:1px solid #d5e3e7;border-radius:12px}img{width:100%;height:auto;display:block}
a{color:#087f8c}.note{padding:15px 20px;background:#fff4df;border-left:4px solid #bb802b}.good{background:#eaf7f4;border-color:#087f8c}
.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:15px}.card{padding:20px;background:white;border:1px solid #d5e3e7;border-radius:10px}.card b{display:block;font-size:29px;color:#087f8c}.muted{color:#657980;font-size:14px}
table{border-collapse:collapse;width:100%;min-width:850px;font-size:14px}td,th{text-align:left;padding:11px;border-bottom:1px solid #d5e3e7}th{background:#eef5f6}.table{overflow:auto}pre{white-space:pre-wrap;background:#f3f7f8;padding:18px;border-radius:8px}code{font-size:14px}@media(max-width:700px){main{padding:22px 12px}.cards{grid-template-columns:1fr}section{padding:16px}h1{font-size:27px}}</style></head><body><main>
<p class="muted">LC CONTROL · 2026-09-15 · 参考 FMU 实验，无硬件</p>
<h1>跟不上的原因，既有模型误差，也有执行速度限制</h1>
<p>已完成固定负荷 ±3 kPa 阶跃、原条件五策略对照，以及长负荷阶段五策略对照。
修正没有改变 FMU 本地 PID、滤波器、设备范围或每分钟 3 kPa 限速，也没有提供未来负荷。</p>
<div class="cards"><div class="card">本地压差阶跃 t90<b>40.4–40.8 min</b>原负荷每 10 min 改变一次</div>
<div class="card">快速变负荷流量误差改善<b>__SHORT__%</b>仍未解决快速跟踪</div>
<div class="card">长负荷阶段流量误差改善<b>__LONG__%</b>每阶段 60 min，单独评价</div></div>
<section><h2>1. 根因：不是把 gain 调大就能解决</h2>
<ol><li><strong>初始 gain 偏大。</strong>原模型 m=0.95√Δp 在约 190 kPa 预测约 13.1 kg/s，实际只有约 12.26 kg/s；反算压差偏低，出现缺流量却先降压。</li>
<li><strong>水力模型形式不匹配。</strong>固定负荷 hold 辨识 m≈__GAIN__Δp；±3 kPa 独立阶跃测得局部斜率约 0.06453–0.06454 kg/(s·kPa)。工作点附近更接近线性；只调平方根模型 gain 能修正一点的偏差，不能修正曲线斜率。</li>
<li><strong>本地执行很慢。</strong>压差与流量 t90 约 40 min，2% 稳定时间约 66–67 min；实际读取 PID k=0.1、Ti=100 s、Td=30 s，二阶滤波 f_cut=0.001 Hz，滤波路径处于选中状态。</li>
<li><strong>外部限速叠加。</strong>例如目标从约 174 升到 262 kPa，单是每分钟 3 kPa 的目标变化就需约 29 min；实际量还需继续响应。不能把“目标回读成功”当成物理量跟踪成功。</li>
<li><strong>低负荷需求可能低于可达下限。</strong>按已辨识关系，最低允许压差 172.37 kPa 对应约 11.14 kg/s；50 kW 阶段的需求约 10.4 kg/s，即使稳态也无法在现有限值内完全贴合。需求和可达范围需同时展示，不能偷偷抬高需求曲线以缩小误差。</li></ol>
<p>以上水力辨识首先适用于参考模型附近工况，不是所有 CDU 的经验公式。数据仍存在负绝对压力，不能作为实机物理标定。</p>
<p><a href="../sustain-fmu-diagnostics-20260915-001/index.html">完整固定负荷阶跃报告</a> · <a href="../sustain-fmu-diagnostics-20260915-001/step_metrics.json">阶跃指标</a></p></section>
<section><h2>2. 代码实际修正了什么</h2>
<pre>热需求与原算法相同 → 需求流量 m*
可配置水力模型：m = gain × Δp^exponent
稳态所需压差 Δp_eq = (m* / gain)^(1/exponent)
压差候选 = Δp_eq + Kp × (Δp_eq − 实测 Δp)
设备上下限 → 每次变化限制 → 安全网关 → 实际写入与回读</pre>
<p>参考 FMU 使用 exponent=1、gain=__GAIN__、Kp=2。Kp 是新增外环反馈系数，与水力 gain、FMU 内部 PID 的 k 是三个不同的量。反馈接近稳态时自动消退，无积分积累；它不能越过设备限制。通用配置默认 Kp=0，现场需要自己的响应辨识。</p>
<p>新增 calibration_max_dp_error_kpa=0.5：内环尚未接近目标时清空校准窗口并冻结晋升。修正实验未发生在线参数晋升；本轮改善来自事前辨识和反馈，不能归因于在线学习越来越快。</p>
<p>决策新增稳态目标、反馈候选、限幅后目标、限速后目标、流量偏差、压差偏差和限制原因。灰线是模型稳态目标，橙线是真实下发目标，青线是实际响应；反馈期间橙线高于灰线是有意驱动，不能只用橙青误差评价外环效果。</p></section>
<section><h2>3. 原条件复测：每 10 分钟变负荷</h2>
<p>共同预热 60 min，评估 60 min；热源依次 60 / 75 / 50 / 65 / 80 / 60 kW。其余四组固定，采样 15 s、控制 60 s。五种策略分别新建 FMU，原在线算法复跑与旧结果一致。</p>
<img src="short_tracking.svg" alt="原算法与修正策略在原快速变负荷下的需求流量、实测流量、压差目标与响应">
__SHORTTABLE__
<div class="note">流量误差降低约 __SHORT__%，但相对原在线算法 CDU 1 泵耗增加 __PUMP_SHORT__%。原 10 min 阶段很短，末 10 min 指标等于全阶段；修正后仍然不能紧跟快速负荷变化，不能宣称节能成功。</div></section>
<section><h2>4. 收敛验证：每阶段固定负荷 60 分钟</h2>
<p>另起实验：热源 60 / 75 / 60 kW，每阶段 60 min，总评估 180 min。共同预热仍为 60 min，其他边界和设备限制不变。这张图验证收敛，不能冒充原来 10 min 负荷的跟踪表现。</p>
<img src="long_tracking.svg" alt="长负荷阶段原算法与修正策略的跟踪对照">
__LONGTABLE__
<div class="note good">平均流量误差改善 __LONG__%；修正后的阶段末 10 min 平均误差约 __TAIL__ kg/s。相对原在线算法，泵耗变化 __PUMP_LONG__%；与固定压差基线相比仍耗电更多。原算法在长恒定阶段仍有偏差，说明 gain 之外的模型形式修正有意义。</div></section>
<section><h2>5. 如何理解结果与下一步</h2>
<p><strong>可交付：纠正初始动作方向、改进稳态模型、增加有界实际压差反馈、阻止未跟上的慢过程被误学为稳态。</strong>原 10 min 负荷跟踪仍受限制；固定负荷阶跃证明了这一点。</p>
<p>若产品必须满足更快的跟踪指标，需要在真实设备台架确认本地 PID/滤波响应与允许的压差变化速度，再选择外环周期与参数；或者接入覆盖执行延迟的有效负荷预测。本轮不放宽限值、不修改 FMU 内部参数、不使用未来负荷。单独做“更快内环”的实验必须另列被控对象版本和结果。</p>
<p>保持“热安全边界内尽量节能”的目标时，还需优化温差/供温目标和完整冷源能耗，而不是将流量跟踪越紧直接等同节能。当前 8.5 K 温差需求本身会在高负荷时增加泵耗。</p>
<p class="muted">MAE 对齐使用严格早于观测时刻的决策，避免同刻先测后控造成时间泄漏。各策略需求由相同热规则及各自实测温差/流量算出，既不是预知热源，也不是同一条外部流量指令。阶段末指标取每阶段最后 600 s；全程指标包括所有评估样本。冷却电量只涵盖模型列出的五台 CDU 泵、一次泵、塔泵与塔风机，不是整个 AIDC。</p>
<p class="muted">所有新运行 FMI 状态为 0，检查了五组的配置运行包络；负绝对压力和热源尺度关系仍未解决，无实机、芯片温度或现场节能证明。</p></section>
<section><h2>6. 文件、配置与复现</h2>
<p><a href="correction_metrics.json">完整指标 JSON</a> · <a href="manifest.json">短实验清单与源码哈希</a> · <a href="../sustain-fmu-correction-long-20260915-001/manifest.json">长实验清单</a> · <a href="corrected_config.json">本次修正配置</a></p>
<p><a href="short_tracking.png">短实验 PNG</a> · <a href="long_tracking.png">长实验 PNG</a> · <a href="short_corrected_aligned.csv">短实验对齐 CSV</a> · <a href="long_corrected_aligned.csv">长实验对齐 CSV</a> · <a href="../../docs/边缘液冷智控产品审查与使用手册.html#section-5">算法与使用说明</a></p>
<pre>python3 tools/run_fmu_tracking_comparison.py \
  --fmu /Users/song/Developer/sustain-lc/LC_Frontier_5Cabinet_4_17_25.fmu \
  --identification outputs/sustain-fmu-diagnostics-20260915-001 \
  --output outputs/my-correction-001
# 长负荷阶段另用一个新目录，并增加 --long-hold。</pre></section></main></body></html>'''
    values={'__SHORT__':f'{improvement("short"):.1f}','__LONG__':f'{improvement("long"):.1f}',
            '__GAIN__':f'{manifest["linear_gain"]:.8f}', '__SHORTTABLE__':table(a), '__LONGTABLE__':table(b),
            '__PUMP_SHORT__':f'{pumpchange("short"):.1f}', '__PUMP_LONG__':f'{pumpchange("long"):+.1f}',
            '__TAIL__':f'{metrics["long"]["corrected"]["flow_tail_mae_kg_s"]:.3f}'}
    for key,value in values.items():page=page.replace(key,value)
    (short/'index.html').write_text(page,encoding='utf-8')
    print(json.dumps(metrics,ensure_ascii=False,indent=2))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--short',required=True);p.add_argument('--long',required=True);p.add_argument('--diagnostics',required=True)
    a=p.parse_args();build(Path(a.short),Path(a.long),Path(a.diagnostics))
