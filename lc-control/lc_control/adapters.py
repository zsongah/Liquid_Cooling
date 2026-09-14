"""纯软件协议替身。

MockCDUAdapter 只改变设定值，不计算温度动态；其失败开关用于网关测试。
ThermalPlant 在另一个文件中扩展这个替身，真实设备不得使用 mock 观测作为回读。"""
from .contracts import Reading, Snapshot, describe_profile


class MockCDUAdapter:
    """静态测量＋可写目标的协议替身。故障标志供测试使用，不是工程配置参数。"""
    def __init__(self, asset_id, profile, owner):
        self.asset_id = asset_id
        self.profile = profile
        self.owner = owner
        self.operating_mode = profile["initial_mode"]
        self.setpoints = dict(profile["initial_setpoints"])
        self.alarms = ()
        self.writes = []
        self.stale_by_s = 0.0
        self.fail_write = False
        self.ignore_write = False
        self.fail_release = False
        self.read_fail = False
        self.releases = 0

    def describe(self):
        return describe_profile(self.asset_id, self.profile, "simulation")

    def read(self, now):
        """根据给定时刻生成快照；stale_by_s 可以模拟旧数据。"""
        if self.read_fail:
            raise OSError("mock_read_failure")
        timestamp = now - self.stale_by_s
        observations = {q: Reading(x["value"], x["unit"], timestamp)
                        for q, x in self.profile["initial_observations"].items()}
        readbacks = {q: Reading(v, self.profile["controls"][q]["unit"], timestamp)
                     for q, v in self.setpoints.items()}
        return Snapshot(self.asset_id, timestamp, self.operating_mode, self.owner,
                        tuple(self.alarms), observations, readbacks)

    def write(self, request):
        """只更新内存设定值，并支持写后结果不确定/忽略写入的测试场景。"""
        self.writes.append(request)
        if self.fail_write:
            raise OSError("mock_write_outcome_unknown")
        if not self.ignore_write:
            self.setpoints[request.quantity] = request.value

    def release_to_local(self, owner):
        """模拟设备本地接管。真实设备必须自己实现并回读这种行为。"""
        self.releases += 1
        if self.fail_release or self.owner != owner:
            return False
        self.owner = None
        self.operating_mode = "local_auto"
        return True
