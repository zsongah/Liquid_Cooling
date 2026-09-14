"""Actual localhost TCP exchanges, never real equipment."""
import copy
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from lc_control.configuration import load_scene
from lc_control.contracts import ControlRequest
from lc_control.emulator import Emulator
from lc_control.modbus import ModbusCDUAdapter, ModbusTCPClient, encode, decode
from lc_control.runtime import ControlService

ROOT=Path(__file__).resolve().parents[1]


class CodecTests(unittest.TestCase):
    def test_known_wire_values_and_order(self):
        self.assertEqual(decode(bytes.fromhex('41480000'),{'dtype':'float32'}),12.5)
        self.assertEqual(decode(bytes.fromhex('00004148'),{'dtype':'float32','word_order':'little'}),12.5)
        self.assertEqual(decode(bytes.fromhex('48410000'),{'dtype':'float32','byte_order':'little'}),12.5)
        self.assertEqual(decode(bytes.fromhex('fff6'),{'dtype':'int16','scale':0.1,'offset':273.15}),272.15)
        self.assertEqual(encode(28.3,{'dtype':'uint16','scale':0.1}),bytes.fromhex('011b'))
        with self.assertRaises(ValueError): decode(bytes.fromhex('7fc00000'),{'dtype':'float32'})
        with self.assertRaises(ValueError): decode(bytes.fromhex('ffff'),{'dtype':'uint16','invalid_raw':[65535]})


class NetworkTests(unittest.TestCase):
    def setUp(self):
        self.scene=load_scene(ROOT/'examples/modbus_emulator.json')
        self.emulator=Emulator(self.scene,port=0).start()
        self.profile=self.scene['devices']['CDU_01']
        self.profile['connection']['port']=self.emulator.port
        self.adapter=ModbusCDUAdapter('CDU_01',self.profile,'lc-core')
        self.tmp=tempfile.TemporaryDirectory()
        self.service=ControlService(self.scene,'CDU_DOMAIN_01',self.adapter,Path(self.tmp.name)/'audit.jsonl')

    def tearDown(self):
        self.emulator.close()
        self.tmp.cleanup()

    def request(self,value=39):
        now=time.time()
        return ControlRequest('wire-test','CDU_01','example-1','lc-core','cdu.dp_sp',value,'kPa',now,now+4)

    def test_monitor_and_shadow_have_zero_writes(self):
        self.adapter.read(time.time())
        self.service.set_mode('shadow')
        self.assertEqual(self.service.submit(self.request(),time.time()).status,'shadow')
        self.assertEqual(self.emulator.writes,[])

    def test_real_tcp_control_readback_and_shutdown_handoff(self):
        self.service.set_mode('control')
        receipt=self.service.submit(self.request(39.04),time.time())
        self.assertEqual(receipt.status,'setpoint_confirmed')
        self.assertEqual(receipt.readback,39)
        self.assertEqual(receipt.provenance,'hardware')
        self.assertEqual(receipt.process_response,'not_verified')
        self.assertTrue(self.service._pause('test_shutdown'))
        self.assertEqual(self.adapter.read(time.time()).operating_mode,'local_auto')

    def test_uncommissioned_hardware_cannot_write(self):
        self.profile['write_enabled']=False
        with self.assertRaisesRegex(ValueError,'commissioning'):
            self.service.set_mode('control')
        with self.assertRaises(PermissionError): self.adapter.write(self.request())
        self.assertEqual(self.emulator.writes,[])

    def test_lost_write_reply_is_uncertain_and_never_retried(self):
        self.service.set_mode('control')
        self.emulator.drop_after_write=True
        request=self.request()
        first=self.service.submit(request,time.time())
        self.assertEqual(first.status,'uncertain')
        self.assertEqual(self.service.submit(request,time.time()),first)
        self.assertEqual(sum(name=='cdu.dp_sp' for name,_ in self.emulator.writes),1)
        self.assertEqual(self.service.mode,'paused')
        restarted=ControlService(self.scene,'CDU_DOMAIN_01',self.adapter,Path(self.tmp.name)/'audit.jsonl')
        with self.assertRaisesRegex(ValueError,'reconciliation'):
            restarted.set_mode('control')

    def test_hardware_alarm_pauses_before_setpoint_write(self):
        self.service.set_mode('control')
        with self.emulator.lock:
            self.emulator.plant.alarms=('leak',)
            self.emulator.refresh()
        result=self.service.heartbeat(time.time())
        self.assertEqual(result['mode'],'paused')
        self.assertFalse(any(name=='cdu.dp_sp' for name,_ in self.emulator.writes))

    def test_encoded_value_cannot_cross_engineering_limit(self):
        self.service.set_mode('control')
        self.profile['controls']['cdu.dp_sp']['maximum']=44.06
        result=self.service.submit(self.request(44.06),time.time())
        self.assertEqual(result.status,'rejected')
        self.assertIn('encoded_control_out_of_bounds',result.reasons)

    def test_wrong_transaction_and_exception_rejected(self):
        self.emulator.wrong_transaction=True
        with self.assertRaisesRegex(ValueError,'header'): self.adapter.read(time.time())
        self.emulator.wrong_transaction=False
        self.emulator.force_exception=True
        with self.assertRaisesRegex(OSError,'exception'): self.adapter.read(time.time())

    def test_total_timeout_is_bounded(self):
        self.emulator.delay_s=0.2
        self.profile['connection']['timeout_s']=0.05
        start=time.monotonic()
        with self.assertRaises(TimeoutError): self.adapter.read(time.time())
        self.assertLess(time.monotonic()-start,0.25)

    def test_expiry_during_scan_never_dispatches(self):
        self.service.set_mode('control')
        self.emulator.delay_s=0.015
        request=self.request()
        request=replace(request,expires_at=request.issued_at+0.05)
        result=self.service.submit(request,time.time())
        self.assertEqual(result.status,'rejected')
        self.assertIn('expired_during_acquisition',result.reasons)
        self.assertFalse(any(name=='cdu.dp_sp' for name,_ in self.emulator.writes))

    def test_plc_watchdog_runs_without_software_polling(self):
        self.profile['commissioning']['watchdog_timeout_s']=0.2
        time.sleep(0.4)
        snapshot=self.adapter.read(time.time())
        self.assertEqual(snapshot.owner,'local')
        self.assertEqual(snapshot.operating_mode,'local_auto')

    def test_fc16_multi_register_and_fc3_read(self):
        # Replace a synthetic fixture point with a two-register float binding.
        p=self.profile['points']['setpoints']['cdu.sec_supply_temp_sp']
        p.update(address=300,dtype='float32',scale=1,write_function=16)
        self.emulator.refresh()
        self.adapter.client.write(p,29.5)
        self.assertEqual(self.adapter.client.read(p),29.5)

    def test_read_only_profile_does_not_need_control_owner(self):
        profile=copy.deepcopy(self.profile)
        profile['controls']={}
        profile['points']['setpoints']={}
        profile['points']['commands']={}
        profile['points']['status']={}
        profile['write_enabled']=False
        profile.pop('policy')
        adapter=ModbusCDUAdapter('CDU_01',profile,'lc-core')
        snapshot=adapter.read(time.time())
        self.assertEqual(snapshot.owner,'unknown')
        self.assertIn('device_alarm_unavailable',snapshot.alarms)
        self.assertFalse(adapter.commissioned())
        self.assertFalse(adapter.describe().controls)
        self.assertFalse(self.emulator.writes)

    def test_discrete_input_and_coil_read(self):
        point={'address':202,'read_function':2,'dtype':'bool','unit':'1'}
        with self.emulator.lock:
            self.emulator.plant.alarms=('test_alarm',)
            self.emulator.registers[202]=b'\x00\x01'
        self.assertEqual(self.adapter.client.read(point),1)
        point['read_function']=1
        self.assertEqual(self.adapter.client.read(point),1)

    def test_separate_write_and_readback_address(self):
        point=self.profile['points']['setpoints']['cdu.dp_sp']
        point['write_address']=301
        self.service.set_mode('control')
        receipt=self.service.submit(self.request(),time.time())
        self.assertEqual(receipt.status,'setpoint_confirmed')
        self.assertEqual(receipt.readback,39)

    def test_frozen_plc_source_counter_marks_readings_stale(self):
        self.profile['points']['status']['sample_counter']={'address':203,'read_function':3,'dtype':'uint16','unit':'1'}
        with self.emulator.lock:
            self.emulator.freeze_sample_counter=True
            self.emulator.refresh()
        self.adapter.read(time.time())
        self.adapter.counter_changed_at=time.monotonic()-20
        snapshot=self.adapter.read(time.time())
        self.assertTrue(all(p.quality=='stale_source' for p in snapshot.observations.values()))

    def test_full_scan_has_shared_deadline(self):
        self.profile['max_data_age_s']=0.06
        self.emulator.delay_s=0.02
        start=time.monotonic()
        with self.assertRaises(TimeoutError):
            self.adapter.read(time.time())
        self.assertLess(time.monotonic()-start,0.3)

    def test_cli_reports_fault_with_nonzero_exit(self):
        """进程管理器必须能区分一次健康运行与通信故障退出。"""
        import json
        import subprocess
        import sys
        self.emulator.force_exception=True
        scene_path=Path(self.tmp.name)/'scene.json'
        scene_path.write_text(json.dumps(self.scene))
        result=subprocess.run([sys.executable,'-m','lc_control','run',str(scene_path),
                               '--mode','monitor','--steps','1','--output',str(Path(self.tmp.name)/'run')],
                              cwd=ROOT,capture_output=True,text=True,timeout=10)
        self.assertEqual(result.returncode,2,result.stdout+result.stderr)
        self.assertFalse(self.emulator.writes)

    def test_temperature_write_requires_condensation_protection(self):
        """固定供温上下限不能替代动态露点或已验收的 OEM 防凝露保护。"""
        self.service.set_mode('control')
        request=replace(self.request(),quantity='cdu.sec_supply_temp_sp',value=29,unit='degC')
        result=self.service.submit(request,time.time())
        self.assertIn('temperature_protection_not_verified',result.reasons)
        self.profile['temperature_protection']={'mode':'oem_local','verified':True,
            'evidence_reference':'emulator test only'}
        request=replace(request,request_id='protected-temperature')
        self.assertEqual(self.service.submit(request,time.time()).status,'setpoint_confirmed')

    def test_quantized_temperature_still_respects_dew_point(self):
        self.profile['temperature_protection']={'mode':'observed_dew_point','margin_k':0}
        self.profile['points']['observations']['environment.dew_point']={
            'address':12,'read_function':4,'dtype':'float32','unit':'K'}
        with self.emulator.lock:
            self.emulator.plant.observations['environment.dew_point']={'value':301.175,'unit':'K'}
            self.emulator.refresh()
        self.service.set_mode('control')
        request=replace(self.request(),quantity='cdu.sec_supply_temp_sp',value=28.04,unit='degC')
        result=self.service.submit(request,time.time())
        self.assertIn('encoded_temperature_below_dew_point_margin',result.reasons)
        self.assertFalse(any(n=='cdu.sec_supply_temp_sp' for n,_ in self.emulator.writes))


if __name__ == '__main__': unittest.main()
