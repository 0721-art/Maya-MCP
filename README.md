# Maya-MCP

面向“在对话中完成 Maya 建模作业”的 MCP stdio 服务。它把建模、拓扑、UV、烘焙、截图/渲染、导出和作业验收组织成可被 Agent 调用的工具，而不是把某一道 HONOR 遥控器题目写死在代码里。

HONOR 遥控器只是 `examples/honor_remote/` 中的示例；真正的作业通过 JSON 配置驱动，可以替换题目、参考图、命名和交付项。

## 设计来源

参考了 [mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) 的“MCP Server + DCC 内桥接”思路，并调研了这些 Maya MCP 项目：

- [PatrickPalmer/MayaMCP](https://github.com/PatrickPalmer/MayaMCP)：Maya `commandPort`、Maya 侧动态工具、低层 MCP。
- [Xxx91n/mcp-for-maya](https://github.com/Xxx91n/mcp-for-maya)：`commandPort`、自动发现端口、多 Maya 实例、场景/相机/资产工具。
- [GimbalGoats/GG_MayaMCP](https://github.com/GimbalGoats/GG_MayaMCP)：`commandPort`、71 个类型化工具、Maya 2024+。
- [dcc-mcp/dcc-mcp-maya](https://github.com/dcc-mcp/dcc-mcp-maya)：大量可发现工具、UV/烘焙专题、Maya 2022/Python 3.7 兼容提示。
- [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server)：`commandPort`、多会话资源。

本仓库没有直接复制这些项目。当前实现使用**本地回环 TCP + token 的 JSON 桥接**，同时支持 Maya GUI 和 `mayapy` headless；它与 `commandPort` 的目标相同（在 Maya 与 Agent 之间传命令），但协议独立，详见 [Maya 参考调研](docs/MAYA_REFERENCES.md) 和 [架构](#架构)。

## 特性

- 标准 MCP stdio 服务，可接入 DSH、Codex、Claude 等 MCP 客户端。
- 44 个工具，覆盖：
  - Maya 安装发现、场景/对象检查、对象与 NURBS 建模；
  - 手动/自动低模拓扑、减少面数、重拓扑、吸附、对称；
  - UV 投影、展开、排布、UV 集与 UV 检查；
  - 高模到低模的 native bake（normal/AO/curvature/color）和 Arnold texture bake（需 Arnold 许可）；
  - 渲染、GUI viewport 截图、多图拼版、UV 截图；
  - 保存 `.ma/.mb`、导出 FBX/OBJ；
  - 作业计划、交付验收、长任务轮询。
- 支持 Maya 2022–2026 的**发现与版本选择**；当前机器只在 Maya 2026 上完成实测，其他版本会明确标记为 `UNTESTED`。
- 服务进程不 `import maya`；Maya 相关代码只在 Maya 的 Python 进程里执行，避免宿主进程被 Maya 绑定。
- 路径限制、token 校验、命令白名单、超时与 job 轮询。

## 架构

```text
Agent / MCP client
        │  MCP stdio (JSON-RPC)
        ▼
maya_mcp.server (宿主 Python 3.11+)
        │  token-protected TCP JSON
        ├──────────────► Maya GUI：Script Editor 里启动的 bridge
        └──────────────► mayapy headless：worker 进程
                              │
                              ▼
                         Maya cmds
```

- **GUI 模式**：Maya 处于打开状态，操作会出现在当前场景/Outliner 中，适合人工检查、建模作业和截图。
- **headless 模式**：`MayaSession` 发现 `mayapy` 后启动 `python -m maya_mcp.worker`，适合自动测试、批量生成和 CI。
- 服务默认 `MAYA_MCP_MODE=auto`：先探测已有 bridge；没有则启动 headless worker。要用 GUI，请把模式设为 `gui` 并在 Maya 内启动 bridge。

## 环境要求

- 宿主：Python 3.11+（`.venv` 已使用 3.11）。
- Maya：2026 已实测；2022–2025 仅实现发现/选择逻辑，尚未在本机实测。
- Maya 2026 路径（本机）：
  - `D:\Maya 2026\Maya2026`
  - `D:\Maya 2026\Maya2026\bin\mayapy.exe`
- Python 依赖：`mcp==1.30.0`、`Pillow>=10,<13`（见 `pyproject.toml`）。

## 安装

在仓库根目录执行：

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

如果使用已有的 `.venv`，只需确认依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## 快速开始

### 1. 只启动 MCP 服务

```powershell
cd D:\Maya-MCP
.\.venv\Scripts\python.exe -m maya_mcp.server
```

stdin/stdout 使用 MCP 协议；不要在同一条 stdout 上打印调试信息。

### 2. headless（自动启动 Maya）

```powershell
$env:MAYA_MCP_PROJECT_ROOT = "D:\Maya-MCP"
$env:MAYA_MCP_MAYAPY = "D:\Maya 2026\Maya2026\bin\mayapy.exe"
$env:MAYA_MCP_MODE = "auto"
.\.venv\Scripts\python.exe -m maya_mcp.server
```

首次工具调用会启动 `mayapy` worker，等待 Maya standalone 就绪。

### 3. GUI 模式（推荐做作业时使用）

先设置和 MCP 服务一致的 token，再在 Maya 的 Script Editor 中运行：

```python
import os
import sys

os.environ["MAYA_MCP_TOKEN"] = "replace-with-a-long-random-token"
sys.path.insert(0, r"D:\Maya-MCP")

from maya_mcp.bridge import start_gui

start_gui()
```

MCP 服务侧：

```powershell
$env:MAYA_MCP_MODE = "gui"
$env:MAYA_MCP_TOKEN = "replace-with-a-long-random-token"
$env:MAYA_MCP_PORT = "9876"
.\.venv\Scripts\python.exe -m maya_mcp.server
```

GUI bridge 默认监听 `127.0.0.1:9876`。如果端口被占用，修改 `MAYA_MCP_PORT` 并保持服务端一致。

## MCP 客户端配置

通用 stdio 配置示例（把路径替换为你的实际路径）：

```json
{
  "mcpServers": {
    "maya-mcp": {
      "command": "D:\\Maya-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "maya_mcp.server"],
      "cwd": "D:\\Maya-MCP",
      "env": {
        "MAYA_MCP_PROJECT_ROOT": "D:\\Maya-MCP",
        "MAYA_MCP_MAYAPY": "D:\\Maya 2026\\Maya2026\\bin\\mayapy.exe",
        "MAYA_MCP_MODE": "auto"
      }
    }
  }
}
```

接入 Agents-MCP 时，server 名称建议用 `maya-mcp`，工具会以客户端约定的前缀暴露；本仓库不依赖特定客户端。

## 配置项

| 变量 | 默认 | 说明 |
|---|---|---|
| `MAYA_MCP_PROJECT_ROOT` | 当前目录 | 项目根；所有输出路径都限制在它下面 |
| `MAYA_MCP_MAYAPY` | 自动发现 | 指定 `mayapy.exe`，优先于版本扫描 |
| `MAYA_MCP_VERSION` | 自动 | `2022`–`2026`；指定后只接受该版本 |
| `MAYA_MCP_INSTALL_ROOTS` | 系统默认 | 额外的 Maya 安装根目录，多个用 `;` 分隔 |
| `MAYA_MCP_MODE` | `auto` | `auto` / `gui` / `headless` |
| `MAYA_MCP_PORT` | `9876` | bridge 端口；只允许回环地址 |
| `MAYA_MCP_TOKEN` | 空 | GUI 模式必须显式设置；headless 会自动生成 |
| `MAYA_MCP_TIMEOUT` | `60` | 普通命令超时（秒） |
| `MAYA_MCP_RENDER_TIMEOUT` | `600` | 渲染/烘焙等长任务超时（秒） |
| `MAYA_MCP_ALLOW_CODE` | `0` | 是否允许受限的 `execute_maya_code` |
| `MAYA_MCP_LOG` | `.tmp/maya-worker.log` | Maya worker 日志路径 |

## 工具目录（44 个）

### 会话、场景与低层查询

`get_maya_info`、`list_maya_installations`、`get_scene_info`、`get_object_info`、`execute_maya_code`

### 建模与组织

`create_object`、`modify_object`、`delete_object`、`create_curve`、`loft_mesh`、`mesh_extrude`、`mesh_bevel`、`mesh_mirror`、`array_objects`、`mesh_boolean`、`mesh_combine`、`mesh_separate`、`group_objects`

### 材质、渲染与导出

`set_material`、`create_layer`、`assign_layer`、`render_view`、`capture_viewport`、`compose_sheet`、`uv_snapshot`、`save_scene`、`export_scene`

### 拓扑与低模

`mesh_topology`、`mesh_reduce`、`mesh_retopo`、`mesh_rebuild`、`mesh_snap`、`set_symmetry`

### UV

`uv_project`、`uv_unfold`、`uv_layout`、`uv_sets`、`uv_inspect`

### 烘焙

`bake_map`（native，输出 normal/AO/curvature/color PNG）、`bake_arnold_texture`（Requires Arnold/MtoA 和有效许可）

### 作业与长任务

`assignment_plan`、`assignment_check`、`submit_job`、`job_status`

## 作业工作流

### 1. 创建配置

从 [examples/assignment.template.json](examples/assignment.template.json) 复制一份，填写：

- `title`、`work_name`：题目/作品名；
- `student_id`、`student_name`：没有就保留 `null`，工具会生成 `MISSING-...` 占位符并提醒；
- `reference_images`：参考图路径（相对项目根）；
- `requirements`：文字要求；
- `scene_format`、`export_formats`、`bake_maps`、`views`、`comparison_views`；
- `output_dir` 和 `naming_pattern`。

HONOR 示例见 [examples/honor_remote/assignment.json](examples/honor_remote/assignment.json)，但不要把示例当成唯一题目。

### 2. 生成计划

用 MCP 客户端调用：

```json
{
  "name": "assignment_plan",
  "arguments": {
    "config_path": "examples/assignment.template.json"
  }
}
```

返回阶段、禁用项、命名结果、缺失输入、预期文件路径和警告。它不会伪造交付物；`ready` 仍取决于真实文件和输入。

### 3. 建模、拓扑、UV、烘焙

按计划调用工具。典型流程：

1. `create_object` / `loft_mesh` / `mesh_bevel` / `mesh_mirror` 做中模；
2. `create_layer` / `assign_layer` / `group_objects` 整理 Outliner 和图层；
3. `mesh_retopo` / `mesh_reduce` / `mesh_snap` / `mesh_topology` 做低模与检查；
4. `uv_project` / `uv_unfold` / `uv_layout` / `uv_inspect` 做 UV；
5. `bake_map` 或 `bake_arnold_texture` 烘焙；
6. `render_view` / `capture_viewport` / `compose_sheet` 出三视图或对比图；
7. `save_scene` / `export_scene` 保存和导出。

长任务可以用 `submit_job` 提交，用 `job_status` 轮询。

### 4. 验收

```json
{
  "name": "assignment_check",
  "arguments": {
    "config_path": "examples/honor_remote/assignment.json",
    "low_meshes": ["低模对象名"],
    "write_report": true
  }
}
```

`assignment_check` 检查：

- `.mb/.ma`、FBX/OBJ 的签名/内容；
- PNG 是否可读；
- 低模面数、是否全 quad（配置要求时）；
- 非流形边/顶点、UV 未映射面/UV 集；
- 可选地把结果写入 `docs/acceptance.json`。

最终视觉要求（例如参考图相似度）仍需人工确认，工具会在 `manual_review` 中提示。

## 测试

```powershell
cd D:\Maya-MCP
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests\maya_smoke.py
```

当前实测结果（Maya 2026）：

- `6 tests OK`
- smoke：`PASS: MCP stdio -> Maya 2026: modeling, topology, UV, four PNG bake maps, render, export, assignments, jobs`
- Maya 2022–2025：`UNTESTED`（本机未安装可用的 mayapy）
- Arnold：本机 Arnold 许可不可用；`bake_arnold_texture` 会明确报错，native `bake_map` 可正常产出四张 PNG

## 版本适配

`maya_mcp.versions` 扫描 Maya 2022–2026 的注册表和常见安装目录，`list_maya_installations` 会返回每个版本的 `installed`、`mayapy` 和 `smoke_status`。

- **Maya 2026**：已完成真实 smoke（建模、拓扑、UV、四张 bake PNG、渲染、导出、作业、job）。
- **Maya 2022–2025**：已实现发现、`MAYA_MCP_MAYAPY`/`MAYA_MCP_VERSION` 选择、缺少工具时的 `UNTESTED`/能力降级提示；尚未在真实安装上验收。Maya 2022 的 Python 3.7 兼容性尤其需要在目标机器上补 smoke。
- 不要在未实测版本上宣称“完全支持”；先跑 `tests/maya_smoke.py`。

## 已知限制

1. **GUI bridge 需要手动启动**：当前不是 `commandPort` 的零配置模式；需要在 Maya Script Editor 执行 `start_gui()`，并设置相同 token。`commandPort` 适配是后续工作（见 [参考调研](docs/MAYA_REFERENCES.md)）。
2. **只有 Maya 2026 实测**；2022–2025 仍需目标机验证。
3. **Arnold**：`bake_arnold_texture` 依赖 Arnold/MtoA 和有效许可；无许可时使用 native `bake_map`，并接受其质量/功能差异。
4. **headless 截图**：`capture_viewport` 在 standalone 下回退为 Maya Software render，结果标记为 `fallback`；GUI 模式才是真实 viewport。
5. **作业示例没有真实资产**：HONOR 示例中的参考图、学号、姓名和最终模型需要用户提供；`assignment_check` 会保持 `ready:false`，不会假装完成。
6. **`execute_maya_code`** 默认关闭，且只允许白名单 `cmds` 调用；不要用它绕过作业验收。

## 目录结构

```text
maya_mcp/
  assignment.py      作业配置、计划、验收
  baking.py          native/Arnold 烘焙
  bridge.py          Maya 内 TCP JSON bridge
  capabilities.py    Maya 版本/插件能力探测
  client.py          bridge 客户端
  composition.py     多图拼版
  config.py          环境变量和 Settings
  envelope.py        统一返回结构
  handlers.py        Maya cmds 处理器
  images.py          渲染/截图
  jobs.py            长任务轮询
  modeling.py        建模/NURBS/组织
  policy.py          路径/代码/发布约束
  protocol.py        帧协议
  server.py          MCP stdio 服务与工具注册
  session.py         GUI/headless 会话
  topology.py        拓扑工具
  uv.py              UV 工具
  versions.py        Maya 2022–2026 发现
  worker.py          mayapy worker
tests/
examples/
docs/
```

## 相关文档

- [作业配置模板](examples/assignment.template.json)
- [HONOR 示例](examples/honor_remote/README.md)
- [通用需求说明](docs/REQUIREMENTS.md)
- [Maya MCP 参考调研](docs/MAYA_REFERENCES.md)
- [Git 提交计划](docs/PLAN.md)

## 状态

核心功能已完成到可运行、可 smoke 的阶段：44 个工具、Maya 2026 真实执行、6 个单元测试通过、完整 smoke 通过。下一步是把 Maya 2022–2025 的目标机 smoke 补齐，并评估 `commandPort` 适配；在此之前，README 不把“多版本发现”写成“多版本已实测”。
