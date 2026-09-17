"""边缘工作台的应用服务；不依赖 HTTP 框架，也不把浏览器变成控制循环。

配置草稿/发布版本与任务元数据使用原子 JSON 文件保存。每个后台任务独占
Engine、SQLite writer 和设备适配器；页面查询始终打开短生命周期的只读连接。
停止任务意味着退出本软件控制并尝试本地接管，不是停泵。历史任务仅供回放，
服务重启不会恢复设备写入。任何网络采集都标为 network_unverified，而非实机证明。
"""
import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import ExitStack, closing
from pathlib import Path

from .configuration import validate_scene
from .engine import EndpointLock, Engine
from .operations import domain_of, load_at

MASK = "[REDACTED]"
ACTIVE = {"starting", "running", "stop_requested"}
KINDS = {"all", "session", "telemetry", "decision", "calibration", "fault", "model",
         "forecast_input_error", "forecast_usage"}


class WorkbenchError(ValueError):
    """可安全交给 API 客户端的结构化错误，status 为建议 HTTP 状态码。"""
    def __init__(self, code, message=None, status=400):
        self.code, self.message, self.status = code, message or code, status
        super().__init__(self.message)


def _secret_key(key):
    """仅遮蔽凭据值，保留 secret_ref 等引用，避免正常工程字段丢失。"""
    key = str(key).lower().replace("-", "_")
    if key.endswith("_ref"):
        return False
    return any(word in key for word in ("password", "passwd", "secret", "credential", "token", "private_key", "api_key"))


def redact(value, secrets=()):
    """递归脱敏配置与事件；已知秘密若被嵌入错误文本，也不能出现在 UI 中。"""
    if isinstance(value, dict):
        return {k: MASK if _secret_key(k) and v not in (None, "") else redact(v, secrets)
                for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secrets) for v in value]
    if isinstance(value, str):
        for secret in secrets:
            if isinstance(secret, str) and secret:
                value = value.replace(secret, MASK)
        # 连接字符串有时把口令放在 URL 的 user:password@ 中。
        return re.sub(r"(\w+://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", value)
    return value


def _secret_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if _secret_key(key) and isinstance(item, str) and item:
                yield item
            else:
                yield from _secret_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _secret_values(item)


def _restore_masks(value, previous=None):
    """脱敏配置往返时保留原值；没有来源的遮蔽值绝不能成为实际凭据。"""
    if isinstance(value, str) and value in (MASK, "***", "********"):
        if previous is None or previous in (MASK, "***", "********"):
            raise WorkbenchError("masked_secret_requires_source", "遮蔽凭据需要原草稿或 source_config_id，不能作为新凭据保存。")
        return copy.deepcopy(previous)
    if isinstance(value, dict):
        old = previous if isinstance(previous, dict) else {}
        return {k: _restore_masks(v, old.get(k)) for k, v in value.items()}
    if isinstance(value, list):
        old = previous if isinstance(previous, list) else []
        return [_restore_masks(v, old[i] if i < len(old) else None) for i, v in enumerate(value)]
    return value


def _atomic_json(path, payload, sort_keys=False):
    """同目录临时文件、fsync 与替换，防止断电或并发读取得到半份配置。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink():
        raise WorkbenchError("unsafe_state_path")
    temp = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temp.open("x", encoding="utf-8") as stream:
            os.chmod(temp, 0o600)
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False, sort_keys=sort_keys)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        descriptor = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        if temp.exists():
            temp.unlink()


def _number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise WorkbenchError("invalid_" + name, "%s 必须在 %s 到 %s 之间。" % (name, low, high))
    return value


class Workbench:
    """单边缘服务的配置仓库、历史查询和有限后台任务管理器。

    project_root 必须指向含 examples/ 和 outputs/ 的 lc-control 工程。外部 API
    只接收不透明 ID，不接收路径。state_dir 是管理员启动配置，不从浏览器输入。
    allow_hardware_control 仅增加启动许可，原设备验收/网关检查仍全部执行。
    """
    def __init__(self, project_root, state_dir=None, allow_hardware_control=False):
        self.root = Path(project_root).resolve()
        self.state = Path(state_dir).resolve() if state_dir else self.root / "outputs" / "workbench"
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        for name in ("configs", "jobs", "runs", "forecasts", "forecast_archive"):
            directory = self.state / name
            if directory.is_symlink():
                raise WorkbenchError("unsafe_state_path")
            directory.mkdir(exist_ok=True, mode=0o700)
        self.allow_hardware_control = bool(allow_hardware_control)
        self._lock = threading.RLock()
        self._configs, self._jobs, self._threads, self._stops, self._runs = {}, {}, {}, {}, {}
        self._forecasts = {}
        self._forecast_ids = {}
        self._closed = False
        self._state_lock = None
        # 端口不能代表仓库归属：两个进程可以绑定不同端口，却修改相同任务元数据。
        # 必须先取得状态目录的跨进程锁，才允许恢复或标记旧任务；失败的第二实例
        # 连 _load_state 都不能执行，以免把仍在运行的控制任务误标为 interrupted。
        ownership = EndpointLock("workbench-state:" + str(self.state.resolve()))
        try:
            ownership.__enter__()
        except RuntimeError:
            raise WorkbenchError("workbench_state_in_use", "此状态目录已有工作台实例运行，请使用现有工作台。", 409)
        self._state_lock = ownership
        try:
            self._load_templates()
            self._load_state()
            self._load_forecasts()
            self._scan_runs()
        except BaseException:
            # 初始化异常和 Ctrl-C 均不能遗留锁，防止用户修复配置后仍无法启动。
            ownership.__exit__(None, None, None)
            self._state_lock = None
            raise

    def _load_templates(self):
        """只加载场景模板；矩阵/报告 JSON 不被误当成可运行设备配置。"""
        directory = self.root / "examples"
        if not directory.is_dir() or directory.is_symlink():
            return
        for path in sorted(directory.glob("*.json"))[:200]:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 2_000_000:
                continue
            try:
                scene = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(scene, dict) or "assets" not in scene or "devices" not in scene:
                    continue
                identity = "template-" + hashlib.sha256(path.name.encode()).hexdigest()[:20]
                self._configs[identity] = {"id": identity, "name": path.stem, "revision": 1,
                    "status": "template", "origin": "examples/" + path.name,
                    "scene_id": scene.get("scene_id"), "scene": scene,
                    "created_at": path.stat().st_mtime, "updated_at": path.stat().st_mtime}
            except (OSError, ValueError):
                continue

    def _load_state(self):
        """恢复已落盘数据；遗留运行只能标记中断，不自动恢复控制权。"""
        for group, target in (("configs", self._configs), ("jobs", self._jobs)):
            for path in sorted((self.state / group).glob("*.json"))[:1000]:
                if path.is_symlink() or path.stat().st_size > 2_000_000:
                    continue
                try:
                    item = json.loads(path.read_text(encoding="utf-8"))
                    identity = item.get("id", "")
                    prefix = "config-" if group == "configs" else "job-"
                    if not re.fullmatch(prefix + r"[a-f0-9]{32}", identity) or path.stem != identity:
                        continue
                    if group == "configs" and (item.get("status") not in {"draft", "published"} or not isinstance(item.get("scene"), dict)):
                        continue
                    if group == "jobs" and item.get("status") in ACTIVE:
                        item.update(status="interrupted", active=False, handoff_confirmed=None,
                                    termination_reason="service_restarted_no_control_resumed", updated_at=time.time(),
                                    required_action="local_operator_required" if item.get("mode") == "control" else None)
                        self._write_job(item)
                    target[identity] = item
                    if group == "jobs":
                        output = self.state / "runs" / identity
                        if not output.is_symlink():
                            self._runs[item.get("run_id", "run-" + identity[4:])] = output / "runtime.sqlite"
                except (OSError, ValueError, TypeError):
                    continue

    def _scan_runs(self):
        """有界扫描本工程 outputs，跳过全部符号链接，避免读取外部数据库。"""
        outputs = self.root / "outputs"
        if not outputs.is_dir() or outputs.is_symlink():
            return
        seen_dirs = 0
        for directory, dirs, files in os.walk(outputs, followlinks=False):
            seen_dirs += 1
            here = Path(directory)
            depth = len(here.relative_to(outputs).parts)
            dirs[:] = sorted(d for d in dirs if not (here / d).is_symlink()) if depth < 8 else []
            if seen_dirs > 4096 or len(self._runs) >= 512:
                break
            if "runtime.sqlite" not in files:
                continue
            path = here / "runtime.sqlite"
            if path.is_symlink() or not path.is_file():
                continue
            if path in self._runs.values():
                continue
            relative = path.relative_to(self.root).as_posix()
            self._runs["run-" + hashlib.sha256(relative.encode()).hexdigest()[:24]] = path

    def _load_forecasts(self):
        """恢复输入记录，但不会因此恢复任务；运行时重新检查时间基准与有效期。"""
        from .forecasting import ForecastError, validate_input
        for path in sorted((self.state / "forecasts").glob("config-*.json"))[:1000]:
            if path.is_symlink() or path.stat().st_size > 16_000_000:
                continue
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                config = self._configs.get(path.stem)
                if not config or config["status"] != "published" or record.get("config_hash") != config.get("scene_hash"):
                    continue
                if record.get("input") is not None:
                    record["input"] = validate_input(config["scene"], record["input"], record["input"]["issued_at"])
                    digest = self._archive_forecast(path.stem, record["input"])
                    if record.get("input_sha256") not in (None, digest):
                        continue
                    self._bind_forecast_id(path.stem, record["input"]["forecast_id"], digest)
                    if record.get("input_sha256") is None:
                        # 旧版只有 current 的记录，在启用前补存原始包；不续写发行时间。
                        record["input_sha256"] = digest
                        _atomic_json(path, record)
                self._forecasts[path.stem] = record
            except (OSError, ValueError, KeyError, TypeError, ForecastError):
                continue

    def _published_config(self, identity):
        config = self._raw_config(identity)
        if config["status"] != "published":
            raise WorkbenchError("published_config_required", "预测必须绑定不可变的已发布配置。", 409)
        return config

    def forecast_descriptor(self, config_id):
        """公开实际配置的预测契约、可引用资产和显式合成测试模板。"""
        from .forecasting import descriptor
        with self._lock:
            config = copy.deepcopy(self._published_config(config_id))
        return self._public(descriptor(config["scene"], config_id, config["revision"]))

    def _forecast_reference(self, config_id, package, job_id=None):
        """仿真时间来自指定任务的相对秒；无任务时仅预览运行起点 0。"""
        if job_id is not None:
            job = self._jobs.get(job_id) if isinstance(job_id, str) else None
            if not job or job["config_id"] != config_id:
                raise WorkbenchError("forecast_job_config_mismatch")
            clock = "simulation" if job["kind"] == "thermal_sim" else "unix"
            now = job.get("elapsed_s", 0) if clock == "simulation" else time.time()
            return {"job_id": job_id, "clock_basis": clock, "now": now,
                    "job_status": job["status"], "meaning": "job_relative_seconds" if clock == "simulation" else "unix_seconds"}
        clock = package.get("clock_basis", "simulation") if package else "simulation"
        return {"job_id": None, "clock_basis": clock, "now": 0 if clock == "simulation" else time.time(),
                "meaning": "preview_at_run_start_not_execution" if clock == "simulation" else "wall_clock_preview_not_execution"}

    def _validated_forecast(self, config_id, payload):
        from .forecasting import ForecastError, validate_input
        if not isinstance(payload, dict):
            raise WorkbenchError("forecast_object_required")
        config = self._published_config(config_id)
        reference = self._forecast_reference(config_id, payload, payload.get("job_id"))
        if reference["clock_basis"] != payload.get("clock_basis"):
            raise WorkbenchError("forecast_clock_mismatch", "预测时间基准与所选任务不一致。")
        try:
            package = validate_input(config["scene"], payload, reference["now"])
        except ForecastError as exc:
            raise WorkbenchError(exc.code, exc.message, 422)
        expected = self._forecast_id_index(config_id).get(package["forecast_id"])
        if expected is not None and expected != hashlib.sha256(self._forecast_bytes(package)).hexdigest():
            raise WorkbenchError("forecast_id_conflict", "预测编号已绑定其他内容；即使清除或重启，也必须使用新编号。", 409)
        previous = self._forecasts.get(config_id, {}).get("input")
        if previous and previous["forecast_id"] == package["forecast_id"] and previous != package:
            raise WorkbenchError("forecast_id_conflict", "同一预测编号不能覆盖不同内容，请使用新的 forecast_id。", 409)
        if previous and previous["clock_basis"] == package["clock_basis"] and package["issued_at"] < previous["issued_at"]:
            raise WorkbenchError("out_of_order_forecast", "不能用更旧的预测替换已经收到的新预测。", 409)
        return config, package, reference

    def validate_forecast(self, config_id, payload):
        """只校验和预览，不保存，也不会使已有任务使用正在编辑的预测。"""
        from .forecasting import preview
        try:
            with self._lock:
                config, package, reference = self._validated_forecast(config_id, payload)
                result = {"valid": True, "errors": [], "input": package, "reference": reference,
                          "config_id": config_id, "config_revision": config["revision"],
                          **preview(config["scene"], package, reference["now"], reference["clock_basis"])}
            return self._public(result)
        except WorkbenchError as exc:
            return {"valid": False, "errors": [{"code": exc.code, "message": exc.message}]}

    def submit_forecast(self, config_id, payload):
        """原子接收一份有界预测；当前/后续匹配任务在下个周期读取，未创建任务不执行。"""
        with self._lock:
            config, package, reference = self._validated_forecast(config_id, payload)
            # 先保存不可变原曲线，再切换 current 指针；任一步失败都不能激活新包。
            digest = self._archive_forecast(config_id, package)
            self._bind_forecast_id(config_id, package["forecast_id"], digest)
            record = {"config_id": config_id, "config_revision": config["revision"], "config_hash": config["scene_hash"],
                      "input": package, "input_sha256": digest, "received_at_unix": time.time(), "submitted_for_job_id": reference["job_id"]}
            _atomic_json(self.state / "forecasts" / (config_id + ".json"), record)
            self._forecasts[config_id] = record
        return self.get_forecast(config_id, reference["job_id"])

    def _archive_forecast(self, config_id, package):
        """内容寻址归档；文件名是其 UTF-8 文件字节的 SHA-256，可直接 sha256sum。

        只使用已验证配置 ID 与本地计算的哈希构造路径。已存在文件只核验，不覆盖；
        配置、日期、forecast_id 都不会作为任意路径片段。归档保留原始 canonical 包，
        即使 current 更新或清除，运行事件中的 input_sha256 仍可定位确切输入。
        """
        if not isinstance(config_id, str) or not re.fullmatch(r"config-[a-f0-9]{32}", config_id):
            raise WorkbenchError("invalid_archive_config_id")
        content = self._forecast_bytes(package)
        if len(content) > 16_000_000:
            raise WorkbenchError("forecast_archive_too_large", status=413)
        digest = hashlib.sha256(content).hexdigest()
        base = self.state / "forecast_archive"
        directory = base / config_id
        if base.is_symlink() or directory.is_symlink():
            raise WorkbenchError("unsafe_forecast_archive_path", status=403)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / (digest + ".json")
        if path.is_symlink():
            raise WorkbenchError("unsafe_forecast_archive_path", status=403)
        if path.exists():
            if not path.is_file() or path.stat().st_size != len(content) or path.read_bytes() != content:
                raise WorkbenchError("forecast_archive_integrity_error", "既有预测归档内容与哈希不一致，输入未激活。", 409)
        else:
            _atomic_json(path, package, sort_keys=True)
        return digest

    @staticmethod
    def _forecast_bytes(package):
        """归档和编号索引统一使用同一字节表示，避免序列化顺序造成错误匹配。"""
        return (json.dumps(package, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")

    def _forecast_id_index(self, config_id):
        """只在校验/接收时读取一次索引；旧归档做有界迁移，控制周期不扫描目录。

        无索引时迁移结果先留在内存，提交才持久化；仅校验输入不会创建档案。
        迁移必须核对每个文件的字节哈希；历史出现同编号不同内容时明确拒绝。
        """
        if config_id in self._forecast_ids:
            return self._forecast_ids[config_id]
        if not re.fullmatch(r"config-[a-f0-9]{32}", config_id):
            raise WorkbenchError("invalid_archive_config_id")
        base = self.state / "forecast_archive"
        directory, ids = base / config_id, {}
        path = directory / "id_index.json"
        if base.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise WorkbenchError("unsafe_forecast_archive_path", status=403)
        if path.exists():
            if not path.is_file() or path.stat().st_size > 2_000_000:
                raise WorkbenchError("forecast_id_index_invalid")
            record = json.loads(path.read_text(encoding="utf-8"))
            ids = record.get("ids") if isinstance(record, dict) and record.get("schema_version") == 1 else None
            if not isinstance(ids, dict) or any(not isinstance(k, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", k)
                or not isinstance(v, str) or not re.fullmatch(r"[a-f0-9]{64}", v) for k, v in ids.items()):
                raise WorkbenchError("forecast_id_index_invalid")
        elif directory.exists():
            size = 0
            for count, archive in enumerate(directory.iterdir(), 1):
                if count > 4096:
                    raise WorkbenchError("forecast_archive_migration_limit", "历史归档超过自动迁移范围，请先离线整理。")
                if not re.fullmatch(r"[a-f0-9]{64}\.json", archive.name):
                    continue
                if archive.is_symlink() or not archive.is_file():
                    raise WorkbenchError("unsafe_forecast_archive_path", status=403)
                size += archive.stat().st_size
                if size > 64_000_000:
                    raise WorkbenchError("forecast_archive_migration_limit")
                content = archive.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                if digest != archive.stem:
                    raise WorkbenchError("forecast_archive_integrity_error", "历史归档与文件名哈希不一致，迁移未完成。", 409)
                package = json.loads(content)
                identity = package.get("forecast_id") if isinstance(package, dict) else None
                if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", identity):
                    raise WorkbenchError("forecast_archive_identity_invalid")
                if identity in ids and ids[identity] != digest:
                    raise WorkbenchError("forecast_archive_id_conflict", "历史已有同编号不同内容，需先核对归档。", 409)
                ids[identity] = digest
        self._forecast_ids[config_id] = ids
        return ids

    def _bind_forecast_id(self, config_id, identity, digest):
        """原子保存不可逆编号绑定，再允许 current 切换；clear 不删除绑定。"""
        ids = dict(self._forecast_id_index(config_id))
        if identity in ids and ids[identity] != digest:
            raise WorkbenchError("forecast_id_conflict", "预测编号已绑定其他内容。", 409)
        ids[identity] = digest
        base = self.state / "forecast_archive"
        directory = base / config_id
        path = directory / "id_index.json"
        if base.is_symlink() or directory.is_symlink() or path.is_symlink():
            raise WorkbenchError("unsafe_forecast_archive_path", status=403)
        if len(json.dumps({"schema_version": 1, "ids": ids}, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")) + 1 > 2_000_000:
            raise WorkbenchError("forecast_id_index_capacity", "预测编号索引已达当前存储范围，输入未激活。", 409)
        _atomic_json(path, {"schema_version": 1, "ids": ids}, sort_keys=True)
        self._forecast_ids[config_id] = ids

    def clear_forecast(self, config_id):
        """移除前馈输入；下周期恢复仅依靠原有观测反馈，不发送撤销或停泵命令。"""
        with self._lock:
            config = self._published_config(config_id)
            record = {"config_id": config_id, "config_revision": config["revision"], "config_hash": config["scene_hash"],
                      "input": None, "input_sha256": None, "cleared_at_unix": time.time()}
            _atomic_json(self.state / "forecasts" / (config_id + ".json"), record)
            self._forecasts[config_id] = record
        return self.get_forecast(config_id)

    def get_forecast(self, config_id, job_id=None):
        """区分当前聚合预览 ready 与实际控制周期的 usage，不能将预览宣传为已执行。"""
        from .forecasting import preview
        with self._lock:
            config = self._published_config(config_id)
            record = copy.deepcopy(self._forecasts.get(config_id, {}))
            package = record.get("input")
            reference = self._forecast_reference(config_id, package, job_id)
            usage = [{"job_id": job["id"], "run_id": job["run_id"], **copy.deepcopy(job["forecast_usage"])}
                     for job in self._jobs.values() if job.get("config_id") == config_id and job.get("forecast_usage")
                     and (job_id is None or job_id == job["id"])]
            result = {"config_id": config_id, "config_revision": config["revision"], "input": package,
                      "input_sha256": record.get("input_sha256"),
                      "received_at_unix": record.get("received_at_unix"), "reference": reference, "usage": usage,
                      **preview(config["scene"], package, reference["now"], reference["clock_basis"])}
        return self._public(result)

    def _forecast_for_tick(self, job, scene, now):
        from .forecasting import resolve_domain
        with self._lock:
            record = self._forecasts.get(job["config_id"], {})
            package = copy.deepcopy(record.get("input"))
            digest = record.get("input_sha256")
        clock = "simulation" if job["kind"] == "thermal_sim" else "unix"
        internal, detail = resolve_domain(scene, job["domain_id"], package, now, clock)
        # 周期审计不重复保存整条预测曲线，输入本身保存在版本绑定的独立记录中。
        usage = {k: v for k, v in detail.items() if k not in {"series", "racks"}}
        usage["input_sha256"] = digest
        return internal, usage

    def _public(self, item):
        with self._lock:
            secrets = tuple(_secret_values(item)) + tuple(s for config in self._configs.values() for s in _secret_values(config.get("scene", {})))
        return redact(copy.deepcopy(item), secrets)

    def _inspect(self, scene):
        from .site import inspect_scene
        return inspect_scene(copy.deepcopy(scene))

    def validate(self, payload):
        """仅做离线语义/能力检查，绝不访问设备。"""
        scene = payload.get("scene", payload) if isinstance(payload, dict) else payload
        if not isinstance(scene, dict):
            raise WorkbenchError("scene_object_required")
        return self._public(self._inspect(scene))

    def _raw_config(self, identity):
        if not isinstance(identity, str):
            raise WorkbenchError("config_id_required")
        config = self._configs.get(identity)
        if config is None:
            raise WorkbenchError("config_not_found", status=404)
        return config

    def get_config(self, identity):
        with self._lock:
            record = copy.deepcopy(self._raw_config(identity))
        record["validation"] = self._inspect(record["scene"])
        return self._public(record)

    def save_draft(self, payload):
        """创建草稿或乐观锁更新；已发布版本不可覆盖，复制时产生新 ID。"""
        if not isinstance(payload, dict) or not isinstance(payload.get("scene"), dict):
            raise WorkbenchError("scene_object_required")
        if len(json.dumps(payload["scene"], ensure_ascii=False, allow_nan=False)) > 2_000_000:
            raise WorkbenchError("scene_too_large", status=413)
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 160:
            raise WorkbenchError("invalid_config_name")
        with self._lock:
            identity = payload.get("id")
            old = self._raw_config(identity) if identity else None
            if old:
                if old["status"] != "draft":
                    raise WorkbenchError("immutable_config", "模板和发布版本不可覆盖，请另建草稿。", 409)
                self._check_revision(old, payload.get("expected_revision"))
            source = old
            if source is None and payload.get("source_config_id"):
                source = self._raw_config(payload["source_config_id"])
            scene = _restore_masks(copy.deepcopy(payload["scene"]), source.get("scene") if source else None)
            now = time.time()
            record = {"id": identity or "config-" + uuid.uuid4().hex, "name": name.strip(),
                      "revision": old["revision"] + 1 if old else 1, "status": "draft",
                      "origin": "workbench", "scene_id": scene.get("scene_id"), "scene": scene,
                      "created_at": old["created_at"] if old else now, "updated_at": now}
            if source and not old:
                record["derived_from"] = {"id": source["id"], "revision": source["revision"]}
            _atomic_json(self.state / "configs" / (record["id"] + ".json"), record)
            self._configs[record["id"]] = record
        return self.get_config(record["id"])

    @staticmethod
    def _check_revision(record, expected):
        if type(expected) is not int or expected != record["revision"]:
            raise WorkbenchError("revision_conflict", "配置已变化，请刷新后重新检查差异。", 409)

    def publish(self, identity, expected_revision):
        """离线校验后冻结版本；发布不创建设备连接，也不启动控制。"""
        with self._lock:
            old = self._raw_config(identity)
            self._check_revision(old, expected_revision)
            if old["status"] != "draft":
                raise WorkbenchError("immutable_config", status=409)
            validation = self._inspect(old["scene"])
            if not validation.get("valid", False):
                raise WorkbenchError("invalid_configuration", "配置检查未通过，不能发布。", 422)
            record = copy.deepcopy(old)
            record.update(status="published", revision=old["revision"] + 1,
                          published_at=time.time(), updated_at=time.time())
            record["scene_hash"] = hashlib.sha256(json.dumps(record["scene"], sort_keys=True).encode()).hexdigest()
            _atomic_json(self.state / "configs" / (identity + ".json"), record)
            self._configs[identity] = record
        return self.get_config(identity)

    def _safe_run_path(self, identity):
        path = self._runs.get(identity)
        if path is None:
            raise WorkbenchError("run_not_found", status=404)
        # 重新检查路径：扫描后被替换成符号链接也不能穿出允许根目录。
        allowed = [self.root / "outputs", self.state / "runs"]
        for base in allowed:
            try:
                relative = path.relative_to(base)
            except ValueError:
                continue
            cursor = base
            if cursor.is_symlink():
                continue
            if any((base.joinpath(*relative.parts[:i])).is_symlink() for i in range(1, len(relative.parts) + 1)):
                continue
            return path
        raise WorkbenchError("unsafe_run_path", status=403)

    def _db(self, path):
        """每个查询新建只读句柄，绝不跨线程复用 Engine 的写连接。"""
        if any(Path(str(path) + suffix).is_symlink() for suffix in ("-wal", "-shm", "-journal")):
            raise WorkbenchError("unsafe_run_path", status=403)
        db = None
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
            db.execute("PRAGMA query_only=ON")
            db.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
            # get_run 的 session 和各类 latest 必须来自同一个读快照，避免新会话
            # 在两次 SELECT 中间启动，导致旧配置与新遥测混合显示。
            db.execute("BEGIN")
            return db
        except sqlite3.OperationalError:
            if db is not None:
                db.close()
            with self._lock:
                active = any(j.get("status") in ACTIVE and self._runs.get(j.get("run_id")) == path
                             for j in self._jobs.values())
            # 旧 FMU 报告拷贝只有已完成的主库文件，可能保留 WAL 模式标记但不能
            # 创建共享内存文件。仅对无活动任务且无任何 WAL/SHM 的历史快照回退。
            # 有 WAL 时必须保留正常读事务，否则会漏掉尚未 checkpoint 的新事件。
            if active or any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm")):
                raise
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True, timeout=2)
            db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN")
            return db

    @staticmethod
    def _session(db):
        row = db.execute("SELECT id,payload FROM events WHERE kind='session' ORDER BY id DESC LIMIT 1").fetchone()
        return (row[0], json.loads(row[1])) if row else (0, {})

    def _job_for_run(self, identity):
        return next((copy.deepcopy(j) for j in self._jobs.values() if j.get("run_id") == identity), None)

    @staticmethod
    def _source(scene):
        adapters = {p.get("adapter") for p in scene.get("devices", {}).values()}
        if "sustain_fmu" in adapters:
            return "sustain_fmu"
        if "modbus_tcp" in adapters:
            return "modbus_network_unverified"
        if "thermal_sim" in adapters:
            return "synthetic_thermal"
        if "mock" in adapters:
            return "synthetic_mock"
        return "historical_unknown"

    def get_run(self, identity):
        with self._lock:
            path = self._safe_run_path(identity)
            job = self._job_for_run(identity)
        latest = {kind: None for kind in ("telemetry", "decision", "calibration", "fault", "forecast_usage")}
        session = {}
        if path.is_file():
            try:
                with closing(self._db(path)) as db:
                    first, session = self._session(db)
                    for kind in latest:
                        row = db.execute("SELECT id,at,payload FROM events WHERE kind=? AND id>=? ORDER BY id DESC LIMIT 1", (kind, first)).fetchone()
                        if row:
                            latest[kind] = {"id": row[0], "at": row[1], "kind": kind, "payload": json.loads(row[2])}
            except (sqlite3.Error, ValueError, OSError):
                raise WorkbenchError("run_unreadable", "运行数据库暂不可读或格式不受支持。", 422)
        elif not job:
            raise WorkbenchError("run_not_found", status=404)
        scene = session.get("scene", {})
        if not scene and job:
            with self._lock:
                scene = copy.deepcopy(self._configs.get(job.get("config_id"), {}).get("scene", {}))
        validation = self._inspect(scene) if scene else {}
        label = job.get("name", job["id"]) if job else path.parent.relative_to(self.root / "outputs").as_posix()
        source = job["source"] if job else self._source(scene)
        result = {"id": identity, "label": label, "source": source, "scene": scene,
                  "session": session, "job": job, "latest": latest,
                  "topology": validation.get("topology", {}), "capabilities": validation.get("capabilities", []),
                  "validation": validation, "active": bool(job and job.get("status") in ACTIVE),
                  "updated_at": job.get("updated_at") if job else path.stat().st_mtime}
        return self._public(result)

    def events(self, run_id, kind="telemetry", after=0, limit=200, tail=False):
        """分页当前会话事件。after/next_cursor 使用持久化事件 ID，不是数组偏移。"""
        if kind not in KINDS:
            raise WorkbenchError("unsupported_event_kind")
        if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise WorkbenchError("invalid_event_pagination")
        path = self._safe_run_path(run_id)
        if not path.is_file():
            return {"items": [], "next_cursor": after}
        try:
            with closing(self._db(path)) as db:
                first, session = self._session(db)
                where, args = "id>=? AND id>?", [first, after]
                if kind != "all":
                    where += " AND kind=?"
                    args.append(kind)
                order = "DESC" if tail else "ASC"
                rows = db.execute("SELECT id,at,kind,payload FROM events WHERE " + where + " ORDER BY id " + order + " LIMIT ?", args + [limit]).fetchall()
                if tail:
                    rows.reverse()
                items = [{"id": row[0], "at": row[1], "kind": row[2], "payload": json.loads(row[3])} for row in rows]
                return self._public(redact({"items": items, "next_cursor": items[-1]["id"] if items else after}, tuple(_secret_values(session))))
        except (sqlite3.Error, ValueError, OSError):
            raise WorkbenchError("run_unreadable", status=422)

    def catalog(self):
        from .registry import registry_report
        with self._lock:
            self._scan_runs()
            configs = [{k: copy.deepcopy(c.get(k)) for k in ("id", "name", "revision", "status", "origin", "scene_id", "updated_at")} for c in self._configs.values()]
            identities = list(self._runs)
        runs = []
        for identity in identities:
            try:
                run = self.get_run(identity)
                runs.append({k: run[k] for k in ("id", "label", "source", "active", "updated_at")})
            except WorkbenchError:
                continue
        runs.sort(key=lambda r: (r["active"], r["updated_at"] or 0), reverse=True)
        return self._public({"configs": configs, "runs": runs, "jobs": self.list_jobs(),
                             "registry": registry_report(), "hardware_control_enabled": self.allow_hardware_control})

    def _write_job(self, job):
        _atomic_json(self.state / "jobs" / (job["id"] + ".json"), job)

    def _update_job(self, identity, **changes):
        with self._lock:
            job = self._jobs[identity]
            job.update(changes, updated_at=time.time())
            job["active"] = job["status"] in ACTIVE
            self._write_job(job)
            return copy.deepcopy(job)

    def list_jobs(self):
        with self._lock:
            return self._public(sorted(self._jobs.values(), key=lambda j: j.get("created_at", 0), reverse=True))

    def start_job(self, payload):
        """只运行不可变发布版本。首版开放热工合成仿真与原有 Modbus 会话。

        speed 仅压缩仿真等待时间，不改变数值步长；现场网络时钟始终用真实时间。
        共享水路可读取，不能通过工作台绕过原协调器缺失保护。
        """
        if not isinstance(payload, dict):
            raise WorkbenchError("job_object_required")
        with self._lock:
            if self._closed:
                raise WorkbenchError("workbench_closed", status=503)
            if sum(j["status"] in ACTIVE for j in self._jobs.values()) >= 4:
                raise WorkbenchError("job_capacity_reached", status=409)
            config = self._raw_config(payload.get("config_id"))
            if config["status"] != "published":
                raise WorkbenchError("published_config_required", "请先发布配置，再创建运行会话。", 409)
            scene = copy.deepcopy(config["scene"])
            validate_scene(scene)
            try:
                domain = domain_of(scene, payload.get("domain_id"))
            except (ValueError, StopIteration):
                raise WorkbenchError("valid_domain_required")
            profile = scene["devices"][domain["cdu_id"]]
            kind = payload.get("kind", "thermal_sim" if profile["adapter"] == "thermal_sim" else "modbus")
            if kind not in {"thermal_sim", "modbus"} or profile["adapter"] != {"thermal_sim": "thermal_sim", "modbus": "modbus_tcp"}.get(kind):
                raise WorkbenchError("job_adapter_mismatch", "该配置不支持所选任务类型；FMU 使用已有独立实验入口。")
            mode = payload.get("mode", "control" if kind == "thermal_sim" else "monitor")
            if mode not in {"monitor", "shadow", "control"}:
                raise WorkbenchError("invalid_runtime_mode")
            if mode != "monitor" and not profile.get("policy"):
                raise WorkbenchError("policy_required_for_automatic_operation")
            if mode == "control" and domain["shared_hydraulics"]:
                raise WorkbenchError("shared_hydraulics_coordinator_required")
            inspection = self._inspect(scene)
            capability = next((item for item in inspection.get("capabilities", [])
                               if item.get("domain_id") == domain["id"]), {})
            # 发布意味着结构有效，并不意味着具备自动控制所需全部观测。
            # 网络 control 的授权和验收单独检查，不能被静态模式报告代替。
            needed_mode = "shadow" if kind == "modbus" and mode == "control" else mode
            if needed_mode not in capability.get("supported_modes", []):
                raise WorkbenchError("runtime_capability_unavailable", "该控制域缺少所选模式的观测、策略或协调能力。", 422)
            seconds = _number(payload.get("seconds", 300), "seconds", 1, 86400)
            interval = _number(payload.get("interval_s", 5), "interval_s", 0.1, 3600)
            speed = _number(payload.get("speed", 20 if kind == "thermal_sim" else 1), "speed", 0.1, 1000)
            if kind != "thermal_sim" and speed != 1:
                raise WorkbenchError("network_speed_must_be_one")
            if seconds / interval > 100000:
                raise WorkbenchError("too_many_control_cycles")
            if kind == "modbus" and mode == "control":
                if not self.allow_hardware_control:
                    raise WorkbenchError("hardware_control_disabled", "服务未启用现场写控制；可使用 monitor 或 shadow。", 403)
                from .modbus import ModbusCDUAdapter
                if not ModbusCDUAdapter(domain["cdu_id"], profile, domain["owner"]).commissioned():
                    raise WorkbenchError("hardware_commissioning_required", status=403)
                timeout = profile.get("commissioning", {}).get("watchdog_timeout_s")
                if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < interval < timeout / 2:
                    raise WorkbenchError("control_interval_must_be_less_than_half_verified_device_watchdog_timeout")
            now = time.time()
            identity = "job-" + uuid.uuid4().hex
            output = self.state / "runs" / identity
            output.mkdir(mode=0o700)
            job = {"id": identity, "run_id": "run-" + identity[4:], "name": str(payload.get("name") or config["name"])[:160],
                   "config_id": config["id"], "config_revision": config["revision"],
                   "config_hash": config["scene_hash"], "schema_version": scene["schema_version"],
                   "domain_id": domain["id"], "kind": kind, "mode": mode,
                   "seconds": seconds, "interval_s": interval, "speed": speed,
                   "source": "synthetic_thermal" if kind == "thermal_sim" else "modbus_network_unverified",
                   "status": "starting", "active": True, "created_at": now, "updated_at": now,
                   "elapsed_s": 0, "cycles": 0, "handoff_confirmed": None,
                   "stop_meaning": "停止本软件任务并尝试交还本地控制，不是停泵命令。"}
            self._write_job(job)
            self._jobs[identity] = job
            self._runs[job["run_id"]] = output / "runtime.sqlite"
            stop = threading.Event()
            self._stops[identity] = stop
            thread = threading.Thread(target=self._run_job, args=(identity, scene, output, stop), daemon=True,
                                      name="lc-workbench-" + identity[-8:])
            self._threads[identity] = thread
            thread.start()
            return self._public(job)

    def _run_job(self, identity, scene, output, stop):
        """每线程创建、使用并关闭完整控制实例，故障退出也执行原网关交还流程。"""
        job = copy.deepcopy(self._jobs[identity])
        domain = domain_of(scene, job["domain_id"])
        profile = scene["devices"][domain["cdu_id"]]
        engine, error, elapsed, cycles = None, None, 0, 0
        handoff, termination = None, "completed_requested_duration"
        final_status = "completed"
        try:
            with ExitStack() as stack:
                stack.enter_context(EndpointLock(str(output.resolve())))
                if job["kind"] == "modbus":
                    connection = profile["connection"]
                    endpoint = json.dumps([connection[k] for k in ("host", "port", "unit_id")])
                    stack.enter_context(EndpointLock(endpoint))
                from .registry import create_adapter
                adapter = create_adapter(domain["cdu_id"], profile, domain["owner"])
                engine = Engine(scene, domain["id"], adapter, output, job["mode"])
                forecast_provider = lambda decision_now: self._forecast_for_tick(job, scene, decision_now)
                try:
                    self._update_job(identity, status="stop_requested" if stop.is_set() else "running", session=engine.session)
                    start = time.monotonic()
                    while elapsed < job["seconds"] and not stop.is_set():
                        tick_start = time.monotonic()
                        step = min(job["interval_s"], job["seconds"] - elapsed)
                        if job["kind"] == "thermal_sim":
                            adapter.advance(step, load_at(elapsed))
                            elapsed += step
                            decision = engine.tick(elapsed, forecast_provider=forecast_provider)
                        else:
                            decision = engine.tick(time.time(), forecast_provider=forecast_provider)
                            elapsed = min(time.monotonic() - start, job["seconds"])
                        cycles += 1
                        self._update_job(identity, elapsed_s=elapsed, cycles=cycles,
                                         runtime_mode=engine.service.mode,
                                         forecast_usage=decision.get("forecast_input"))
                        if engine.service.mode == "paused" or decision.get("error"):
                            final_status, termination = "failed", "runtime_fault"
                            error = {"code": decision.get("error", "runtime_paused"), "message": decision.get("reason", "运行已暂停，请查看故障与接管记录。")}
                            break
                        if elapsed >= job["seconds"]:
                            break
                        delay = step / job["speed"] if job["kind"] == "thermal_sim" else min(job["interval_s"], job["seconds"] - elapsed)
                        stop.wait(max(0, delay - (time.monotonic() - tick_start)))
                        if job["kind"] == "modbus":
                            elapsed = min(time.monotonic() - start, job["seconds"])
                    if stop.is_set() and final_status != "failed":
                        final_status, termination = "stopped", "operator_stop_requested"
                finally:
                    engine.close()
                    pauses = [e for e in engine.service.events if e.get("event") == "paused"]
                    if pauses:
                        handoff = bool(pauses[-1].get("handoff_confirmed"))
        except Exception as exc:
            final_status, termination = "failed", "job_exception"
            # 原始网络异常可能包含远端内容，不把它直接复制到任务列表。
            error = {"code": type(exc).__name__, "message": "任务启动或运行失败，请核对配置、设备连接和故障事件。"}
        finally:
            if job["mode"] == "control" and engine and handoff is None:
                handoff = False
            self._update_job(identity, status=final_status, termination_reason=termination,
                             elapsed_s=elapsed, cycles=cycles, error=error,
                             handoff_confirmed=handoff, finished_at=time.time(),
                             required_action="local_operator_required" if handoff is False else None)
            self._release_state_lock_if_idle()

    def stop_job(self, identity):
        """提出异步停止请求；真正 stopped 与接管结果由控制线程最终写入。"""
        with self._lock:
            if identity not in self._jobs:
                raise WorkbenchError("job_not_found", status=404)
            job = self._jobs[identity]
            if job["status"] not in ACTIVE:
                return self._public(job)
            self._stops[identity].set()
            return self._public(self._update_job(identity, status="stop_requested", stop_requested_at=time.time()))

    def close(self):
        """服务退出请求所有任务停止。最多等待 15 秒；超时不会伪报本地接管。"""
        with self._lock:
            if self._closed and self._state_lock is None:
                return
            self._closed = True
            identities = [key for key, j in self._jobs.items() if j["status"] in ACTIVE]
        for identity in identities:
            self.stop_job(identity)
        deadline = time.monotonic() + 15
        for thread in list(self._threads.values()):
            thread.join(max(0, deadline - time.monotonic()))
        self._release_state_lock_if_idle()

    def _release_state_lock_if_idle(self):
        """最后一个后台任务完成落盘后才释放归属；超时关闭仍保留仓库独占。

        close 可与任务 finally 同时调用。锁释放放在 RLock 内，保证幂等且不会
        在尚有任务写入元数据时让下一实例把它们误当成异常退出的历史任务。
        """
        with self._lock:
            if self._closed and self._state_lock is not None and not any(j["status"] in ACTIVE for j in self._jobs.values()):
                self._state_lock.__exit__(None, None, None)
                self._state_lock = None
