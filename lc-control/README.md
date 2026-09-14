# LC Control · AIDC 液冷智控软件

当前为 **v0.4.0 工程验证版**：支持 Modbus TCP、流量/压差控制、有界参数校准和实际 Sustain-LC FMU 闭环。尚无厂家实机验收；当前 FMU 实验没有取得节能收益。

**完整说明只看 [产品与使用手册](docs/边缘液冷智控产品审查与使用手册.html)**（[Markdown 源文件](docs/边缘液冷智控产品审查与使用手册.md)）。配置、算法、厂家接口、部署、代码职责、验证和待办均集中在那里。[结果入口](index.html) 可直接打开已有报告。

在本目录运行，核心需要 Python 3.9+ 和 Unix 环境：

```bash
python3 -m lc_control simulate examples/thermal_liquid_to_liquid.json --output outputs/my-first-001
python3 tools/wire_demo.py
```

第一条生成合成三策略对比，第二条与本机模拟 CDU 交换真实 TCP 报文。实际 FMU 的 Docker 环境与命令见手册第 9 节。示例均非现场数据；真实设备从 `examples/field_template.UNBOUND.json` 核对绑定。

开发验证：`python3 -m unittest discover -s tests -v`。文档更新后运行 `python3 tools/build_manual.py` 和 `python3 tools/build_portal.py`；只编辑 Markdown，HTML 自动生成。
