"""有限观察窗口的阶跃指标；先减去同条件 hold，避免把背景漂移当作响应。

只有尾段满足稳定判据才报告 t10/t50/t90 与 2% 稳定时间。
这些是采样分辨率内、有限窗口的估计，不是全工况或无限时间稳定性证明。
"""
import statistics


def response_metrics(times, stepped, hold, step_size, floor=1e-5):
    if not (len(times) == len(stepped) == len(hold)) or len(times) < 4:
        raise ValueError("response_length_mismatch")
    if any(b <= a for a, b in zip(times, times[1:])):
        raise ValueError("nonmonotonic_response_time")
    if not step_size:
        raise ValueError("zero_step")
    before = [a-b for t,a,b in zip(times,stepped,hold) if t <= 0]
    if not before:
        raise ValueError("pre_step_samples_required")
    offset = statistics.mean(before)
    post = [(t,a-b-offset) for t,a,b in zip(times,stepped,hold) if t > 0]
    if len(post) < 3:
        raise ValueError("post_step_samples_required")
    end = post[-1][0]
    tail = [y for t,y in post if t > end-1200]
    prior = [y for t,y in post if end-2400 < t <= end-1200]
    final = statistics.mean(tail)
    tolerance = max(abs(final)*.02, floor)
    settled = (len(prior) >= 2 and abs(final) > floor*10
               and max(tail)-min(tail) <= tolerance
               and abs(statistics.mean(prior)-final) <= tolerance)
    result = {"baseline_offset":offset,"final_delta":final,"tail_range":max(tail)-min(tail),
              "steady_gain":final/step_size if settled else None,
              "settled_in_window":settled,"window_s":end,"tolerance":tolerance,
              "t10_s":None,"t50_s":None,"t90_s":None,"settling_2pct_s":None,
              "overshoot_pct":None}
    if settled:
        for fraction, name in [(.1,"t10_s"),(.5,"t50_s"),(.9,"t90_s")]:
            result[name] = next((t for t,y in post if y/final >= fraction),None)
        violations = [i for i,(_,y) in enumerate(post) if abs(y-final) > tolerance]
        index = violations[-1]+1 if violations else 0
        if index < len(post):result["settling_2pct_s"]=post[index][0]
        result["overshoot_pct"]=max(0,max(y/final for _,y in post)-1)*100
    return result
