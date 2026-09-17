"""命令行入口：validate / analyze / migrate / demo / simulate / run / emulator / report / serve。

解析参数后先校验配置，再选择运行流程；不会自动探测或抢占设备控制权。
run 故障返回非零状态，便于外部进程管理器识别失败；SIGTERM 进入退出流程。"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .adapters import MockCDUAdapter
from .configuration import capability_report, load_scene
from .contracts import ControlRequest
from .runtime import ControlService


def main():
    """解析命令行参数、校验配置、调用对应流程，并输出结果。默认设备模式为 monitor。"""
    parser = argparse.ArgumentParser(description="Configurable CDU control runtime")
    parser.add_argument("action", choices=("validate", "analyze", "migrate", "demo", "simulate", "run", "emulator", "report", "serve"))
    parser.add_argument("scene", nargs="?")
    parser.add_argument("--audit", default=None)
    parser.add_argument("--output", help="analyze/migrate: JSON 输出文件（省略时仅 stdout）；其他运行命令: 输出目录，默认 outputs/latest")
    parser.add_argument("--mode", choices=("monitor", "shadow", "control"), default="monitor")
    parser.add_argument("--steps", type=int, default=12, help="0 means continuous")
    parser.add_argument("--seconds", type=float, default=1800)
    parser.add_argument("--interval", type=float, default=5)
    parser.add_argument("--domain")
    parser.add_argument("--forecast-file")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"), help="工作台仅绑定本机回环地址")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--project-root", help="工作台配置模板及已保存运行所在的项目目录")
    parser.add_argument("--state-dir", help="工作台草稿、版本与后台运行保存目录")
    parser.add_argument("--allow-hardware-control", action="store_true", help="允许工作台申请现场闭环；仍必须通过原有设备验收和安全网关")
    args = parser.parse_args()
    import math
    if not math.isfinite(args.interval) or args.interval <= 0 or args.steps < 0 or not math.isfinite(args.seconds) or args.seconds <= 0:
        parser.error("interval/seconds must be positive and finite; steps >= 0")
    if args.action == "serve":
        from .console_server import serve
        root = Path(args.project_root).resolve() if args.project_root else Path(__file__).resolve().parents[1]
        serve(root, host=args.host, port=args.port, state_dir=args.state_dir,
              allow_hardware_control=args.allow_hardware_control)
        return
    if args.action == "report":
        from .report import render_report
        print(render_report(args.output or "outputs/latest").resolve())
        return
    if not args.scene:
        parser.error("scene is required")
    scene = load_scene(args.scene)
    if args.action == "migrate":
        if scene.get("schema_version") != "0.1":
            parser.error("migration_requires_legacy_schema: migrate requires schema_version 0.1")
        from .analysis_config import migrate_legacy_scene
        migrated = migrate_legacy_scene(scene)
        # 文件仅包含新场景，可直接传给 validate/analyze；迁移缺口作为独立
        # stdout 报告输出。缺失管径、阻力和压力边界不会用示例默认值补齐。
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(json.dumps(migrated["scene"], ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            print(json.dumps(migrated["migration"], ensure_ascii=False, indent=2, allow_nan=False))
        else:
            print(json.dumps(migrated, ensure_ascii=False, indent=2, allow_nan=False))
        return
    if args.action == "analyze":
        if scene.get("schema_version") != "0.2":
            parser.error("analysis_schema_required: analyze requires schema_version 0.2")
        from .hydraulics import analyze_scene
        result = analyze_scene(scene, domain_id=args.domain)
        rendered = json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            destination = Path(args.output)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(rendered + "\n", encoding="utf-8")
        print(rendered)
        return
    if args.action == "validate" and scene.get("schema_version") == "0.2":
        from .site import inspect_scene
        print(json.dumps(inspect_scene(scene), ensure_ascii=False, indent=2, allow_nan=False))
        return
    if args.action != "validate":
        from .operations import require_runtime_scene
        try:
            require_runtime_scene(scene)
        except ValueError as error:
            parser.error(str(error) + ": use analyze for schema_version 0.2")
    output_directory = args.output or "outputs/latest"
    if args.action in ("simulate", "run", "emulator"):
        from .operations import compare, run
        if args.action == "simulate":
            result = compare(scene, output_directory, args.seconds, args.interval)
        elif args.action == "run":
            result = run(scene, output_directory, args.mode, args.steps, args.interval, args.domain, args.forecast_file)
        else:
            import time
            from .emulator import Emulator
            if scene["scene_id"] != "LOOPBACK-EMULATOR-ONLY":
                raise ValueError("emulator_requires_synthetic_fixture")
            emulator = Emulator(scene).start()
            print("Synthetic Modbus TCP CDU: 127.0.0.1:%s; no hardware attached" % emulator.port, flush=True)
            try:
                while True:
                    time.sleep(0.5)
            except KeyboardInterrupt:
                pass
            finally:
                emulator.close()
            return
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        if args.action == "run" and result.get("exit_code"):
            raise SystemExit(result["exit_code"])
        return
    output = {"scene_id": scene["scene_id"], "capabilities": capability_report(scene)}
    if args.action == "demo":
        if any(p["adapter"] != "mock" for p in scene["devices"].values()):
            raise ValueError("demo_requires_mock_profile")
        domain = scene["control_domains"][0]
        profile = scene["devices"][domain["cdu_id"]]
        adapter = MockCDUAdapter(domain["cdu_id"], profile, domain["owner"])
        service = ControlService(scene, domain["id"], adapter, args.audit)
        service.set_mode("control")
        request = ControlRequest(
            request_id="demo-temperature", asset_id=domain["cdu_id"],
            topology_version=scene["topology_version"], owner=domain["owner"],
            quantity="cdu.sec_supply_temp_sp", value=29.0, unit="degC",
            issued_at=100.0, expires_at=120.0)
        accepted = service.submit(request, now=100.0)
        rejected = service.submit(ControlRequest(
            request_id="demo-invalid", asset_id=domain["cdu_id"],
            topology_version=scene["topology_version"], owner=domain["owner"],
            quantity="cdu.sec_supply_temp_sp", value=999.0, unit="degC",
            issued_at=110.0, expires_at=130.0), now=110.0)
        adapter.stale_by_s = 60.0
        fallback = service.heartbeat(now=115.0)
        output["demo"] = {
            "accepted": asdict(accepted), "rejected": asdict(rejected),
            "stale_data_response": fallback,
            "write_count": len(adapter.writes),
            "limitations": ["protocol_mock_not_thermal_simulation", "no_hardware_write",
                            "no_energy_savings_measurement", "no_rack_safety_guarantee"],
        }
    print(json.dumps(output, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    import signal
    def terminate(signum, frame):
        raise KeyboardInterrupt()
    signal.signal(signal.SIGTERM, terminate)
    try:
        main()
    except KeyboardInterrupt:
        print("Runtime stopped; inspect command journal for handoff confirmation.")
