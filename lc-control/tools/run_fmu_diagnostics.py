"""已有闭环跟踪补图 + 固定负荷压差阶跃；只用于独占参考 FMU 的实验。

宿主使用 --docker 运行已有 PyFMI 镜像。三种工况各自独立进程，不连接硬件。
源代码、旧结果、FMU 只读；新目录保存轨迹、内部控制量、参数、哈希与分析。
阶跃直接施加到 Master，关闭外部 FlowPolicy，保留 FMU 本地 PID/滤波器。
"""
import argparse
import copy
import csv
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lc_control.configuration import load_scene
from lc_control.sustain_fmu import SustainFmuMaster, KPA_PER_PSI, block_prefix, inspect_contract
from run_sustain_fmu import check_envelope

EXPERIMENT={"warmup_s":7200,"pre_step_s":900,"post_step_s":7200,
            "communication_step_s":5,"step_kpa":3.0,"blade_input_w":60000.0}
VARIANTS={"hold":0,"up":1,"down":-1}
INTERNALS={"pid_output":"controls.PID_CDUP.y", "filtered_output":"controls.filter_CDUP.y",
           "pump_command":"controls.switch_setpoint.y", "pid_path_selected":"controls.switch_setpoint.u2",
           "pid_setpoint_psi":"controls.PID_CDUP.u_s", "pid_measurement_psi":"controls.PID_CDUP.u_m"}


def save(path,value):
    path.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)+'\n',encoding='utf-8')


def worker(fmu,scene,output,variant):
    output.mkdir(parents=True,exist_ok=False)
    config=copy.deepcopy(scene["extensions"]["sustain_fmu_experiment"])
    e=EXPERIMENT
    config.update(warmup_s=e["warmup_s"],evaluation_s=e["pre_step_s"]+e["post_step_s"],
                  background_blade_input_w=e["blade_input_w"],communication_step_s=e["communication_step_s"])
    save(output/'conditions.json',{"master_config":config,"test":e,"variant":variant})
    master=SustainFmuMaster(fmu,config)
    origin=e["warmup_s"]+e["pre_step_s"]
    target=config['baseline_dp_psi']*KPA_PER_PSI+VARIANTS[variant]*e['step_kpa']
    records=[]
    summary={"complete":False,"variant":variant,"step_kpa":VARIANTS[variant]*e['step_kpa'],
             "experiment":e,"target_kpa":target,"contract":master.contract}
    p=block_prefix(1)+'.cdu[1].'
    def read_record():
        record=dict(master.last_record)
        record['relative_s']=master.time-origin
        for key,name in INTERNALS.items():record[key]=master.value(p+name)
        check_envelope(record,config)
        return record
    try:
        summary['internal_parameters']={name:master.value(p+name) for name in
            ['controls.PID_CDUP.k','controls.PID_CDUP.Ti','controls.PID_CDUP.Td',
             'controls.filter_CDUP.f_cut','controls.filter_CDUP.order']}
        for _ in range(round(e['warmup_s']/e['communication_step_s'])):
            master.advance(e['communication_step_s'],e['blade_input_w'])
        records.append(read_record())
        for _ in range(round(e['pre_step_s']/e['communication_step_s'])):
            master.advance(e['communication_step_s'],e['blade_input_w'])
            records.append(read_record())
        # t=0 记录的是阶跃前状态；命令随后施加，影响 t=5 s 及以后的记录。
        master.write_dp(target)
        for _ in range(round(e['post_step_s']/e['communication_step_s'])):
            master.advance(e['communication_step_s'],e['blade_input_w'])
            records.append(read_record())
        summary.update(complete=True,last_fmu_time_s=master.time,
                       min_absolute_pressure_pa=min(r[f'g{g}_min_absolute_pressure_pa'] for r in records for g in range(1,6)),
                       peak_return_degC=max(r[f'g{g}_return_degC'] for r in records for g in range(1,6)))
    except Exception as error:
        summary['error']=str(error)
        raise
    finally:
        summary['fmi_statuses']=sorted(set(master.statuses))
        save(output/'summary.json',summary)
        save(output/'fmu_writes.json',master.writes)
        if records:
            with (output/'trace.csv').open('w',newline='') as f:
                writer=csv.DictWriter(f,fieldnames=list(records[0]));writer.writeheader();writer.writerows(records)
        master.close()
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def suite(args):
    output=Path(args.output).resolve()
    if output.exists():raise ValueError('diagnostics_requires_new_output_directory')
    fmu=Path(args.fmu).resolve();source=Path(args.source).resolve() if args.source else None
    scene=load_scene(args.config)
    contract=inspect_contract(fmu)
    # 只允许挂载项目内旧实验，整个 /app 保持只读。
    source_relative=source.relative_to(ROOT) if source else None
    manifest={"experiment":EXPERIMENT,"fmu_contract":contract,"old_results":str(source),
              "old_result_hashes":{},"code_sha256":{},"runs":{},
              "scope":"Open-loop supervisor experiment; OEM PID inside FMU remains enabled; no hardware."}
    for variant in (['baseline','fixed','adaptive'] if source else []):
        for name in ['trace.csv','runtime.sqlite','scene.json','fmu_writes.json']:
            file=source/variant/name
            wal=file.with_name(file.name+'-wal')
            if name=='runtime.sqlite' and wal.exists() and wal.stat().st_size:
                raise ValueError('source_database_has_uncheckpointed_wal')
            manifest['old_result_hashes'][f'{variant}/{name}']=hashlib.sha256(file.read_bytes()).hexdigest()
    scripts=list((ROOT/'tools').glob('*.py'))
    for p in list((ROOT/'lc_control').glob('*.py'))+scripts:
        manifest['code_sha256'][str(p.relative_to(ROOT))]=hashlib.sha256(p.read_bytes()).hexdigest()
    output.mkdir(parents=True)
    save(output/'scene.json',scene);save(output/'manifest.json',manifest)
    def launch(arguments,tag,timeout=240):
        name='lc-fmu-diag-'+uuid.uuid4().hex[:12]
        if args.docker:
            command=['docker','run','--rm','--pull=never','--platform','linux/amd64','--network','none',
                     '--cpus','2','--memory','2g','--name',name,
                     '--mount',f'type=bind,src={ROOT},dst=/app,readonly',
                     '--mount',f'type=bind,src={fmu},dst=/model/reference.fmu,readonly',
                     '--mount',f'type=bind,src={output},dst=/results','--workdir','/tmp',
                     args.image,'python','/app/tools/run_fmu_diagnostics.py']+arguments
        else:
            arguments=[a.replace('/results',str(output)).replace('/model/reference.fmu',str(fmu))
                       .replace('/app/',str(ROOT)+'/') for a in arguments]
            command=[sys.executable,str(Path(__file__))]+arguments
        print('Running '+tag,flush=True)
        try:
            done=subprocess.run(command,capture_output=True,text=True,timeout=timeout,cwd='/tmp')
        except (subprocess.TimeoutExpired,KeyboardInterrupt):
            if args.docker:subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=20)
            raise
        (output/(tag+'.log')).write_text(done.stdout+'\n'+done.stderr,encoding='utf-8')
        if done.returncode:raise RuntimeError(tag+' failed: '+done.stderr[-2000:])
    # 先从旧证据补图，随后独立创建每个 FMU；单工况失败也保存日志和已有结果。
    if source:
        launch(['--tracking-only','--source','/app/'+str(source_relative),'--output','/results'],'tracking')
    for variant in VARIANTS:
        try:
            launch(['--worker',variant,'--fmu','/model/reference.fmu','--config','/results/scene.json',
                    '--output','/results/'+variant],variant)
            manifest['runs'][variant]=json.loads((output/variant/'summary.json').read_text())
        except Exception as error:
            manifest['runs'][variant]={"complete":False,"error":str(error)}
            save(output/'manifest.json',manifest)
            raise
        save(output/'manifest.json',manifest)
    launch(['--report-only','--output','/results'],'report')
    print(output/'index.html',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--docker',action='store_true')
    parser.add_argument('--image',default='sustain-lc:amd64')
    parser.add_argument('--fmu')
    parser.add_argument('--config',default=str(ROOT/'examples/sustain_fmu_experiment.json'))
    parser.add_argument('--source',help='可选旧三策略目录；省略时只执行新的固定负荷阶跃')
    parser.add_argument('--output',required=True)
    parser.add_argument('--worker',choices=VARIANTS)
    parser.add_argument('--tracking-only',action='store_true')
    parser.add_argument('--report-only',action='store_true')
    args=parser.parse_args()
    if args.tracking_only:
        if not args.source:parser.error('--tracking-only requires --source')
        from fmu_diagnostics_report import tracking
        tracking(Path(args.source),Path(args.output))
    elif args.report_only:
        from fmu_diagnostics_report import report
        report(Path(args.output))
    elif args.worker:worker(Path(args.fmu),load_scene(args.config),Path(args.output),args.worker)
    elif args.fmu:suite(args)
    else:parser.error('--fmu required')


if __name__=='__main__':main()
