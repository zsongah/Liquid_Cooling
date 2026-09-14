"""Sustain-LC 已有结果的因果时序回放。
只使用当前与过去的公开测量，未来样本仅用于评分；候选不执行。
固定 CSV 不能响应新动作，因此不能据此评估闭环节能或控制稳定性。"""
import argparse
import csv
import hashlib
import json
import math
import statistics
import sys
from dataclasses import asdict
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from lc_control.configuration import load_scene
from lc_control.contracts import ControlCapability, Reading, Snapshot
from lc_control.policy import FlowPolicy
from lc_control.sustain_fmu import KPA_PER_PSI


def replay(path, output):
    path, output = Path(path), Path(output)
    raw = path.read_bytes()
    with path.open() as file:
        rows = [{k: float(v) for k,v in row.items() if k != 'phase'} for row in csv.DictReader(file)]
    if any(b['time_s'] <= a['time_s'] for a,b in zip(rows, rows[1:])):
        raise ValueError('trace_time_must_increase')
    base = Path(__file__).resolve().parents[1]
    config = load_scene(base/'examples/thermal_liquid_to_liquid.json')['devices']['CDU_01']['policy']
    config.update(minimum_flow_kg_s=8,maximum_flow_kg_s=18,flow_per_sqrt_kpa=.95,
                  gain_bounds=[.5,1.5],max_liquid_load_w=1000000,thermal_tau_s=300,adaptive_enabled=False)
    policy = FlowPolicy(config)
    quantity = 'cdu.dp_sp'
    controls = {quantity:ControlCapability('kPa',25*KPA_PER_PSI,38*KPA_PER_PSI,3,60,('reference',),'illustrative_reference_limits')}
    domain = {'cdu_id':'FMU_GROUP_1','owner':'replay-no-device-owner', 'served_racks':[], 'shared_hydraulics':True}
    predictions, decisions = [], []
    for row in rows:
        now = row['time_s']
        if now < 600:
            continue
        supply, returned, flow = row['g1_sec_supply_C']+273.15, row['g1_sec_return_C']+273.15, row['g1_sec_mass_flow_kg_s']
        if not all(math.isfinite(v) for v in (now,supply,returned,flow)):
            raise ValueError('nonfinite_trace')
        snapshot = Snapshot('FMU_GROUP_1',now,'reference','replay-no-device-owner',(),
            {'cdu.sec_supply_temp':Reading(supply,'K',now,provenance='sustain_csv'),
             'cdu.sec_return_temp':Reading(returned,'K',now,provenance='sustain_csv'),
             'cdu.sec_flow':Reading(flow,'kg/s',now,provenance='sustain_csv')},
            {quantity:Reading(27.5*KPA_PER_PSI,'kPa',now,provenance='historical_psi_input_converted_not_pressure_measurement')})
        calibration=policy.observe(snapshot,now,controls,True)
        request=policy.propose(snapshot,{'now':now,'domain':domain,'controls':controls,'topology_version':'sustain-fixed'})
        predictions.append((now,policy.last_decision['return_forecast_k']))
        decisions.append({'at':now,'candidate':asdict(request) if request else None,
            'dispatch':'disabled_shared_facility_and_replay_only','calibration':calibration,**policy.last_decision})
    actual = {r['time_s']:r['g1_sec_return_C']+273.15 for r in rows}
    errors = [value-actual[t+config['horizon_s']] for t,value in predictions if t+config['horizon_s'] in actual]
    persistence = [actual[t]-actual[t+config['horizon_s']] for t,_ in predictions if t+config['horizon_s'] in actual]
    summary = {'source':str(path.resolve()),'source_sha256':hashlib.sha256(raw).hexdigest(),
        'replayed_samples':len(decisions),'scored_predictions':len(errors),'horizon_s':config['horizon_s'],
        'return_prediction_mae_k':statistics.mean(abs(e) for e in errors),
        'persistence_mae_k':statistics.mean(abs(e) for e in persistence),
        'writes':0,'model_promotions':policy.version,
        'findings':['Without independent load information this thermal predictor collapses to persistence.',
                    'No actual DP measurement in this trace; hydraulic parameter learning stays frozen.',
                    'Shared facility coupling and absence of command feedback prevent active control validation.',
                    'This replay is not a new FMU run; use run_sustain_fmu.py for the separate closed-loop experiment.'],
        'mapping_notes':['Only verified summary *_C and mass-flow columns are used.',
                         'Legacy g*_chan*_T_C columns are NOT used: their underlying medium.T values are kelvin.',
                         'Historical input 27.5 is psi, not kPa; corrected using the FMU internal Pa-to-psi comparator.',
                         'Configured 25–38 psi converted to kPa and 8–18 kg/s are development bounds, not OEM limits.']}
    output.mkdir(parents=True,exist_ok=True)
    (output/'replay_summary.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2)+'\n')
    (output/'replay_decisions.jsonl').write_text(''.join(json.dumps(d,ensure_ascii=False,allow_nan=False)+'\n' for d in decisions))
    return summary


if __name__ == '__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('trace')
    parser.add_argument('--output',default='outputs/sustain-replay')
    args=parser.parse_args()
    print(json.dumps(replay(args.trace,args.output),ensure_ascii=False,indent=2))
