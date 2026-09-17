"""回归：纠正方向、慢过程闭环、约束和不合格数据隔离。无需实际 FMU。"""
import copy
from dataclasses import replace
from pathlib import Path
import unittest

from lc_control.configuration import load_scene, validate_scene
from lc_control.contracts import Reading, Snapshot, describe_profile
from lc_control.policy import DP_FEEDBACK_GAIN_BOUNDS, FlowPolicy

ROOT=Path(__file__).resolve().parents[1]


class TrackingTests(unittest.TestCase):
    def setUp(self):
        self.scene=load_scene(ROOT/'examples/sustain_fmu_experiment_legacy.json')
        self.profile=self.scene['devices']['CDU_01']
        self.config=copy.deepcopy(self.profile['policy'])
        self.config.pop('flow_per_sqrt_kpa');self.config.pop('gain_bounds')
        self.config.update(hydraulic_model={'exponent':1.,'gain':.06464,'gain_bounds':[.04,.09]},
                           dp_feedback_gain=2.,calibration_max_dp_error_kpa=.5)
        self.controls=describe_profile('CDU_01',self.profile,'simulated').controls

    def snapshot(self,t,dp=189.6,sp=189.6,delta=8.7,load=None):
        obs={'cdu.sec_flow':Reading(.06464*dp,'kg/s',t),
             'cdu.sec_dp':Reading(dp,'kPa',t),
             'cdu.sec_supply_temp':Reading(301.55,'K',t),
             'cdu.sec_return_temp':Reading(301.55+delta,'K',t)}
        if load is not None:obs['cdu.liquid_load']=Reading(load,'W_th',t)
        return Snapshot('CDU_01',t,'experiment_dp','fmu-laboratory-master',(),obs,
                        {'cdu.dp_sp':Reading(sp,'kPa',t)})

    def context(self,t):
        return {'now':t,'domain':self.scene['control_domains'][0],
                'controls':self.controls,'topology_version':self.scene['topology_version']}

    def test_correct_direction_with_flow_shortfall_and_unchanged_rate_limit(self):
        old=FlowPolicy(self.profile['policy']);new=FlowPolicy(self.config)
        sample=self.snapshot(0)
        self.assertLess(old.propose(sample,self.context(0)).value,189.6)
        command=new.propose(sample,self.context(0))
        self.assertGreater(command.value,189.6)
        self.assertLessEqual(command.value,192.6)
        self.assertIn('control_rate_limited',new.last_decision['warnings'])

    def test_feedback_speeds_slow_plant_without_integral_windup(self):
        # 独立一阶被控对象；不能代替 FMU。检验闭环方向与约束，
        # 需求撤销后不应继续积累旧误差，且每一步均通过既有能力限制。
        results=[]
        for feedback in [0.,2.]:
            c=copy.deepcopy(self.config);c['dp_feedback_gain']=feedback
            p=FlowPolicy(c);dp=sp=189.6;target=195.*.06464
            reach=None
            for t in range(0,7200,60):
                command=p.propose(self.snapshot(t,dp,sp,load=target*4180*8.5),self.context(t))
                if command:
                    self.assertLessEqual(abs(command.value-sp),3.+1e-9)
                    self.assertTrue(172.368932<=command.value<=262.00077664)
                    sp=command.value
                dp+=(sp-dp)*(1-__import__('math').exp(-60/1000))
                if reach is None and dp>=189.6+.9*(195-189.6):reach=t+60
            results.append(reach)
            command=p.propose(self.snapshot(7200,dp,sp,load=8*4180*8.5),self.context(7200))
            self.assertLess(command.value,sp)
        self.assertLess(results[1],results[0])

    def test_slow_unsettled_samples_do_not_train(self):
        p=FlowPolicy(self.config);p.samples=[(190,12.28)]*30
        for t in range(0,3600,60):
            event=p.observe(self.snapshot(t,sp=220),t,self.controls,True)
        self.assertEqual(event['reason'],'inner_loop_not_settled')
        self.assertEqual(p.samples,[]);self.assertEqual(p.version,0)

    def test_bad_dp_cannot_generate_feedback(self):
        p=FlowPolicy(self.config);s=self.snapshot(100)
        s.observations['cdu.sec_dp']=Reading(190,'kPa',0)
        with self.assertRaisesRegex(ValueError,'observation_unavailable'):p.propose(s,self.context(100))

    def test_model_parameters_are_validated(self):
        for path,value in [('exponent',0),('gain',float('nan')),('gain_bounds',[.07,.08])]:
            c=copy.deepcopy(self.config);c['hydraulic_model'][path]=value
            with self.assertRaises(ValueError):FlowPolicy(c)
        c=copy.deepcopy(self.config);c['dp_feedback_gain']=3
        with self.assertRaises(ValueError):FlowPolicy(c)
        c=copy.deepcopy(self.config);c['flow_per_sqrt_kpa']=.95
        with self.assertRaisesRegex(ValueError,'unambiguous'):FlowPolicy(c)
        c=copy.deepcopy(self.config);del c['hydraulic_model']['gain']
        with self.assertRaisesRegex(ValueError,'unambiguous'):FlowPolicy(c)

    def test_dp_feedback_gain_supported_software_bounds_and_default(self):
        # 此处锁定当前产品承诺的区间，不把 2 当成普适物理/稳定性上限。
        # 同时经过场景校验，防止工作台发布路径与直接构造策略的边界分叉。
        self.assertEqual(DP_FEEDBACK_GAIN_BOUNDS, (0.0, 2.0))
        for gain in (0.0, 1.0, 2.0):
            with self.subTest(gain=gain):
                config = copy.deepcopy(self.config)
                config['dp_feedback_gain'] = gain
                self.assertEqual(FlowPolicy(config).dp_feedback_gain, gain)
                scene = copy.deepcopy(self.scene)
                scene['devices']['CDU_01']['policy'] = config
                self.assertIs(validate_scene(scene), scene)
        config = copy.deepcopy(self.config)
        del config['dp_feedback_gain']
        self.assertEqual(FlowPolicy(config).dp_feedback_gain, 0.0)

    def test_dp_feedback_gain_outside_supported_bounds_is_rejected_without_clamping(self):
        # 略超过上限也应拒绝，而不是静默裁成 2 后让界面显示未实际使用的值。
        for gain in (-1e-12, 2.0000000001, 3.0, float('nan'), float('inf'), True, '2'):
            with self.subTest(gain=gain):
                config = copy.deepcopy(self.config)
                config['dp_feedback_gain'] = gain
                with self.assertRaisesRegex(ValueError, '^invalid_dp_feedback_gain$'):
                    FlowPolicy(config)
                scene = copy.deepcopy(self.scene)
                scene['devices']['CDU_01']['policy'] = config
                # NaN/Inf 会先被整个配置的非有限数值检查拒绝，不能参与运行。
                with self.assertRaises(ValueError):
                    validate_scene(scene)

    def test_no_pressure_feedback_in_direct_flow_mode(self):
        c=copy.deepcopy(self.config);c['control_quantity']='cdu.sec_flow_sp'
        with self.assertRaisesRegex(ValueError,'requires_dp'):FlowPolicy(c)
        c['dp_feedback_gain']=0.0
        self.assertEqual(FlowPolicy(c).dp_feedback_gain,0.0)


if __name__=='__main__':unittest.main()
