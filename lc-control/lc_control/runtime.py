"""串行命令网关：每条动作必须经过这里再进入设备适配器。

职责包括模式、归属、拓扑、质量、单位、限值、频率、时效、量化和回读检查。
正常循环和没有新动作时都需要 heartbeat；设备仍保留自身 PLC 保护。
命令意图在网络写入前持久化，重启读取审计实现去重和未决动作阻断。
本地 RLock 只防进程内并发，多主机控制权由现场 PLC/BMS 仲裁。"""
from dataclasses import asdict
import json
import math
import os
import time
from pathlib import Path
from threading import RLock

from .configuration import finite
from .contracts import ControlReceipt, ControlRequest


class ControlService:
    """每个控制域一个串行网关，默认 monitor。constructor 会恢复命令日志与未决动作。"""
    def __init__(self, scene, domain_id, adapter, audit_path=None):
        self.scene = scene
        self.domain = next(d for d in scene["control_domains"] if d["id"] == domain_id)
        self.profile = scene["devices"][self.domain["cdu_id"]]
        self.adapter = adapter
        self.mode = "monitor"
        self.audit_path = Path(audit_path) if audit_path else None
        self.events = []
        self.cache = {}
        self.last_write = {}
        self.lock = RLock()
        self.unresolved = set()
        if self.audit_path and self.audit_path.exists():
            intents = {}
            for line in self.audit_path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)  # Corrupt/truncated journals fail closed.
                if event["event"] == "dispatch_intent":
                    req = ControlRequest(**event["request"])
                    intents[req.request_id] = req
                    self.unresolved.add(req.request_id)
                    self.last_write[req.quantity] = event["at"]
                if event["event"] == "receipt":
                    data = event["receipt"]
                    req = ControlRequest(**event["request"]) if "request" in event else intents.get(data["request_id"])
                    if req:
                        data["reasons"] = tuple(data["reasons"])
                        self.cache[req.request_id] = (req, ControlReceipt(**data))
                    if data["status"] == "uncertain":
                        self.unresolved.add(data["request_id"])
                    else:
                        self.unresolved.discard(data["request_id"])
            for key in self.unresolved:
                if key not in self.cache:
                    self.cache[key] = (intents[key], ControlReceipt(key, "uncertain", ("restart_pending_intent",)))

    def _now(self, supplied):
        """真实网络使用当前 Unix 时间，仿真保留调用方通信时刻，防止网络耗时被虚拟时钟忽略。"""
        return time.time() if self.adapter.describe().deployment == "hardware" else supplied

    def _shared_control_permitted(self):
        """默认禁止共享水路独立写入。实验主控可在专用仿真子类中实现受限协调。

        普通设备运行入口始终使用本实现；不能通过 JSON 标志放开共享设备控制。
        """
        return not self.domain["shared_hydraulics"]

    @staticmethod
    def _step_exceeded(delta, limit):
        # Floating arithmetic can turn an exact 0.5 kg/s step into 0.5000000000000002.
        """比较动作幅度并仅容忍浮点舍入误差；不能用传感器精度作为放宽工程限值的理由。"""
        return delta > limit and not math.isclose(delta, limit, rel_tol=1e-12, abs_tol=1e-12)

    def _audit(self, event):
        # Persist before I/O for dispatch events. An audit failure prevents the write.
        """追加 JSONL 审计并 flush/fsync；写入意图审计失败时，不发送控制报文。"""
        if self.audit_path:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as file:
                file.write(json.dumps(event, ensure_ascii=False, allow_nan=False) + "\n")
                file.flush()
                os.fsync(file.fileno())
        self.events.append(event)

    def set_mode(self, mode):
        """设置 monitor/shadow/control。自动控制要求设备验收、日志和独立回路；paused 需新会话与处置。"""
        with self.lock:
            if mode not in ("monitor", "shadow", "control"):
                raise ValueError("unsupported_runtime_mode")
            if self.mode == "paused":
                raise ValueError("new_session_and_recommission_required")
            if mode == "control":
                if self.unresolved:
                    raise ValueError("unresolved_journal_requires_device_reconciliation")
                if self.adapter.describe().deployment != "simulation":
                    if not self.audit_path or not self.adapter.commissioned():
                        raise ValueError("hardware_commissioning_and_durable_audit_required")
                if not self._shared_control_permitted():
                    raise ValueError("shared_hydraulics_coordinator_required")
            if self.mode == "control" and mode != "control":
                self._pause("operator_exit_control")
                return
            self._audit({"event": "mode", "from": self.mode, "to": mode})
            self.mode = mode

    def _pause(self, reason):
        """暂停后尝试交还本地控制，并明确记录已确认或未确认。
        网络断开时回退可能无法确认，必须依靠设备失联策略与现场处置。"""
        self.mode = "paused"
        try:
            released = self.adapter.release_to_local(self.domain["owner"])
        except Exception:
            released = False
        self._audit({"event": "paused", "reason": reason,
                     "handoff_confirmed": released,
                     "required_action": "recommission" if released else "local_operator_required"})
        return released

    def _valid_reading(self, reading, unit, now):
        """同时检查存在性、有限值、单位、质量和数据年龄，禁止旧数据参与新动作。"""
        return (reading is not None and finite(reading.value) and reading.unit == unit
                and reading.quality == "good" and finite(reading.timestamp)
                and 0 <= now - reading.timestamp <= self.profile["max_data_age_s"])

    def _snapshot_errors(self, snapshot, now):
        """检查设备身份、快照时间、实际归属/模式、告警与所有已配置的运行守护量。"""
        errors = []
        if snapshot.asset_id != self.domain["cdu_id"]:
            errors.append("snapshot_asset_mismatch")
        if not finite(snapshot.timestamp) or not 0 <= now - snapshot.timestamp <= self.profile["max_data_age_s"]:
            errors.append("snapshot_stale_or_future")
        if snapshot.owner != self.domain["owner"]:
            errors.append("control_owner_mismatch")
        allowed_modes = {mode for capability in self.adapter.describe().controls.values()
                         for mode in capability.allowed_modes}
        if snapshot.operating_mode not in allowed_modes:
            errors.append("device_mode_mismatch")
        if snapshot.alarms:
            errors.append("device_alarm_active")
        for point, guard in self.profile["guards"].items():
            reading = snapshot.observations.get(point)
            if not self._valid_reading(reading, guard["unit"], now):
                errors.append("invalid_guard_observation:" + point)
            elif not guard["minimum"] <= reading.value <= guard["maximum"]:
                errors.append("guard_out_of_bounds:" + point)
        return errors

    def heartbeat(self, now):
        """即使没有新动作也要调用。检查工况，成功后喂设备看门狗，失败则暂停。"""
        with self.lock:
            if not finite(now):
                raise ValueError("invalid_clock")
            if self.mode != "control":
                return {"mode": self.mode}
            try:
                snapshot = self.adapter.read(now)
                errors = self._snapshot_errors(snapshot, self._now(now))
                if not errors and hasattr(self.adapter, "feed_watchdog"):
                    self.adapter.feed_watchdog()
            except Exception:
                errors = ["read_failed"]
            if errors:
                released = self._pause(";".join(errors))
                return {"mode": self.mode, "reasons": errors, "handoff_confirmed": released}
            return {"mode": self.mode, "status": "configured_guards_passed"}

    def submit(self, request, now):
        """完整命令事务入口：去重 → 申请校验 → 新采集 → 工程边界 → 预写审计 → 写入 → 回读。
        shadow 不执行；写/回读异常返回 uncertain 并暂停，不能当作安全失败后重试。"""
        with self.lock:
            now = self._now(now)
            if request.request_id in self.cache:
                original, receipt = self.cache[request.request_id]
                if original == request:
                    return receipt
                return ControlReceipt(request.request_id, "rejected", ("idempotency_conflict",))

            errors = []
            if not all(finite(v) for v in (now, request.value, request.issued_at, request.expires_at)):
                return ControlReceipt(request.request_id, "rejected", ("nonfinite_request",))
            if not request.request_id:
                errors.append("request_id_required")
            if self.mode not in ("shadow", "control"):
                errors.append("runtime_not_enabled")
            if request.asset_id != self.domain["cdu_id"]:
                errors.append("asset_mismatch")
            if request.topology_version != self.scene["topology_version"]:
                errors.append("topology_version_mismatch")
            if request.owner != self.domain["owner"]:
                errors.append("request_owner_mismatch")
            if not request.issued_at <= now < request.expires_at:
                errors.append("expired_or_future_request")
            if not self._shared_control_permitted():
                errors.append("coordinator_required")
            capabilities = self.adapter.describe()
            if capabilities.asset_id != request.asset_id:
                errors.append("adapter_asset_mismatch")
            control = capabilities.controls.get(request.quantity)
            if control is None:
                errors.append("unsupported_control")
            elif request.unit != control.unit:
                errors.append("control_unit_mismatch")
            elif not control.minimum <= request.value <= control.maximum:
                errors.append("control_out_of_bounds")
            if errors:
                return self._finish(request, "rejected", errors)

            try:
                snapshot = self.adapter.read(now)
                now = self._now(now)
                errors = self._snapshot_errors(snapshot, now)
                if now >= request.expires_at:
                    errors.append("expired_during_acquisition")
            except Exception:
                errors = ["read_failed"]
                snapshot = None
            if errors:
                if self.mode == "control":
                    self._pause(";".join(errors))
                return self._finish(request, "rejected", errors)
            if snapshot.operating_mode not in control.allowed_modes:
                errors.append("device_mode_mismatch")
            # 真实设备的温度目标不能只检查固定最小值：凝露边界随环境变化。
            # 允许已验收的 OEM 本地防凝露保护，或使用实时露点观测加工程裕量。
            if request.quantity == "cdu.sec_supply_temp_sp" and capabilities.deployment == "hardware":
                protection = self.profile.get("temperature_protection", {})
                local = (protection.get("mode") == "oem_local" and protection.get("verified") is True
                         and bool(protection.get("evidence_reference")))
                if not local:
                    dew = snapshot.observations.get("environment.dew_point")
                    margin = protection.get("margin_k")
                    if (protection.get("mode") != "observed_dew_point" or not finite(margin) or margin < 0
                            or not self._valid_reading(dew, "K", now)):
                        errors.append("temperature_protection_not_verified")
                    elif request.value + 273.15 < dew.value + margin:
                        errors.append("temperature_below_dew_point_margin")
            current = snapshot.setpoints.get(request.quantity)
            if not self._valid_reading(current, control.unit, now):
                errors.append("setpoint_readback_invalid")
            elif self._step_exceeded(abs(request.value - current.value), control.max_step):
                errors.append("max_step_exceeded")
            if hasattr(self.adapter, "normalized_value") and not errors:
                try:
                    wire_value = self.adapter.normalized_value(request.quantity, request.value)
                    if not control.minimum <= wire_value <= control.maximum:
                        errors.append("encoded_control_out_of_bounds")
                    elif self._step_exceeded(abs(wire_value - current.value), control.max_step):
                        errors.append("encoded_max_step_exceeded")
                    elif (request.quantity == "cdu.sec_supply_temp_sp" and capabilities.deployment == "hardware"
                          and not local and wire_value + 273.15 < dew.value + margin):
                        errors.append("encoded_temperature_below_dew_point_margin")
                except (ValueError, OverflowError):
                    errors.append("control_not_representable")
            previous_time = self.last_write.get(request.quantity)
            if previous_time is not None and now - previous_time < control.minimum_interval_s:
                errors.append("minimum_interval_not_met")
            if errors:
                return self._finish(request, "rejected", errors)
            if self.mode == "shadow":
                return self._finish(request, "shadow", ["not_executed"])

            self._audit({"event": "dispatch_intent", "at": now, "request": asdict(request)})
            try:
                self.adapter.write(request)
                self.last_write[request.quantity] = now
                after = self.adapter.read(now)
                now = self._now(now)
                feedback = after.setpoints.get(request.quantity)
                valid = (not self._snapshot_errors(after, now)
                         and after.operating_mode in control.allowed_modes
                         and self._valid_reading(feedback, control.unit, now)
                         and abs(feedback.value - request.value) <= (
                             self.adapter.tolerance(request.quantity) if hasattr(self.adapter, "tolerance") else 1e-8))
                if not valid:
                    raise RuntimeError("readback_mismatch")
            except Exception:
                released = self._pause("write_or_readback_uncertain")
                return self._finish(request, "uncertain", ["write_or_readback_uncertain",
                                      "handoff_confirmed" if released else "handoff_unconfirmed"])
            return self._finish(request, "setpoint_confirmed", [], feedback.value)

    def _finish(self, request, status, reasons, readback=None):
        """缓存请求/回执并持久化结果。先缓存再写完成日志，避免同进程因审计异常重发已经执行的动作。"""
        receipt = ControlReceipt(request.request_id, status, tuple(reasons), readback,
                                 provenance=self.adapter.describe().deployment)
        # Save in memory before completion audit so retry cannot repeat an uncertain write.
        self.cache[request.request_id] = (request, receipt)
        self._audit({"event": "receipt", "request": asdict(request), "receipt": asdict(receipt)})
        return receipt
