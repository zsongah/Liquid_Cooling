# LC Control · AIDC 液冷智控软件

当前为 **v0.6.0 边缘工作台工程验证版**：提供机房拓扑与多独立域状态、功率预测接入及真实前馈、决策回放、配置草稿/发布、后台合成仿真及 Modbus 会话管理。沿用流量/压差控制、有界参数校准和实际 Sustain-LC FMU 实验链路。尚无厂家实机验收；物理连接图不等于管网求解器，尚无全站节能收益证明。

在本目录启动工作台（Python 3.9+，Unix，无第三方运行依赖）：

```bash
python3 -m lc_control serve
```

浏览器打开 **[本机工作台](http://127.0.0.1:8765)**。选择 `workbench_physical` 模板，复制为新草稿，检查配置并保存发布，再到运行页启动合成仿真。界面使用真实的仿真记录；关闭页面不停止后台任务。草稿、版本及新运行保存在 `outputs/workbench/`。配置和数据来源在页面中分别标识；旧 FMU 记录可回放，新增 FMU 实验仍使用专用命令。

“功率预测”页可查看输入契约、校验并提交显式 synthetic 试验数据；没有数据时保持未接入。后台按节点/机柜归属和液冷分担比例聚合到 CDU，实际算法使用记录与预览分别显示。详见手册第 8.4 节，原计划实现边界见第 13.1 节。

默认禁止网页发起现场闭环写入；监测/影子运行及显式启用现场控制的条件见手册第 8.3 节。工作台仅监听本机回环地址，不是已部署的远程多用户平台。

验证报告（FMU 统一验证 29 组实验/11 组图/66 面板、向上级汇报版 HTML 等）是**本地实验产物**，保存在 `outputs/`，**不随仓库发布**，因此仓库内不会出现指向它们的死链。需查看时先运行对应实验生成，再从 [结果入口](index.html) 打开：入口只对本地实际存在的产物生成链接，缺失项标注“未随仓库发布”。本地生成命令见手册第 9–10 节。

**完整说明只看 [产品与使用手册](docs/边缘液冷智控产品审查与使用手册.html)**（[Markdown 源文件](docs/边缘液冷智控产品审查与使用手册.md)）。配置、算法、厂家接口、部署、代码职责、验证和待办均集中在那里。

在本目录运行，核心需要 Python 3.9+ 和 Unix 环境：

```bash
python3 -m lc_control simulate examples/thermal_liquid_to_liquid.json --output outputs/my-first-001
python3 tools/wire_demo.py
```

第一条生成合成三策略对比，第二条与本机模拟 CDU 交换真实 TCP 报文。实际 FMU 的 Docker 环境与命令见手册第 9 节。示例均非现场数据；真实设备从 `examples/field_template.UNBOUND.json` 核对绑定。

开发验证：`python3 -m unittest discover -s tests -v`。文档更新后运行 `python3 tools/build_manual.py` 和 `python3 tools/build_portal.py`；只编辑 Markdown，HTML 自动生成。

前端交互回归使用 Node 内置测试器：`node --test tests/frontend-workbench.test.cjs`（Node 18+；控制服务本身不依赖 Node）。架构图更新运行 `python3 tools/build_architecture.py`，生成对应当前版本的 PNG、SVG 和查看页。

场景示例在 `examples/`；FMU 实验组合在 `experiments/fmu_validation_matrix.json`，由 `tools/run_fmu_validation_matrix.py --matrix` 读取，不传给场景 `validate`。`field_template.UNBOUND.json` 是预期校验失败的现场占位模板。
