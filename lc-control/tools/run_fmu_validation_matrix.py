"""运行额外 FMU 参数/工况矩阵，汇集已验证参考证据；不连接设备。

--config 指定设备运行场景；--matrix 指定 experiments/ 下的工况/参数组合，
矩阵包含 cases/variants，不是可交给 lc_control validate 的场景。
所有策略共享初态/边界/时域。Kp 对照关闭在线晋升，避免混入
模型更新的影响。任一失败保存日志与失败状态，禁止将未完成运行当作合格结果。
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import uuid
from run_sustain_fmu import ROOT, docker_command, save, validate_experiment
from lc_control.configuration import load_scene

DEFAULT_MATRIX = ROOT / 'experiments/fmu_validation_matrix.json'


def suite(args):
    output=Path(args.output).resolve()
    if output.exists():raise ValueError('fresh_output_required')
    scene=load_scene(args.config);matrix=json.loads(Path(args.matrix).read_text())
    for c in matrix['cases']:
        if not c['id'].replace('_','').isalnum():raise ValueError('invalid_case_id')
    if len({c['id'] for c in matrix['cases']})!=len(matrix['cases']):raise ValueError('duplicate_case')
    fmu=Path(args.fmu).resolve();fmu_hash=hashlib.sha256(fmu.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    save(output/'base_scene.json',scene);save(output/'matrix.json',matrix)
    manifest={'fmu_sha256':fmu_hash,'runs':{},'references':{},'code_sha256':{
        str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest()
        for p in list((ROOT/'lc_control').glob('*.py'))+list((ROOT/'tools').glob('*.py'))}}
    # 新实验独立运行。旧证据复制到统一目录，先核验哈希再清理旧入口。
    for name,source in [('short','sustain-fmu-correction-20260915-001'),
                        ('long','sustain-fmu-correction-long-20260915-001'),
                        ('step','sustain-fmu-diagnostics-20260915-001')]:
        src=ROOT/'outputs'/source
        if not src.exists():src=Path(args.reference_root)/'reference'/name
        dst=output/'reference'/name
        dst.mkdir(parents=True)
        variants=['hold','up','down'] if name=='step' else ['baseline','legacy','gain_only','model_only','corrected']
        files={}
        for variant in variants:
            summary=json.loads((src/variant/'summary.json').read_text())
            if not summary['complete'] or summary['contract']['fmu_sha256']!=fmu_hash:
                raise ValueError('reference_mismatch:'+name+'/'+variant)
            shutil.copytree(src/variant,dst/variant)
            for p in (dst/variant).iterdir():
                if p.is_file():files[str(p.relative_to(dst))]=hashlib.sha256(p.read_bytes()).hexdigest()
        for filename in ['manifest.json','step_metrics.json','short_tracking.svg','long_tracking.svg',
                         'step_pressure_flow.svg','step_internal_control.svg']:
            if (src/filename).exists():shutil.copy2(src/filename,dst/filename)
        manifest['references'][name]={'original_directory':str(src),'files_sha256':files}
    for case in matrix['cases']:
        for variant,kp in matrix['variants'].items():
            runscene=copy.deepcopy(scene);e=runscene['extensions']['sustain_fmu_experiment']
            e.update(load_phase_s=case['phase_s'],blade_input_profile_w=case['profile_w'],
                     evaluation_s=case['phase_s']*len(case['profile_w']))
            runscene['devices']['CDU_01']['policy']['adaptive_enabled']=False
            if kp is not None:runscene['devices']['CDU_01']['policy']['dp_feedback_gain']=kp
            validate_experiment(runscene)
            run_id=case['id']+'/'+variant
            folder=output/'cases'/case['id'];folder.mkdir(parents=True,exist_ok=True)
            config=folder/(variant+'_config.json');save(config,runscene)
            name='lc-fmu-matrix-'+uuid.uuid4().hex[:12]
            command=docker_command(args.image,fmu,output,
                ['--worker','baseline' if variant=='baseline' else 'fixed','--fmu','/model/reference.fmu',
                 '--config','/results/'+str(config.relative_to(output)),
                 '--output','/results/cases/'+run_id],name)
            print('Running '+run_id,flush=True)
            try:
                done=subprocess.run(command,capture_output=True,text=True,timeout=300,cwd='/tmp')
            except (subprocess.TimeoutExpired,KeyboardInterrupt):
                subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
                manifest['runs'][run_id]={'complete':False,'error':'interrupted_or_timeout'}
                save(output/'manifest.json',manifest);raise
            (folder/(variant+'.log')).write_text(done.stdout+'\n'+done.stderr)
            summary_path=folder/variant/'summary.json'
            summary=json.loads(summary_path.read_text()) if summary_path.exists() else {}
            manifest['runs'][run_id]={'exit_code':done.returncode,'complete':summary.get('complete',False)}
            save(output/'manifest.json',manifest)
            if done.returncode or not summary.get('complete'):
                raise RuntimeError(run_id+' failed: '+(done.stdout+done.stderr)[-2000:])
    print(output,flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--fmu',required=True);p.add_argument('--output',required=True)
    p.add_argument('--config',default=str(ROOT/'examples/sustain_fmu_experiment.json'))
    p.add_argument('--matrix',default=str(DEFAULT_MATRIX),
                   help='工况/参数组合矩阵（不是运行场景）；默认 experiments/fmu_validation_matrix.json')
    p.add_argument('--image',default='sustain-lc:amd64')
    p.add_argument('--reference-root',default=str(ROOT/'outputs/fmu-validation-20260915'),
                   help='统一目录中的已验证参考数据；旧目录清理后从这里复制')
    suite(p.parse_args())
