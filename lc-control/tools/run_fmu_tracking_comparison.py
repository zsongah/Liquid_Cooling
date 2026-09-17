"""同一 FMU、初态、负荷、设备限制下的消融对照；不调 FMU 内部 PID。

旧算法、只校准 gain、修正水力模型、模型 + 有界反馈分别独立运行。
水力初值只由先前固定负荷 hold 辨识，不使用对照实验未来负荷。
"""
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import uuid

from run_sustain_fmu import ROOT, docker_command, save

VARIANTS = {'baseline':'baseline', 'legacy':'adaptive', 'gain_only':'fixed',
            'model_only':'fixed', 'corrected':'adaptive'}


def suite(args):
    output=Path(args.output).resolve()
    if output.exists():raise ValueError('fresh_output_required')
    source=Path(args.identification).resolve()
    hold=source/'hold/trace.csv'
    identification_summary=json.loads((source/'hold/summary.json').read_text())
    if not identification_summary.get('complete') or identification_summary.get('fmi_statuses') != [0]:
        raise ValueError('identification_run_incomplete')
    if hashlib.sha256(Path(args.fmu).read_bytes()).hexdigest() != identification_summary['contract']['fmu_sha256']:
        raise ValueError('identification_fmu_mismatch')
    with hold.open() as f:rows=list(csv.DictReader(f))
    end=float(rows[-1]['relative_s'])
    tail=[r for r in rows if float(r['relative_s'])>end-1200]
    linear=statistics.mean(float(r['g1_flow_kg_s'])/float(r['g1_dp_kpa']) for r in tail)
    sqrt=statistics.mean(float(r['g1_flow_kg_s'])/float(r['g1_dp_kpa'])**.5 for r in tail)
    original=json.loads(Path(args.config).read_text())
    fmu=Path(args.fmu).resolve()
    output.mkdir(parents=True)
    manifest={'identification_hold_sha256':hashlib.sha256(hold.read_bytes()).hexdigest(),
              'identification_source':str(source),'linear_gain':linear,'sqrt_gain':sqrt,
              'runs':{},'code_sha256':{str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
              for p in list((ROOT/'lc_control').glob('*.py'))+list((ROOT/'tools').glob('*.py'))}}
    for name,worker in VARIANTS.items():
        scene=copy.deepcopy(original)
        p=scene['devices']['CDU_01']['policy']
        if name=='gain_only':p['flow_per_sqrt_kpa']=sqrt
        if name in ('model_only','corrected'):
            p.pop('flow_per_sqrt_kpa',None);p.pop('gain_bounds',None)
            p['hydraulic_model']={'exponent':1.0,'gain':linear,'gain_bounds':[.04,.09]}
            p['calibration_max_dp_error_kpa']=.5
            p['dp_feedback_gain']=2.0 if name=='corrected' else 0.0
        if args.long_hold:
            scene['extensions']['sustain_fmu_experiment'].update(
                evaluation_s=10800,load_phase_s=3600,blade_input_profile_w=[60000,75000,60000])
        config_path=output/(name+'_config.json');save(config_path,scene)
        cname='lc-fmu-compare-'+uuid.uuid4().hex[:12]
        command=docker_command(args.image,fmu,output,
            ['--worker',worker,'--fmu','/model/reference.fmu','--config','/results/'+config_path.name,
             '--output','/results/'+name],cname)
        print('Running '+name,flush=True)
        try:
            run=subprocess.run(command,capture_output=True,text=True,timeout=240,cwd='/tmp')
        except (subprocess.TimeoutExpired,KeyboardInterrupt):
            subprocess.run(['docker','rm','-f',cname],capture_output=True,timeout=20)
            raise
        (output/(name+'.log')).write_text(run.stdout+'\n'+run.stderr)
        manifest['runs'][name]={'exit_code':run.returncode}
        save(output/'manifest.json',manifest)
        if run.returncode:raise RuntimeError(name+' failed: '+(run.stdout+run.stderr)[-2500:])
    print(output,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fmu',required=True)
    p.add_argument('--config',default=str(ROOT/'examples/sustain_fmu_experiment_legacy.json'))
    p.add_argument('--identification',required=True)
    p.add_argument('--output',required=True)
    p.add_argument('--image',default='sustain-lc:amd64')
    p.add_argument('--long-hold',action='store_true')
    suite(p.parse_args())
