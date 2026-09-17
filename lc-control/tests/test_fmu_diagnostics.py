"""阶跃指标的独立数学检查，避免把漂移/未收敛误报成 t90。"""
import math
from pathlib import Path
import sys
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))
from fmu_step_metrics import response_metrics


class StepMetricsTests(unittest.TestCase):
    def test_exponential_up_and_down_with_background_drift(self):
        times=list(range(-900,7201,5))
        hold=[100+.0005*t for t in times]
        for sign in [1,-1]:
            stepped=[b+sign*3*(1-math.exp(-max(t,0)/300)) for t,b in zip(times,hold)]
            m=response_metrics(times,stepped,hold,sign*3)
            self.assertTrue(m['settled_in_window'])
            self.assertAlmostEqual(m['steady_gain'],1,places=5)
            self.assertLessEqual(abs(m['t90_s']-300*math.log(10)),5)
    def test_unsettled_ramp_has_no_time_constants(self):
        times=list(range(-900,7201,5));hold=[0]*len(times)
        m=response_metrics(times,[max(t,0)*.001 for t in times],hold,3)
        self.assertFalse(m['settled_in_window']);self.assertIsNone(m['t90_s'])
    def test_no_response_is_not_settled_gain(self):
        times=list(range(-900,7201,5))
        m=response_metrics(times,[5]*len(times),[5]*len(times),3)
        self.assertFalse(m['settled_in_window']);self.assertIsNone(m['steady_gain'])
    def test_time_and_length_validation(self):
        with self.assertRaises(ValueError):response_metrics([0,1,1,2],[0]*4,[0]*4,3)
        with self.assertRaises(ValueError):response_metrics([-1,0,1,2],[0]*3,[0]*4,3)


if __name__=='__main__':unittest.main()
