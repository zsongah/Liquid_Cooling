import copy
import json
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from lc_control.configuration import load_scene, validate_scene
from lc_control.contracts import ControlRequest
from lc_control.engine import EndpointLock, Engine
from lc_control.operations import compare
from lc_control.plant import ThermalPlant
from lc_control.policy import FlowPolicy
from lc_control.runtime import ControlService

ROOT = Path(__file__).resolve().parents[1]


class ProductTests(unittest.TestCase):
    def setUp(self):
        self.scene = load_scene(ROOT / 'examples/thermal_liquid_to_liquid.json')
        self.domain = self.scene['control_domains'][0]
        self.profile = self.scene['devices']['CDU_01']
        self.adapter = ThermalPlant('CDU_01', self.profile, 'lc-core')
        self.policy = FlowPolicy(self.profile['policy'])

    def test_matched_comparison_and_model_improvement(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(self.scene, tmp)
            adaptive, fixed = result['adaptive_policy'], result['fixed_model_policy']
            self.assertEqual(adaptive['seconds'], 1800)
            self.assertEqual(adaptive['mode_before_shutdown'], 'control')
            self.assertEqual(adaptive['return_guard_exceedance_samples'], 0)
            self.assertGreater(adaptive['model_updates'], 0)
            true = self.profile['simulation']['true_flow_per_sqrt_kpa']
            self.assertLess(abs(adaptive['final_gain'] - true), abs(fixed['final_gain'] - true))
            self.assertLess(adaptive['pump_energy_kwh'], fixed['pump_energy_kwh'])
            self.assertTrue((Path(tmp) / 'report.html').is_file())

    def test_liquid_to_air_flow_control_runs_same_engine(self):
        scene = load_scene(ROOT / 'examples/thermal_liquid_to_air.json')
        with tempfile.TemporaryDirectory() as tmp:
            result = compare(scene, tmp, seconds=1800)
            self.assertEqual(result['adaptive_policy']['seconds'], 1800)
            self.assertEqual(result['adaptive_policy']['mode_before_shutdown'], 'control')
            import sqlite3
            with sqlite3.connect(str(Path(tmp)/'runtime.sqlite')) as db:
                decisions=[json.loads(r[0]) for r in db.execute("SELECT payload FROM events WHERE kind='decision'")]
            self.assertFalse(any(d.get('receipt') and d['receipt']['status']=='rejected' for d in decisions))

    def test_exact_flow_step_not_rejected_by_float_roundoff(self):
        scene = load_scene(ROOT/'examples/thermal_liquid_to_air.json')
        p=scene['devices']['CDU_01']
        p['initial_setpoints']['cdu.sec_flow_sp']=1.963802647154398
        adapter=ThermalPlant('CDU_01',p,'lc-core')
        service=ControlService(scene,'CDU_DOMAIN_01',adapter)
        service.set_mode('control')
        request=ControlRequest('float-step','CDU_01','example-1','lc-core','cdu.sec_flow_sp',
                               2.463802647154398,'kg/s',100,104)
        self.assertEqual(service.submit(request,100).status,'setpoint_confirmed')

    def test_forecast_requires_full_aligned_coverage_and_cannot_lower_demand(self):
        racks = ['RACK_01', 'RACK_02']
        forecast = {'issued_at':100, 'valid_until':130, 'unit':'W_e', 'racks':[
            {'rack_id':name, 'liquid_fraction':0.8, 'samples':[{'at':100,'power_w':50000}, {'at':120,'power_w':60000}]}
            for name in racks]}
        self.assertEqual(self.policy.forecast_load(forecast,100,racks), (96000,'forecast_used'))
        for mutation in ('stale','partial','duplicate','misaligned','nan','unit'):
            bad = copy.deepcopy(forecast)
            if mutation == 'stale': bad['issued_at']=1
            if mutation == 'partial': bad['racks'].pop()
            if mutation == 'duplicate': bad['racks'][1]['rack_id']='RACK_01'
            if mutation == 'misaligned': bad['racks'][1]['samples'][0]['at']=101
            if mutation == 'nan': bad['racks'][0]['liquid_fraction']=float('nan')
            if mutation == 'unit': bad['unit']='kW'
            self.assertEqual(self.policy.forecast_load(bad,100,racks)[1], 'forecast_rejected', mutation)
        self.adapter.advance(5,110000)
        context={'now':100,'domain':self.domain,'controls':self.adapter.describe().controls,
                 'topology_version':self.scene['topology_version'],'forecast':forecast}
        self.policy.propose(self.adapter.read(100),context)
        self.assertEqual(self.policy.last_decision['load_w_th'],110000)

    def test_bad_data_freezes_learning_and_guard_failure_releases(self):
        self.policy.samples=[(5,2)]*30
        result=self.policy.observe(self.adapter.read(100),100,self.adapter.describe().controls,False)
        self.assertEqual(result['status'],'frozen')
        self.assertEqual(self.policy.samples,[])
        with tempfile.TemporaryDirectory() as tmp:
            engine=Engine(self.scene,self.domain['id'],self.adapter,tmp,'control')
            self.adapter.alarms=('leak',)
            engine.tick(100)
            self.assertEqual(engine.service.mode,'paused')
            self.assertEqual(self.adapter.writes,[])
            engine.close()

    def test_no_excitation_cannot_promote_parameters(self):
        for now in range(1,100):
            result=self.policy.observe(self.adapter.read(now),now,self.adapter.describe().controls,True)
        self.assertEqual(self.policy.version,0)
        self.assertIn(result['status'],('collecting','frozen'))

    def test_restart_replay_and_unresolved_intent(self):
        request=ControlRequest('persist','CDU_01','example-1','lc-core','cdu.dp_sp',39,'kPa',100,104)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.jsonl'
            service=ControlService(self.scene,self.domain['id'],self.adapter,path)
            service.set_mode('control')
            receipt=service.submit(request,100)
            restart=ControlService(self.scene,self.domain['id'],self.adapter,path)
            self.assertEqual(restart.submit(request,101),receipt)
            self.assertEqual(len(self.adapter.writes),1)
            path.write_text(json.dumps({'event':'dispatch_intent','at':100,'request':asdict(request)})+'\n')
            restart=ControlService(self.scene,self.domain['id'],self.adapter,path)
            self.assertEqual(restart.submit(request,101).status,'uncertain')
            with self.assertRaisesRegex(ValueError,'reconciliation'):
                restart.set_mode('control')

    def test_journal_corruption_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'audit.jsonl';path.write_text('{broken')
            with self.assertRaises(ValueError):
                ControlService(self.scene,self.domain['id'],self.adapter,path)

    def test_single_local_owner_lock(self):
        with EndpointLock('unit-test-endpoint'):
            with self.assertRaisesRegex(RuntimeError,'already_used'):
                with EndpointLock('unit-test-endpoint'):
                    pass

    def test_unbound_template_and_bad_mapping_rejected(self):
        with self.assertRaises(ValueError):
            load_scene(ROOT/'examples/field_template.UNBOUND.json')
        scene=load_scene(ROOT/'examples/modbus_emulator.json')
        scene['devices']['CDU_01']['points']['commands']['release']['address']=100
        with self.assertRaisesRegex(ValueError,'overlapping'):
            validate_scene(scene)


if __name__ == '__main__': unittest.main()
