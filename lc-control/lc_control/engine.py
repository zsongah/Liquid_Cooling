"""把采集、网关、策略、校准和存储串成一个单 CDU 运行周期。

Engine 本身不包含网络循环或 UI；operations.run 和仿真入口共同调用 tick。
每个运行会话使用 UUID，完整场景哈希绑定模型参数，防止换配置后误用旧模型。
任何控制阶段的异常都尝试释放控制，再记录故障；无法联通时依赖设备看门狗。"""
import hashlib
import json
import time
import uuid
from dataclasses import asdict, replace
from pathlib import Path

from .registry import create_policy
from .runtime import ControlService
from .storage import Store


class Engine:
    """单设备产品运行实例。调用者负责循环、互斥锁及最终 close；一次 tick 对应一个采集决策周期。"""
    def __init__(self, scene, domain_id, adapter, output, mode, service_factory=ControlService):
        if scene.get("schema_version") != "0.1":
            raise ValueError("analysis_only_schema_not_executable")
        self.scene, self.adapter = scene, adapter
        self.domain = next(d for d in scene["control_domains"] if d["id"] == domain_id)
        self.profile = scene["devices"][self.domain["cdu_id"]]
        self.output = Path(output)
        self.output.mkdir(parents=True, exist_ok=True)
        self.session = str(uuid.uuid4())
        self.fingerprint = hashlib.sha256(json.dumps(scene, sort_keys=True).encode()).hexdigest()
        # 普通入口不传 factory；FMU 专用实验入口注入管理固定邻机边界的仿真网关。
        self.service = service_factory(scene, domain_id, adapter, self.output / "commands.jsonl")
        self.policy = create_policy(self.profile["policy"]) if "policy" in self.profile else None
        if mode != "monitor" and self.policy is None:
            raise ValueError("policy_required_for_automatic_operation")
        # 配置/审计校验通过后再打开数据库，避免初始化失败遗留连接。
        self.store = Store(self.output / "runtime.sqlite")
        prior = self.store.latest("model")
        if self.policy and prior and prior["fingerprint"] == self.fingerprint:
            gain = prior["gain"]
            if self.policy.gain_bounds[0] <= gain <= self.policy.gain_bounds[1]:
                self.policy.gain, self.policy.version = gain, prior["version"]
        self.last_sample = None
        self.last_tick = None
        # 同一周期的观测、校准、决策和故障使用稳定关联 ID。界面据此回放
        # 真正参与计算的样本，不能用“最近一条”把不同周期的数据拼在一起。
        self.cycle_index = 0
        self.closed = False
        try:
            self.store.put(time.time(), "session", {"session": self.session, "fingerprint": self.fingerprint,
                           "scene": scene, "requested_mode": mode,
                           "deployment": adapter.describe().deployment})
            self.service.set_mode(mode)
        except Exception:
            self.store.close()
            raise

    def tick(self, now, forecast=None, forecast_context=None, forecast_provider=None):
        """先采集保存，再守护和校准，最后产生候选并经过命令网关。
        持久化失败或算法异常都会阻止继续自动控制；异常记录不包含完整远端报文。

        forecast_provider 是可信应用服务传入的内部回调，JSON 不能声明或执行它。
        它在实际决策时刻物化预测，避免网络采集耗时使前馈首样本在计算前已过期。
        旧 tick(now, forecast) 调用保持兼容。forecast_context 仅提供审计元数据。
        """
        if self.last_tick is not None and now <= self.last_tick:
            raise ValueError("nonmonotonic_control_clock")
        self.last_tick = now
        self.cycle_index += 1
        cycle = {"session": self.session, "cycle_id": "%s:%s" % (self.session, self.cycle_index),
                 "asset_id": self.domain["cdu_id"], "domain_id": self.domain["id"]}
        try:
            snapshot = self.adapter.read(now)
            clock = self.service._now(now)
            self.store.put(clock, "telemetry", {**cycle, **asdict(snapshot)})
            self.last_sample = snapshot
            if self.service.mode == "control":
                self.service.heartbeat(clock)
            if not self.policy or self.service.mode not in ("shadow", "control"):
                if forecast_provider:
                    forecast, forecast_context = forecast_provider(self.service._now(now))
                usage = dict(forecast_context) if forecast_context is not None else None
                if usage is not None:
                    if usage.get("status") == "ready":
                        usage.update(status="not_used", reason="monitor_mode_no_automatic_control")
                    self.store.put(clock, "forecast_usage", {**cycle, **usage})
                return {**cycle, "mode": self.service.mode, "forecast_input": usage}
            errors = self.service._snapshot_errors(snapshot, self.service._now(now))
            safe = not errors
            calibration = self.policy.observe(snapshot, clock, self.adapter.describe().controls, safe)
            self.store.put(clock, "calibration", {**cycle, **calibration})
            if calibration["status"] == "promoted":
                self.store.put(clock, "model", {**cycle, "fingerprint": self.fingerprint,
                               "gain": self.policy.gain, "version": self.policy.version, **calibration})
            if errors:
                raise ValueError(";".join(errors))
            context = {"now": self.service._now(now), "domain": self.domain,
                       "controls": self.adapter.describe().controls,
                       "topology_version": self.scene["topology_version"], "forecast": forecast}
            if forecast_provider:
                forecast, forecast_context = forecast_provider(context["now"])
                context["forecast"] = forecast
            request = self.policy.propose(snapshot, context)
            receipt = None
            if request:
                if hasattr(self.adapter, "normalized_value"):
                    request = replace(request, value=self.adapter.normalized_value(request.quantity, request.value))
                receipt = self.service.submit(request, self.service._now(now))
                if receipt.status == "rejected" and receipt.reasons != ("minimum_interval_not_met",):
                    self.policy.last_decision["warnings"].append("command_rejected")
            decision = {**cycle, "mode": self.service.mode,
                        "policy_quantity": self.profile["policy"]["control_quantity"],
                        "input_timestamp": snapshot.timestamp,
                        "observed_setpoints": {name: asdict(reading) for name, reading in snapshot.setpoints.items()},
                        **self.policy.last_decision, "request": asdict(request) if request else None,
                        "receipt": asdict(receipt) if receipt else None}
            if forecast_context is not None:
                usage = dict(forecast_context)
                usage["policy_status"] = self.policy.last_decision.get("forecast_status")
                if usage.get("status") == "ready":
                    used = usage["policy_status"] == "forecast_used"
                    usage.update(status="used" if used else "rejected",
                                 reason="participated_in_demand_maximum" if used else "policy_rejected_materialized_forecast")
                decision["forecast_input"] = usage
                self.store.put(context["now"], "forecast_usage", {**cycle, **usage})
            self.store.put(clock, "decision", decision)
            return decision
        except Exception as error:
            if self.service.mode == "control":
                self.service._pause("runtime_error:" + type(error).__name__)
            # Record diagnostic category, without copying raw remote payloads or secrets.
            event = {**cycle, "mode": self.service.mode,
                     "error": type(error).__name__, "reason": str(error)[:240]}
            self.store.put(now, "fault", event)
            return event

    def close(self):
        """可重复调用的关闭流程；处于 control 时尝试本地接管，最后关闭数据库。"""
        if self.closed:
            return
        self.closed = True
        try:
            if self.service.mode == "control":
                self.service._pause("runtime_shutdown")
        finally:
            self.store.close()


class EndpointLock:
    """基于 fcntl 的同机文件锁，防止两个本机进程控制同一端点或共写同一输出目录。
    不能替代跨主机/BMS/PLC 的唯一控制权仲裁；Windows 尚未提供适配。"""
    def __init__(self, identity):
        import tempfile
        self.path = Path(tempfile.gettempdir()) / ("lc-control-" + hashlib.sha256(identity.encode()).hexdigest() + ".lock")
        self.file = None

    def __enter__(self):
        import fcntl
        self.file = self.path.open("a+")
        try:
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("device_already_used_by_local_process")
        return self

    def __exit__(self, *args):
        import fcntl
        fcntl.flock(self.file, fcntl.LOCK_UN)
        self.file.close()
