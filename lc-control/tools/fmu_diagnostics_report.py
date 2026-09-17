"""从只读旧闭环和新阶跃 CSV 生成诊断图；不重算或伪造历史控制动作。"""
import bisect
import csv
import html
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import statistics
from fmu_step_metrics import response_metrics


def load_csv(path):
    with path.open() as f:return [{k:float(v) for k,v in r.items()} for r in csv.DictReader(f)]


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def plot_env():
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    return plt


def style(ax,title,ylabel):
    ax.set(title=title,xlabel='Time (min)',ylabel=ylabel)
    ax.grid(alpha=.2);ax.spines[['top','right']].set_visible(False)


def save_figure(fig,output,name,plt):
    fig.savefig(output/(name+'.png'),dpi=180)
    fig.savefig(output/(name+'.svg'))
    plt.close(fig)


def tracking(source,output):
    """决策使用真实 tick 时刻阶梯保持；压力目标使用 Master 实际写入日志。
    CSV 在同一时刻的 tick 之前采样，因此误差只用严格更早的决策配对。
    """
    output.mkdir(parents=True,exist_ok=True)
    summaries={};plot_data=[]
    for i,variant in enumerate(['fixed','adaptive']):
        folder=source/variant
        dbpath=folder/'runtime.sqlite'
        wal=dbpath.with_name(dbpath.name+'-wal')
        if wal.exists() and wal.stat().st_size:raise ValueError('uncheckpointed_source_wal')
        with sqlite3.connect(dbpath.resolve().as_uri()+'?mode=ro&immutable=1',uri=True) as db:
            decisions=[(at,json.loads(payload)) for at,payload in db.execute(
                "SELECT at,payload FROM events WHERE kind='decision' ORDER BY id")]
            first=json.loads(db.execute("SELECT payload FROM events WHERE kind='telemetry' ORDER BY id LIMIT 1").fetchone()[0])
        scene=json.loads((folder/'scene.json').read_text())
        warmup=scene['extensions']['sustain_fmu_experiment']['warmup_s']
        rows=load_csv(folder/'trace.csv')
        times=[0]+[r['elapsed_s']/60 for r in rows]
        flows=[first['observations']['cdu.sec_flow']['value']]+[r['g1_flow_kg_s'] for r in rows]
        dp=[first['observations']['cdu.sec_dp']['value']]+[r['g1_dp_kpa'] for r in rows]
        tx=[(t-warmup)/60 for t,_ in decisions]
        demand=[d['target_flow_kg_s'] for _,d in decisions]
        writes=json.loads((folder/'fmu_writes.json').read_text())
        wt=[(w['time_s']-warmup)/60 for w in writes]
        wv=[w['value_kpa'] for w in writes]
        if wt[0]>0:wt.insert(0,0);wv.insert(0,first['setpoints']['cdu.dp_sp']['value'])
        cap=scene['devices']['CDU_01']['controls']['cdu.dp_sp']
        desired=[d.get('bounded_control_target',max(cap['minimum'],min(cap['maximum'],
                 (d['target_flow_kg_s']/d['gain'])**(1/d.get('hydraulic_exponent',.5))))) for _,d in decisions]
        plot_data.append(dict(variant=variant,times=times,flows=flows,dp=dp,
                              tx=tx+[times[-1]],demand=demand+[demand[-1]],
                              desired=desired+[desired[-1]],wt=wt+[times[-1]],wv=wv+[wv[-1]]))
        paired=[]
        ticks=[t for t,_ in decisions]
        for r in rows:
            j=bisect.bisect_left(ticks,r['time_s'])-1
            if j<0:continue
            d=decisions[j][1]
            paired.append({"time_s":r['time_s'],"elapsed_s":r['elapsed_s'],
                           "demand_flow_kg_s":d['demand_flow_kg_s'],"bounded_target_flow_kg_s":d['target_flow_kg_s'],
                           "actual_flow_kg_s":r['g1_flow_kg_s'],
                           "applied_dp_kpa":r['g1_dp_input_psi']*6.89475728,"actual_dp_kpa":r['g1_dp_kpa']})
        with (output/(variant+'_tracking.csv')).open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=paired[0]);w.writeheader();w.writerows(paired)
        summaries[variant]={"flow_tracking_mae_kg_s":statistics.mean(abs(r['bounded_target_flow_kg_s']-r['actual_flow_kg_s']) for r in paired),
                            "pressure_tracking_mae_kpa":statistics.mean(abs(r['applied_dp_kpa']-r['actual_dp_kpa']) for r in paired),
                            "samples":len(paired),"decision_count":len(decisions),"actual_write_count":len(writes)}
    try:plt=plot_env()
    except ModuleNotFoundError:
        # 宿主无 matplotlib 时仅绘制旧日志；不以其他方式访问被限制的 Docker。
        from fmu_tracking_render import render
        render(plot_data,output)
    else:
        flowfig,flowaxes=plt.subplots(1,2,figsize=(13,4.8),constrained_layout=True)
        dpfig,dpaxes=plt.subplots(1,2,figsize=(13,4.8),constrained_layout=True)
        for i,d in enumerate(plot_data):
            name='Fixed model policy' if d['variant']=='fixed' else 'Adaptive policy'
            flowaxes[i].step(d['tx'],d['demand'],where='post',color='#c48a36',label='Demand flow (bounded policy target)')
            flowaxes[i].plot(d['times'],d['flows'],color='#087f8c',label='Actual secondary flow')
            style(flowaxes[i],name,'Mass flow (kg/s)');flowaxes[i].legend(fontsize=8)
            dpaxes[i].step(d['tx'],d['desired'],where='post',color='#a9afb5',ls=':',label='Policy goal before rate limit')
            dpaxes[i].step(d['wt'],d['wv'],where='post',color='#c48a36',label='Applied DP setpoint')
            dpaxes[i].plot(d['times'],d['dp'],color='#087f8c',label='Measured DP')
            style(dpaxes[i],name,'Differential pressure (kPa)');dpaxes[i].legend(fontsize=8)
            for ax in (flowaxes[i],dpaxes[i]):
                for t in [10,20,30,40,50]:ax.axvline(t,color='#8b9aa7',alpha=.25,linewidth=.8)
        flowfig.suptitle('Existing closed loop: demand flow versus actual flow',fontsize=14)
        dpfig.suptitle('Existing closed loop: pressure goal, applied setpoint and actual pressure',fontsize=14)
        save_figure(flowfig,output,'flow_tracking',plt);save_figure(dpfig,output,'dp_tracking',plt)
    save(output/'tracking_summary.json',summaries)
    save(output/'plot_data.json',plot_data)
    save(output/'tracking_manifest.json',{
        'source_directory':str(source.resolve()),'source_files_sha256':{
            str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
            for v in ['fixed','adaptive'] for p in [source/v/n for n in
                ['trace.csv','runtime.sqlite','fmu_writes.json','scene.json']]},
        'alignment':'CSV measurements precede same-time decisions; error pairing uses strictly earlier ticks.',
        'step_experiment_status':'not_executed_by_tracking_only'})
    # 即使后续 FMU 执行被阻断，旧日志补图也有可独立阅读的明确状态入口。
    page='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FMU 需求与实际响应补图</title><style>body{margin:0;background:#f2f6f7;color:#17333e;
font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1250px;margin:auto;padding:28px}
section{background:white;border:1px solid #d5e3e7;border-radius:10px;margin:20px 0;padding:24px}img{width:100%;height:auto}
a{color:#087f8c}.note{background:#fff6e7;border-left:4px solid #c48a36;padding:14px}pre{white-space:pre-wrap;overflow-wrap:anywhere}
</style><main><h1>FMU：需求、下发目标与实际响应</h1>
<p>使用 sustain-fmu-closed-loop-v2 已保存的 CSV、决策数据库和实际写入日志补图；没有重跑或改写旧实验。</p>
<section><h2>需求流量与实际流量</h2><img src="flow_tracking.svg" alt="需求流量与实际流量">
<p>需求是算法限幅后的内部流量目标，本轮实际控制量是压差。需求按真实 60 秒决策时刻阶梯保持；实际量来自 15 秒采样。</p>
<p><a href="flow_tracking.png">PNG 图片</a> · <a href="flow_tracking.svg">SVG 矢量图</a></p></section>
<section><h2>压差策略目标、实际下发与实测</h2><img src="dp_tracking.svg" alt="压差目标与实测">
<p>灰线：设备上下限内、未经过变化速率限制的策略目标。橙线：Master 实际写入目标。青线：FMU 实际压差。</p>
<p>当前每 60 秒最多调整 3 kPa；灰线到橙线的差距反映目标限速，橙线到青线反映模型执行动态。
目标回读成功不代表实际压差已跟上。同时间点先测量后决策，误差计算只配对严格更早的决策。</p>
<p><a href="dp_tracking.png">PNG 图片</a> · <a href="dp_tracking.svg">SVG 矢量图</a></p></section>
<section><h2>固定负荷压差阶跃：待执行</h2><div class="note">阶跃脚本已准备；此入口仅包含旧日志补图，没有新的阶跃实验结果，不能报告 t90 或稳定时间。</div>
<p>预定三个独立工况：hold、+3 kPa、−3 kPa。各组每个热源固定 60 kW；预热 120 分钟、阶跃前保持 15 分钟、阶跃后观察 120 分钟，采样 5 秒。
外部 FlowPolicy/校准关闭，FMU 内部 PID 与滤波保留。用 hold 扣除背景漂移，仅尾段满足判据时计算 t10/t50/t90。</p>
<p>该步骤不启动 Docker。源 FMU 没有 macOS 二进制，新阶跃需在兼容的 Linux/PyFMI 环境或已有 Docker 镜像中执行。</p>
<p>准备好的工具：tools/run_fmu_diagnostics.py；运行时必须使用全新输出目录。</p></section>
<section><h2>核对数据</h2><p><a href="tracking_summary.json">跟踪误差</a> · <a href="tracking_manifest.json">源文件哈希</a> ·
<a href="fixed_tracking.csv">固定参数对齐 CSV</a> · <a href="adaptive_tracking.csv">在线策略对齐 CSV</a></p>
<p>模型仍存在负绝对压力和未完成的热源边界核对；这些图不作为真实 CDU 安全或节能证明。</p></section></main></html>'''
    status_file=output/'step_execution_status.json'
    if status_file.exists():
        status=json.loads(status_file.read_text())
        if status.get('status')=='completed_in_separate_output':
            links=[]
            for key,label in [('report','已完成的固定负荷阶跃'),('correction_report','跟踪修正与能耗对照')]:
                href=status[key]
                if not href.startswith('../') or ':' in href:raise ValueError('invalid_followup_report_path')
                links.append('<a href="'+html.escape(href,quote=True)+'">'+label+'</a>')
            start=page.index('<section><h2>固定负荷压差阶跃：待执行')
            end=page.index('<section><h2>核对数据',start)
            page=page[:start]+'<section><h2>阶跃与修正：已完成</h2><p>上方仍是原 v2 日志补图。新实验另存，详见：'+' · '.join(links)+'</p></section>'+page[end:]
    (output/'index.html').write_text(page,encoding='utf-8')


def report(output):
    plt=plot_env()
    traces={v:load_csv(output/v/'trace.csv') for v in ['hold','up','down']}
    summaries={v:json.loads((output/v/'summary.json').read_text()) for v in traces}
    times=[r['relative_s'] for r in traces['hold']]
    if any([r['relative_s'] for r in trace]!=times for trace in traces.values()):
        raise ValueError('step_time_grid_mismatch')
    if any(r['blade_input_w']!=60000 for trace in traces.values() for r in trace):
        raise ValueError('load_not_held_constant')
    result={};colors={'hold':'#8b9aa7','up':'#087f8c','down':'#c48a36'}
    fig,axes=plt.subplots(2,2,figsize=(13,9),constrained_layout=True)
    for i,(key,unit,title) in enumerate([('g1_dp_kpa','kPa','Measured differential pressure'),('g1_flow_kg_s','kg/s','Secondary mass flow')]):
        for variant,trace in traces.items():
            axes[0,i].plot([t/60 for t in times],[r[key] for r in trace],color=colors[variant],label=variant)
            if variant=='hold':continue
            m=response_metrics(times,[r[key] for r in trace],[r[key] for r in traces['hold']],summaries[variant]['step_kpa'])
            result.setdefault(variant,{})[key]=m
            corrected=[a[key]-b[key]-m['baseline_offset'] for a,b in zip(trace,traces['hold'])]
            axes[1,i].plot([t/60 for t in times],corrected,color=colors[variant],label=variant+' minus hold')
        style(axes[0,i],title,unit);style(axes[1,i],title+' change (hold-corrected)',unit)
        for ax in axes[:,i]:ax.axvline(0,color='#777',ls='--',lw=1);ax.legend()
    # 顶部实际压差图同时显示真实阶跃目标，避免再次混淆设定与测量。
    for variant in ['up','down']:
        baseline=summaries[variant]['target_kpa']-summaries[variant]['step_kpa']
        axes[0,0].step([times[0]/60,0,times[-1]/60],
                       [baseline,summaries[variant]['target_kpa'],summaries[variant]['target_kpa']],
                       where='post',color=colors[variant],ls=':',alpha=.7)
    fig.suptitle('Constant-load DP step: independent hold / +3 kPa / -3 kPa experiments',fontsize=14)
    save_figure(fig,output,'step_pressure_flow',plt)
    fig,axes=plt.subplots(1,2,figsize=(13,4.5),constrained_layout=True)
    for ax,variant in zip(axes,['up','down']):
        for key,label in [('pid_output','PID output'),('filtered_output','Filtered output'),('pump_command','Selected pump command')]:
            ax.plot([t/60 for t in times],[r[key] for r in traces[variant]],label=label,
                    linestyle='--' if key=='pump_command' else '-')
        style(ax,variant+' step: internal control signals','FMU native signal units');ax.legend(fontsize=9)
    save_figure(fig,output,'step_internal_control',plt)
    # 明确邻机与绝压边界；这些诊断不构成现场安全或节能认证。
    for variant in ['up','down']:
        result[variant]['neighbor_max_flow_delta_kg_s']=max(abs(a[f'g{g}_flow_kg_s']-b[f'g{g}_flow_kg_s'])
            for a,b in zip(traces[variant],traces['hold']) for g in range(2,6))
        result[variant]['pid_path_selected_all']=all(r['pid_path_selected']==1 for r in traces[variant])
        result[variant]['post_step_command_count']=len(json.loads((output/variant/'fmu_writes.json').read_text()))
    save(output/'step_metrics.json',result)
    rows=[]
    fmt=lambda x:'未确认' if x is None else f'{x:.2f}'
    for variant in ['up','down']:
        for key,name in [('g1_dp_kpa','实际压差'),('g1_flow_kg_s','实际流量')]:
            m=result[variant][key]
            rows.append('<tr><td>'+('升压 +3 kPa' if variant=='up' else '降压 −3 kPa')+'</td><td>'+name+'</td><td>'
                        +('满足本窗口判据' if m['settled_in_window'] else '未确认稳定')+'</td>'
                        +''.join('<td>'+fmt(m[n]/60 if m[n] is not None else None)+'</td>' for n in ['t10_s','t50_s','t90_s','settling_2pct_s'])
                        +'<td>'+fmt(m['final_delta'])+'</td></tr>')
    e=summaries['up']['experiment']
    body='''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FMU 跟踪曲线与固定负荷压差阶跃</title><style>
body{margin:0;background:#f2f6f7;color:#17333e;font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}
main{max-width:1280px;margin:auto;padding:32px 24px}section{background:white;border:1px solid #d5e3e7;border-radius:12px;padding:24px;margin:20px 0}
h1{font-size:32px}h2{font-size:23px}a{color:#087f8c}img{width:100%;height:auto}table{border-collapse:collapse;width:100%;min-width:760px;font-size:14px}
th,td{text-align:left;padding:12px;border-bottom:1px solid #d5e3e7}th{background:#edf5f5}.table{overflow:auto}.note{border-left:4px solid #c48a36;padding:12px 20px;background:#fff7e9}
@media(max-width:700px){main{padding:15px}section{padding:15px}}
</style><main><h1>FMU 跟踪曲线与固定负荷压差阶跃</h1>
<p>旧闭环日志补图 + 新的独立阶跃实验。未修改原控制算法、源 FMU 或旧实验轨迹。</p>
<section><h2>1. 需求流量与实际流量</h2><p>需求曲线来自原 Engine 决策记录，是限幅后的内部流量目标；算法实际下发压差。
按 60 秒决策周期阶梯保持，未用未来观测插值。两图分别对应固定参数与在线校准策略。</p><img src="flow_tracking.svg" alt="需求流量与实际流量">
<p><a href="flow_tracking.png">PNG</a> · <a href="fixed_tracking.csv">固定参数对齐数据</a> · <a href="adaptive_tracking.csv">在线策略对齐数据</a></p></section>
<section><h2>2. 压差目标与实际压差</h2><p>灰色虚线是设备上下限内、尚未经过单次 3 kPa 限速的策略目标；橙线是 Master 实际写入的目标；蓝绿色是实际测量。
因此可以分开看到“目标爬升受限”和“模型执行滞后”。同一时间点的 CSV 先采样、后做新决策，误差配对使用严格更早的决策。</p>
<img src="dp_tracking.svg" alt="压差目标与实际压差"><p><a href="dp_tracking.png">PNG</a> · <a href="tracking_summary.json">跟踪误差统计</a></p></section>
<section><h2>3. 固定负荷压差阶跃</h2><p>三个独立 FMU：保持目标、+3 kPa、−3 kPa。各组每个外部热源输入均固定 60 kW，阀门、邻机目标、湿球与冷却塔目标一致。
共同预热 __WARM__ 分钟，阶跃前再保持 __PRE__ 分钟，随后观察 __POST__ 分钟；通信采样 __DT__ 秒。</p>
<p>关闭外部 FlowPolicy 与在线学习，直接由独占 Master 一次改变第 1 组压差输入；保留 FMU 内部 PID/滤波/本地控制。
这是模型辨识入口，不经过现场命令网关，也不是修改现场限速。</p>
<img src="step_pressure_flow.svg" alt="固定负荷压差与流量阶跃响应"><p><a href="step_pressure_flow.png">PNG</a></p>
<div class="table"><table><thead><tr><th>实验</th><th>观测</th><th>尾段稳定</th><th>t10 分钟</th><th>t50 分钟</th><th>t90 分钟</th><th>2% 稳定时间 分钟</th><th>末段变化量</th></tr></thead><tbody>__ROWS__</tbody></table></div>
<p>先用同时间 hold 轨迹扣除背景漂移，再计算响应。末段变化量单位分别为 kPa、kg/s。
尾段取最后 20 分钟，要求其波动范围与前一个 20 分钟均值差均小于响应幅度的 2%（最低容差 1e-5）。
只有通过才报告到达比例时间；时间取首个采样越过阈值，分辨率 __DT__ 秒。稳定时间只对本观察窗口有效。</p></section>
<section><h2>4. 内部 PID 与滤波路径</h2><img src="step_internal_control.svg" alt="PID输出、滤波输出与选中泵控制信号">
<p>内部信号按 FMU 原生单位显示，不将其擅自解释为泵转速百分比。路径选择状态、PID 参数与滤波参数见各工况 summary.json。</p></section>
<section><h2>边界与可追溯文件</h2><div class="note">源模型仍存在负绝对压力与未完成的热负荷边界核对。该实验识别的是此 FMU 的响应，不能直接作为真实 CDU 整定值，也不证明节能。</div>
<p><a href="step_metrics.json">完整阶跃指标</a> · <a href="manifest.json">输入、源结果与代码哈希</a> · <a href="scene.json">参考场景</a></p>
<p><a href="hold/trace.csv">hold 轨迹</a> · <a href="up/trace.csv">升压轨迹</a> · <a href="down/trace.csv">降压轨迹</a></p>
<p><a href="hold/summary.json">hold 摘要</a> · <a href="up/summary.json">升压摘要</a> · <a href="down/summary.json">降压摘要</a></p></section></main></html>'''
    for k,v in {'__ROWS__':''.join(rows),'__WARM__':e['warmup_s']/60,'__PRE__':e['pre_step_s']/60,
                '__POST__':e['post_step_s']/60,'__DT__':e['communication_step_s']}.items():body=body.replace(k,str(v))
    if not (output/'flow_tracking.svg').exists():
        start=body.index('<section><h2>1. 需求流量')
        end=body.index('<section><h2>3. 固定负荷',start)
        body=body[:start]+body[end:]
        body=body.replace('旧闭环日志补图 + 新的独立阶跃实验。','独立固定负荷阶跃实验。')
    (output/'index.html').write_text(body,encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False,indent=2))
