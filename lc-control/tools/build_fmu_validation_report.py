"""单文件 FMU 总报告：内嵌全部图、核心配置、指标和完整场景 JSON。

HTML 可单独拷走阅读，无 CDN；原始 CSV/SQLite 保留在相邻目录用于追溯。
旧模型只作为受控消融证据展示，不标记为推荐配置。不能跨负荷直接比较 kWh。
"""
import argparse
import base64
import html
import json
from pathlib import Path
import statistics
from fmu_correction_report import load_run, collect, tracking_figure
from fmu_diagnostics_report import load_csv, plot_env, save_figure, save, style

COLORS={'baseline':'#87939e','kp0':'#527eb1','kp1':'#b88632','kp2':'#088578',
        'legacy':'#b56d50','gain_only':'#ab9e80','model_only':'#527eb1','corrected':'#088578'}
NAMES={'baseline':'固定压差','kp0':'Kp=0','kp1':'Kp=1','kp2':'Kp=2','legacy':'原在线算法（对照）',
       'gain_only':'仅校正 gain（对照）','model_only':'线性模型 Kp=0','corrected':'线性模型 Kp=2'}
ENG={'baseline':'Fixed DP','kp0':'Kp=0','kp1':'Kp=1','kp2':'Kp=2','legacy':'Legacy','gain_only':'Gain-only','model_only':'Linear Kp=0','corrected':'Linear Kp=2'}


def matched(runs):
    base=runs['baseline']
    for r in runs.values():
        if r['scene']['extensions']!=base['scene']['extensions']:raise ValueError('experiment_mismatch')
        for key in ['controls','guards','initial_setpoints','initial_observations']:
            if r['scene']['devices']['CDU_01'][key]!=base['scene']['devices']['CDU_01'][key]:raise ValueError('constraint_mismatch')
        if [(x['time_s'],x['blade_input_w']) for x in r['trace']]!=[(x['time_s'],x['blade_input_w']) for x in base['trace']]:raise ValueError('time_input_mismatch')


def enrich(runs):
    matched(runs)
    for v,r in runs.items():
        e=r['summary']['experiment'];m=r['metrics'];dt=e['communication_step_s']
        m['above_return_soft_limit_min']=sum(x['g1_return_degC']>42 for x in r['trace'])*dt/60
        m['pump_change_vs_baseline_pct']=100*(m['pump_kwh']/runs['baseline']['metrics']['pump_kwh']-1)
        m['control_below_minimum_decisions']=sum('control_below_minimum' in d.get('warnings',[]) for _,d in r['decisions'])
        m['control_capacity_saturated_decisions']=sum('control_capacity_saturated' in d.get('warnings',[]) for _,d in r['decisions'])


def overview(runs,out,name):
    plt=plot_env();fig,axes=plt.subplots(4,2,figsize=(14,17),constrained_layout=True)
    for v,run in runs.items():
        color=COLORS[v];rows=run['trace'];t=[r['elapsed_s']/60 for r in rows]
        for ax,key,scale,title,unit in [
            (axes[0,0],'blade_input_w',.001,'FMU heat input per source','kW'),
            (axes[1,1],'g1_return_degC',1,'CDU 1 return temperature','degC'),
            (axes[2,0],'g1_pump_w',.001,'CDU 1 pump electrical power','kW'),
            (axes[2,1],'represented_cooling_w',.001,'All represented cooling electrical power','kW')]:
            if key=='blade_input_w' and v!='baseline':continue
            if key=='blade_input_w':
                ax.step([0]+t,[rows[0][key]*scale]+[r[key]*scale for r in rows],
                        where='pre',color=color,label=ENG[v],lw=1.5)
            else:
                ax.plot(t,[r[key]*scale for r in rows],color=color,label=ENG[v],lw=1.5)
            style(ax,title,unit)
        axes[0,1].plot(t,[r['g1_flow_kg_s'] for r in rows],color=color,label=ENG[v],lw=1.6)
        axes[1,0].plot(t,[r['g1_dp_kpa'] for r in rows],color=color,label=ENG[v],lw=1.6)
        if run['decisions']:
            tx=[(at-run['warmup'])/60 for at,d in run['decisions']]+[t[-1]]
            ds=[d for _,d in run['decisions']];ds=ds+[ds[-1]]
            axes[0,1].step(tx,[d['target_flow_kg_s'] for d in ds],where='post',color=color,ls='--',lw=.9,alpha=.65)
            # input in row describes the preceding communication interval (pre step).
            axes[1,0].step([0]+t,[run['first']['setpoints']['cdu.dp_sp']['value']]+[r['g1_dp_input_psi']*6.89475728 for r in rows],where='pre',color=color,ls='--',lw=.9,alpha=.65)
            axes[3,0].plot([r['elapsed_s']/60 for r in run['pairs']],
                          [r['demand_flow_kg_s']-r['actual_flow_kg_s'] for r in run['pairs']],color=color,label=ENG[v])
            # 模型的指数不同，不能在同一 gain 轴混用 kg/s/sqrt(kPa) 和 kg/s/kPa。
            exponent=run['scene']['devices']['CDU_01']['policy'].get('hydraulic_model',{}).get('exponent',.5)
            axes[3,1].plot(t,[r['gain']*190**exponent for r in rows],color=color,label=ENG[v])
    for ax,title,unit in [(axes[0,1],'Flow: measured (solid), demand (dashed)','kg/s'),
                         (axes[1,0],'DP: measured (solid), applied command (dashed)','kPa'),
                         (axes[3,0],'Flow error: demand minus actual','kg/s'),
                         (axes[3,1],'Hydraulic model prediction at 190 kPa','kg/s')]:style(ax,title,unit)
    axes[1,1].axhline(42,color='#b65d56',ls=':',lw=1)
    axes[3,0].axhline(0,color='#8b9aa7',lw=.6)
    for ax in axes.flat:ax.legend(fontsize=7,ncol=2,loc='best')
    save_figure(fig,out,name,plt)


def plot_summary(cases,out):
    plt=plot_env();fig,axes=plt.subplots(2,2,figsize=(13,9),constrained_layout=True)
    for ax,(case,runs) in zip(axes.flat,cases):
        for v in ['kp0','kp1','kp2']:
            m=runs[v]['metrics'];ax.scatter(m['flow_mae_kg_s'],m['pump_kwh'],s=75,color=COLORS[v])
            ax.annotate(ENG[v],(m['flow_mae_kg_s'],m['pump_kwh']),xytext=(6,7),textcoords='offset points')
        ax.set(title=case['id'],xlabel='Flow MAE (kg/s) - lower is better',ylabel='CDU 1 pump energy (kWh)')
        ax.grid(alpha=.2);ax.margins(.3)
    save_figure(fig,out,'feedback_tradeoff',plt)


def embed(path,alt):
    mime='image/svg+xml' if path.suffix=='.svg' else 'image/png'
    data=base64.b64encode(path.read_bytes()).decode()
    return '<img loading="lazy" alt="'+html.escape(alt)+'" src="data:'+mime+';base64,'+data+'">'


def metrics_table(runs):
    body=[]
    for v,r in runs.items():
        m=r['metrics'];fmt=lambda x:'—' if x is None else f'{x:.3f}'
        cells=[NAMES[v]]+[fmt(m[k]) for k in ['flow_mae_kg_s','applied_dp_mae_kpa','pump_kwh','represented_cooling_kwh','peak_return_degC','above_return_soft_limit_min']]
        body.append('<tr>'+''.join('<td>'+s+'</td>' for s in cells)+'</tr>')
    headers=['策略','流量 MAE<br>kg/s','命令压差 MAE<br>kPa','CDU 1 泵电量<br>kWh','模型冷却电量<br>kWh','最高回液<br>°C','回液 >42°C<br>min']
    return '<div class="table"><table><thead><tr>'+''.join('<th>'+s+'</th>' for s in headers)+'</tr></thead><tbody>'+''.join(body)+'</tbody></table></div>'


def build(root):
    matrix=json.loads((root/'matrix.json').read_text());base=json.loads((root/'base_scene.json').read_text())
    out=root/'figures';out.mkdir(exist_ok=True)
    cases=[];allmetrics={};configs={}
    for case in matrix['cases']:
        runs={v:load_run(root/'cases'/case['id']/v) for v in matrix['variants']}
        enrich(runs);cases.append((case,runs));overview(runs,out,case['id'])
        allmetrics[case['id']]={v:r['metrics'] for v,r in runs.items()}
        configs[case['id']]={v:r['scene'] for v,r in runs.items()}
    refs={}
    for name in ['short','long']:
        runs=collect(root/'reference'/name);enrich(runs);refs[name]=runs
        allmetrics['reference_'+name]={v:r['metrics'] for v,r in runs.items()}
        configs['reference_'+name]={v:r['scene'] for v,r in runs.items()}
        overview(runs,out,'reference_'+name)
        tracking_figure(runs,out,'reference_'+name+'_tracking')
    plot_summary(cases,out)
    save(root/'all_metrics.json',allmetrics)
    save(root/'all_scenes.json',configs)
    findings=[]
    for case,runs in cases:
        best=min(['kp0','kp1','kp2'],key=lambda v:runs[v]['metrics']['flow_mae_kg_s'])
        m=runs[best]['metrics'];zero=runs['kp0']['metrics']
        pct=100*(1-m['flow_mae_kg_s']/zero['flow_mae_kg_s'])
        findings.append('<li><strong>'+case['title']+'</strong>：本次三个反馈配置中 '+NAMES[best]+' 的流量 MAE 最低，为 '+f"{m['flow_mae_kg_s']:.3f}"+' kg/s；比 Kp=0 低 '+f'{pct:.1f}%'+
                        '，CDU 泵电量相对固定压差基线变化 '+f"{m['pump_change_vs_baseline_pct']:+.1f}%"+'。</li>')
    blocks=[]
    for case,runs in cases:
        label=case['id'];e=runs['kp2']['summary']['experiment']
        limits=runs['kp2']['metrics']
        blocks.append('<section id="'+label+'"><span class="tag">新增 · 4 个独立运行</span><h2>'+case['title']+'</h2><p>热源输入序列：'+
                      ' → '.join(f'{v/1000:g}' for v in case['profile_w'])+' kW/热源；总评估 '+f"{e['evaluation_s']/60:g}"+' min。其余四组各热源保持 60 kW。</p>'+metrics_table(runs)+
                      '<p class="muted">Kp=2 的 '+str(len(runs['kp2']['decisions']))+' 次决策中，限速 '+str(limits['rate_limited_decisions'])+' 次，稳态需求低于最低控制能力 '+str(limits['control_below_minimum_decisions'])+' 次，高于最高控制能力 '+str(limits['control_capacity_saturated_decisions'])+' 次。次数可能重叠。</p>'+embed(out/(label+'.svg'),case['title']+'八项结果')+'</section>')
    legacy=[]
    for name,title in [('short','原条件 · 每 10 min 变负荷'),('long','收敛验证 · 每 60 min 变负荷')]:
        legacy.append('<section id="reference-'+name+'"><span class="tag">保留 · 修正前后对照证据</span><h2>'+title+'</h2><p>原算法、仅修正 gain 是消融对照，不是推荐配置。保留同条件结果，避免只展示修正后的有利曲线。</p>'+metrics_table(refs[name])+embed(out/('reference_'+name+'_tracking.svg'),title+'需求与实际跟踪')+
                      '<details><summary>展开全部 8 项结果：热源、流量、压差、温度、功率、误差与模型</summary>'+embed(out/('reference_'+name+'.svg'),title+'全量结果')+'</details></section>')
    step=root/'reference/step';sm=json.loads((step/'step_metrics.json').read_text())
    step_configs={v:{'conditions':json.loads((step/v/'conditions.json').read_text()),
                    'internal_parameters':json.loads((step/v/'summary.json').read_text())['internal_parameters']}
                  for v in ['hold','up','down']}
    step_rows=''.join('<tr><td>'+v+'</td><td>'+f"{sm[v]['g1_dp_kpa']['t90_s']/60:.2f}"+'</td><td>'+f"{sm[v]['g1_dp_kpa']['settling_2pct_s']/60:.2f}"+'</td><td>'+f"{sm[v]['g1_flow_kg_s']['steady_gain']:.5f}"+'</td></tr>' for v in ['up','down'])
    p=base['devices']['CDU_01']['policy'];e=base['extensions']['sustain_fmu_experiment'];cap=base['devices']['CDU_01']['controls']['cdu.dp_sp']
    core={'controller':p,'device_capability':cap,'guard_limits':base['devices']['CDU_01']['guards'],
          'fixed_boundaries':{k:v for k,v in e.items() if k not in ['evaluation_s','blade_input_profile_w','load_phase_s']},
          'matrix_policy_override':{'adaptive_enabled':False,'dp_feedback_gain':[0,1,2]},
          'topology':base['control_domains']}
    config_details=''.join('<details><summary>'+html.escape(case+'/'+v)+' · 完整实际配置</summary><pre>'+html.escape(json.dumps(scene,ensure_ascii=False,indent=2))+'</pre></details>' for case,variants in configs.items() for v,scene in variants.items())
    cleanup=json.loads((root/'cleanup_manifest.json').read_text()) if (root/'cleanup_manifest.json').exists() else {'status':'尚未清理旧目录'}
    nav=''.join('<a href="#'+c['id']+'">'+c['title']+'</a>' for c,_ in cases)
    page='''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>FMU 验证总览 · 工况、配置与全部结果</title>
<style>*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f1f5f6;color:#183746;font:16px/1.8 -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif}main{max-width:1300px;margin:auto;padding:42px 24px}h1{font-size:38px;line-height:1.35;margin:14px 0}h2{font-size:25px}h3{font-size:19px}.eyebrow,.tag{color:#088578;font-size:13px;letter-spacing:1px}.muted{color:#617681;font-size:14px}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}.card{background:#fff;border:1px solid #d4e1e5;border-radius:12px;padding:22px}.card strong{font-size:33px;display:block;color:#088578}section{background:white;border:1px solid #d4e1e5;border-radius:14px;padding:26px;margin:25px 0;scroll-margin-top:15px}img{width:100%;display:block;height:auto;margin:18px 0}.table{overflow:auto}table{border-collapse:collapse;width:100%;min-width:850px;font-size:14px}td,th{border-bottom:1px solid #dce6ea;padding:11px;text-align:left}th{background:#edf5f5}.note{background:#fff5e4;border-left:4px solid #bd8d3e;padding:16px 20px}a{color:#088578}nav{display:flex;flex-wrap:wrap;gap:8px;margin:25px 0}nav a{padding:8px 12px;background:#e5f0ef;border-radius:7px;text-decoration:none}pre{background:#f4f7f8;padding:18px;white-space:pre-wrap;overflow-wrap:anywhere;font-size:13px}details{margin:15px 0;border:1px solid #dce6ea;border-radius:8px;padding:12px 16px}summary{cursor:pointer;font-weight:600}li{margin:8px 0}button{border:1px solid #b9cecf;border-radius:7px;background:#eaf5f2;padding:10px 18px;color:#18564d;font:inherit;cursor:pointer}@media(max-width:650px){main{padding:22px 12px}h1{font-size:29px}section{padding:16px}.cards{grid-template-columns:1fr}}@media print{details>*{display:block}nav,button{display:none}section{break-inside:auto}body{background:white}main{padding:0}}</style></head><body><main>
<span class="eyebrow">LC CONTROL / FMU VALIDATION / 2026-09-15</span><h1>一份报告，看清配置、跟踪效果与能耗代价</h1>
<p>新增 4 类工况 × 4 种策略；汇入先前 10 组消融对照和 3 组固定负荷阶跃。全部为实际运行参考 FMU 的结果，无硬件。图像、配置与指标均内嵌，HTML 可单独复制阅读。</p>
<div class="cards"><div class="card">新增独立运行<strong>16 组</strong>5 / 10 / 30 / 60 min 不同负荷周期</div><div class="card">本页汇总证据<strong>29 组</strong>26 组控制 + 3 组辨识</div><div class="card">图像面板<strong>66 个</strong>11 组图，涵盖主要相关信号</div></div>
<nav><a href="#findings">结果判断</a><a href="#config">核心配置</a>__NAV__<a href="#reference-short">原条件对照</a><a href="#reference-long">长阶段对照</a><a href="#step">固定负荷阶跃</a><a href="#cleanup">结果清理</a></nav>
<button onclick="document.querySelectorAll('details').forEach(x=>x.open=true)">展开所有补充图与配置</button>
<section id="findings"><h2>这批实验能得出什么</h2><ul>__FINDINGS__</ul>
<p>“误差最低”不等于能耗最低，也不是现场推荐参数。Kp=2 是当前参考配置；这里同时保留 Kp=0/1 结果，不根据单个工况自动改默认值。本轮额外对照关闭在线晋升，不能据此声称在线学习改善。</p>
__TRADEOFF__<div class="note">所有电量只在同一工况内比较。快速负荷可能受慢内环限制；高低负荷可能超出可达流量范围。参考 FMU 仍存在负绝对压力，热源输入与 CDU 总液冷热负荷尺度尚未核对，不能作为厂家实机安全或节能认证。</div></section>
<section id="config"><h2>核心配置与对照规则</h2>
<ul><li>控制域：一个 Master 独占五组 FMU，仅控制第 1 组 CDU 压差；四个邻组保持固定。</li>
<li>水力模型 m = 0.0646448823 × Δp，m 单位 kg/s，Δp 单位 kPa；初值由固定负荷 hold 辨识。新增 Kp 分别 0、1、2，水力参数冻结。</li>
<li>压差候选 = 稳态所需压差 + Kp ×（稳态所需压差 − 实际压差）。原生单位 psi 在适配器统一换算为 kPa。</li>
<li>压差范围 172.369–262.001 kPa；每 60 s 最多改变 3 kPa；固定压差基线 189.606 kPa。内部 PID k=0.1、Ti=100 s、Td=30 s，二阶滤波 f_cut=0.001 Hz，未修改。</li>
<li>共同预热 60 min，通信步长 15 s，控制周期 60 s；目标温差 8.5 K，回液软限 42°C。供温设定 28°C、湿球 20°C，支路阀门 0.33/0.33/0.34。阶跃辨识使用独立的 120 min 预热与 5 s 采样。</li>
<li>热源曲线是第 1 组每个外部热源的输入，不等同 CDU 总热量；控制器使用实测流量/温差估算负荷，不读取未来扰动。新增所有策略在相同工况使用相同时间网格、初态和安全限制。</li></ul>
<details><summary>展开核心配置 JSON</summary><pre>__CORE__</pre></details>
<details><summary>展开 26 个控制实验的完整实际配置</summary>__CONFIGS__</details></section>
__BLOCKS____LEGACY__
<section id="step"><span class="tag">保留 · 独立辨识证据</span><h2>固定负荷 hold / +3 kPa / −3 kPa</h2>
<p>外部控制算法关闭，内部 PID/滤波保留。先扣除同时间 hold 漂移，再评估阶跃响应；只有末段满足稳定判据才报告 t90。</p>
<div class="table"><table><tr><th>阶跃</th><th>压差 t90 / min</th><th>2% 稳定时间 / min</th><th>流量变化 / 压差变化 kg/(s·kPa)</th></tr>__STEP_ROWS__</table></div>
__STEP1____STEP2__<details><summary>阶跃实验条件与内部 PID/滤波参数</summary><pre>__STEP_CONFIG__</pre></details><p>内环约需 40 min 完成 90% 响应，说明仅调整外环 gain 无法保证 5–10 min 负荷紧密跟踪。不能将这组参数直接套用到真实 CDU。</p></section>
<section><h2>指标口径与使用说明</h2><p>流量 MAE 来自实际决策目标与后续采样配对；同一时间点先测量后决策，因此不将新决策倒配给旧测量。不同策略使用同一热规则，但各自观测不同，需求曲线不是完全相同的外部给定。虚线为需求/实际下发目标，实线为实测。压差命令回读不代表实际量达到目标。</p>
<p>电量使用原运行中端点功率梯形积分；“模型冷却”含五台 CDU 泵、一次侧泵、塔泵和塔风机，不是全 AIDC 电耗。回液超过 42°C 是软目标未满足，不等同 55°C 配置保护边界失守。图中水力模型统一显示在 190 kPa 的流量预测，避免混用不同模型 gain 单位。</p>
<p><a href="all_metrics.json">全部指标 JSON</a> · <a href="all_scenes.json">全部场景 JSON</a> · <a href="matrix.json">新增工况矩阵</a> · <a href="manifest.json">FMU、代码与参考数据哈希</a></p>
<p>原始轨迹、决策 SQLite、命令日志、条件与摘要保留于 cases/ 和 reference/。外部数据链接需要保留这些伴随文件，单独复制 HTML 仍可查看所有图表与内嵌配置。</p>
<pre>python3 tools/run_fmu_validation_matrix.py --fmu /Users/song/Developer/sustain-lc/LC_Frontier_5Cabinet_4_17_25.fmu --output outputs/my-matrix-001
# 容器内或有 matplotlib 的环境生成本页：
python tools/build_fmu_validation_report.py --output outputs/my-matrix-001</pre></section>
<section id="cleanup"><h2>结果清理记录</h2><p>保留有效参考证据与当前对照。重复、旧版本、单位错误和未验证结果退出当前结果目录；不是因为曲线不好看而删除失败证据。</p><pre>__CLEANUP__</pre></section>
</main></body></html>'''
    replacements={'__NAV__':nav,'__FINDINGS__':''.join(findings),'__TRADEOFF__':embed(out/'feedback_tradeoff.svg','反馈强度的误差与泵耗取舍'),
      '__CORE__':html.escape(json.dumps(core,ensure_ascii=False,indent=2)),'__CONFIGS__':config_details,'__BLOCKS__':''.join(blocks),'__LEGACY__':''.join(legacy),
      '__STEP_ROWS__':step_rows,'__STEP1__':embed(step/'step_pressure_flow.svg','压差和流量阶跃'),
      '__STEP2__':embed(step/'step_internal_control.svg','内部 PID 与滤波控制信号'),
      '__STEP_CONFIG__':html.escape(json.dumps(step_configs,ensure_ascii=False,indent=2)),
      '__CLEANUP__':html.escape(json.dumps(cleanup,ensure_ascii=False,indent=2))}
    for k,v in replacements.items():page=page.replace(k,v)
    (root/'index.html').write_text(page,encoding='utf-8')
    print(root/'index.html',flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',required=True)
    build(Path(p.parse_args().output))
