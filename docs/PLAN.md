# Maya MCP 作业场景收尾计划

目标是让 Agent 从一份可变的作业配置出发，通过标准 MCP stdio 驱动真实 Maya，取得可打开的模型、图片和逐项验收记录。沿用现有 FastMCP、Maya 主线程 handlers、独立 mayapy worker 与 JSON 作业模板。主机 Python 为 3.11+，进入 Maya 的代码以 Python 3.7 为兼容下限。

## 起点与证据

开始规划时工作树干净，main 指向 `1eaa82c`，前序提交依次为 `0ea8ada`、`e766e34`。依据为 `docs/REQUIREMENTS.md`、当前源码与测试，以及 `D:\Agents-MCP\data\runs\20261126-161309-05eda2\artifacts\research-research-report.md`。该报告研究的是 `0ea8ada`。WIP 已新增建模、拓扑、UV、烘焙、版本发现、作业验收和异步作业，实施时直接修现有入口。

| 待处理项 | 已有证据 | 收尾判据 |
| --- | --- | --- |
| 6 个单测 | 上一轮报告通过。当前 `tests/test_bridge.py` 有 6 项，覆盖版本发现、分帧、非法 JSON、鉴权、排队超时与路径/代码边界 | 在当前 main 复跑，以退出码及用例数为准。旧研究中的临时目录写入问题不能直接列作当前失败 |
| 扩展后的真实 smoke | 同次 run 的 `events.jsonl` 中，较早的短 smoke 完成建模、导出、渲染和 UV；后来工具列表断言 `REQUIRED <= …` 报 AssertionError。旧输出截断，缺失名称尚无可靠结论 | 从当前 stdio 客户端打印缺失工具差集、实际工具数和服务模块位置，再修复或记录已消失的差异；继续执行到最后一个断言 |
| 截图/渲染 | `.tmp/stage-smoke.log` 有摄影机不可渲染、跳过输出记录；另一次临时目录留有 MB、FBX、OBJ 和 render.log | 记录本次相机、后端、进程退出码，验证本次 PNG 可解码且尺寸正确。残留文件不能证明整轮通过 |
| Arnold | 上一轮日志出现 `Arnold license state invalid; no image was published`；默认 smoke 会跳过可选 Arnold 分支 | 分列“已执行成功”“许可/插件不可用”“未运行”，独立报告四张非 Arnold PNG 的结果 |

### 1. 固定运行入口并复现首个失败

- [ ] 从仓库根目录激活现有 `.venv`，核对主机解释器、`maya_mcp.server.__file__` 与 `tests/maya_smoke.py` 的来源。保留已有工作树，用新临时目录与空闲回环端口运行。
- [ ] 依次执行 `python -m unittest discover -s tests` 和 `python -m tests.maya_smoke`。保留首个错误的工具名、入参、五字段响应、工作进程日志和退出码。若模块入口解析到别处，先修复包解析；若工具清单有差异，对照 `server.register_tools`、四组 `TOOLS` 和实际导入位置修复。保留原 smoke 的必需工具断言。

### 2. 补上 GUI commandPort 与会话发现

- [ ] `server.py` 已提供 stdio；`bridge.start_gui` 和 `worker.py` 使用项目自建 TCP JSON 服务。保留现有 headless 调用链。在 `config.py`、`client.py`、`session.py` 增加 GUI 原生 commandPort 适配，让 Maya 用户只需在 Script Editor 打开指定的回环端口，无需在 Maya 安装 MCP SDK 或其他包。复用现有 handler 注册和策略校验，限定请求为已注册工具与序列化参数。明确原生端口对本机进程的信任边界与关闭方式。
- [ ] 增加 `list_sessions` 与显式选会话入口，支持指定端口及有限的本机候选端口。握手返回稳定的会话标识、端口、版本、模式；遇到多个会话时由调用方选择，连接失败不得把写操作转到另一场景。用 2026 GUI 实际打开端口，从 stdio 创建并查询一个有唯一名称的物体，记录会话标识；双实例路由若未实测则单列。
- [ ] 参考 `D:\Agents-MCP\research\` 中 patrick 的零安装、xxx91n 的受限端口扫描、chadrik 的手动添加会话、gimbal 的 typed tools、dcc 的渐进加载与 3.7 边界。沿用 typed 签名和现有工具名，增加可选的基础工具集和按建模/拓扑/UV/烘焙启用的工具组。默认配置保留完整清单；检查基础模式的发现入口能引导客户端启用所需组。
- [ ] 两条桥接路径都核对 `{ok,data,error,warnings,artifacts}`。沿用 `policy.py` 的根目录、保留目录、后缀及覆盖检查，原始命令入口默认关闭并执行白名单。排队超时取消未执行操作，已开始的变更返回状态不确定提示；长任务复用 `submit_job/job_status`，防止因超时自动重试而重复改场景。

### 3. 按工具路径验收真实 Maya 2026

| 现有入口与实现 | 在扩展 smoke 中核对的结果 |
| --- | --- |
| `modeling.py`、`handlers.py` 的基础体、曲线/放样、挤出、倒角、镜像、阵列、布尔、组合/分离、分组与图层 | 检查对象存在、面数、拆分数与层级。遇到失败先定位命令参数及上一步是否已经改名或消耗操作对象 |
| `topology.py` 的 `mesh_retopo/reduce/rebuild/topology/snap`（实际名称均带 `mesh_`）及 `set_symmetry` | 记录高低模面数、三角/四边/多边面和非流形结果。返回实际算法；只有允许近似时才退到 polyReduce，并报告降级 |
| `uv.py` 和 `images.py` 的 `uv_project/unfold/layout/sets/inspect/snapshot`（均带 `uv_`） | 在指定 UV 集完成投影、展开和排布，检查未映射面、实际 tile 与快照。缺 Unfold3D 时显示实际展开后端 |
| `baking.py` 的 `bake_map` | 原生 Transfer Maps 探测实际命令，为高低模烘焙 normal、color；AO、curvature 走现有 Maya 网格几何投射。逐张对照返回的 engine、路径、artifacts，解码 PNG，检查尺寸及已知夹具的颜色或像素特征。后端缺失、无有效投射或产物损坏时报告失败 |
| `bake_arnold_texture` | 设置 `MAYA_MCP_SMOKE_ARNOLD=1` 纳入同轮验收。分别记录 MtoA、命令和许可；成功需验证本轮 EXR 的尺寸、像素和许可状态。失败应返回原因、空 artifacts 并保留既有合法输出；插件缺失也要打印明确状态 |
| `render_view/capture_viewport/compose_sheet`、`save_scene/export_scene` | 在指定对象上验证 headless 渲染降级、PNG 和合图尺寸；GUI 截图单列。核实 MB、FBX、OBJ 的真实路径、文件格式和内容。渲染失败先查 `images.render_view` 的临时场景、相机和 Render 子进程日志 |

只针对复现的缺陷或缺失验收条件补断言，复用 `tests/maya_smoke.py` 的真实夹具。将临时目录删除前的验证结果、实际引擎、文件尺寸和大小记入运行摘要；需要交付的示例文件输出到模板配置目录。

### 4. 版本发现与降级矩阵

检查 `versions.py`、`config.py` 和 `session.py` 对显式路径、安装根目录、注册表、MAYA_LOCATION 与 PATH 的处理。以可执行文件存在作为发现条件，连接后以 Maya 自报版本确认匹配。用 `capabilities.py` 和 `get_maya_info` 记录 Python、渲染器、原生命令、插件及实跑结果。检查 worker、bridge 及 handler 导入链有无 Python 3.8+ 专用 API 或主机 SDK 依赖。静态兼容性检查单列。

| Maya | 本机依据 | 实施阶段应填写的能力结论 |
| --- | --- | --- |
| 2022 | 未安装 | 未实测；检查 Maya 侧 Python 3.7 导入链 |
| 2023 | 旧研究记录注册表残留，无有效 mayapy | 未实测；确认发现逻辑忽略空目录 |
| 2024 | 未安装 | 未实测 |
| 2025 | 未安装 | 未实测 |
| 2026 | 已安装，候选路径为 `D:\Maya 2026\Maya2026\bin\mayapy.exe` | 按 GUI commandPort、headless、建模、拓扑、UV、四类 PNG、Arnold EXR、截图和导出分别填实测结果 |

### 5. 模板、文档与示例状态

- [ ] 用 `examples/assignment.template.json` 和一个不同题目的内存配置调用 `assignment_plan`，改变身份、风格、视角、烘焙与导出格式，核对生成的阶段、命名、工具和预期路径。通过 `assignment_check` 对照 deliverables、renders、exports、docs，记录 missing、invalid、实际大小和 ready。核验写报告时的 overwrite 行为。
- [ ] 用 `examples/honor_remote/assignment.json` 生成示例清单。当前缺参考图、学号、姓名；中模、低模、三视图合图、高低模对照、UV 图和低模 FBX 待交付。示例关闭烘焙，课程要求的手工拓扑留给人工复核。任何 smoke 夹具均不计入课程交付。
- [ ] 重写 `README.md` 的通用入口，题目细节指向 `examples/honor_remote/`。提供 DSH、Codex TOML、Claude stdio 配置，含主机解释器绝对路径、模块、工作目录、项目根目录、模式与端口；区分 headless 自动启动和 GUI 附加。核对客户端配置语法，以 `initialize → tools/list → get_maya_info` 记录实际联通的客户端。给出分组工具清单、降级语义、两条测试命令、版本矩阵和产物位置。

### 6. 复验与最终提交

- [ ] 在实施改动完成后，用仓库 venv 的主机 Python 执行以下命令，记录各自退出码、用例数、版本和跳过原因。真实 2026 的必测项失败时继续修复；许可不可用的 Arnold 单列。

```powershell
.\.venv\Scripts\Activate.ps1
python -m unittest discover -s tests
$env:MAYA_MCP_SMOKE_ARNOLD = '1'
python -m tests.maya_smoke
```

- [ ] 按实际执行记录更新 README 和示例状态。运行 `git diff --check`、`git status --short`，仅暂存本次计划、实现、测试及相关文档，检查 `git diff --cached --stat`。在保留 `1eaa82c` 的 main 历史上执行 `git commit -m "Complete reusable Maya assignment workflow and verify Maya 2026"`，用 `git log -2 --oneline` 与 `git status --short` 核对提交及剩余改动。
- [ ] 最终报告附提交号、变更与工具清单、测试命令和实际结果、2022–2026 能力矩阵、示例交付状态、未完成项。每阶段按已达到的验收条件给出 VERDICT，计划通过仅表示本文已交付。
