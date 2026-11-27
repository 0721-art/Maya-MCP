# Maya MCP

一个通用的 Autodesk Maya MCP stdio 服务：让 Agent 在真实 Maya 会话或 `mayapy` headless 进程中完成建模、文字/曲线挤出、拓扑、UV、烘焙、渲染、截图、快照回滚与资产导入导出。

## 实现

当前实现支持两种传输：默认的**本地回环 TCP + token 的 JSON 桥接**，以及 Maya 原生 `commandPort`（`MAYA_MCP_MODE=commandport`）；同时支持 Maya GUI 和 `mayapy` headless。commandPort 模式不需要在 Maya 侧安装 Python 包。

## 特性

- 标准 MCP stdio 服务，可接入 DSH、Codex、Claude 等 MCP 客户端。
- 48 个工具，覆盖：
  - Maya 安装发现、场景/对象检查（支持 `brief=True` 精简返回）；
  - 9 种多边形原语（`cube` / `sphere` / `cylinder` / `plane` / `torus` / `rounded_box` / `capsule` / `cone` / `pipe`）；
  - 3D 文字生成（`create_text`，基于 `textCurves` + `bevelPlus`/`planarSrf`，自动保留字孔）、曲线挤出（`extrude_curve`）、放样、布尔（可选 `delete_inputs=True`）等建模工具；
  - 批量材质分配与同名材质复用（`set_material` 支持 `names=[...]` 与 `reuse=True`）；
  - 拓扑检查、重拓扑、减面、重建、吸附、对称；
  - UV 投影、展开、排布、UV 集与 UV 检查；
  - 高模到低模的 native bake（normal/AO/curvature/color）和 Arnold texture bake（需有效 Arnold 许可）；
  - 精确非方形比例取景渲染（`render_view`，支持可选 `renderer="arnold"` 与显式 `fallback_to_software=True`）、GUI viewport 截图（headless 下显式标注 fallback 元数据，或通过 `allow_fallback=False` 拒绝退化）、多图拼版、UV 截图；
  - 保存 `.ma/.mb`、导出 FBX/OBJ（自动加载 `fbxmaya`/`objExport`）、导入外部 `.obj/.fbx/.ma/.mb` 资产（`import_asset`）；
  - 场景检查点快照与回滚（`scene_checkpoint`）及 Maya 原生撤销/重做（`undo_scene`）；
  - 两条分级的脚本执行通道：`execute_maya_code`（AST 字面量白名单，由 `MAYA_MCP_ALLOW_CODE=1` 开启）与 `execute_script`（完整 Maya Python，支持 import/循环/变量/多语句，带长度与超时限制，由 `MAYA_MCP_ALLOW_SCRIPT=1` 开启）；
  - 长任务提交与轮询（`submit_job` / `job_status`）。
- 支持 Maya 2022–2026 的**发现与版本选择**。
- 服务进程不 `import maya`；Maya 相关代码只在 Maya 的 Python 进程里执行。
- 支持配置项目根目录（环境变量或 CLI 参数）、多允许根目录（`MAYA_MCP_ALLOWED_ROOTS`）、显式外部路径开关（`MAYA_MCP_ALLOW_EXTERNAL_PATHS=1` + 每次调用 `allow_outside_root=True`）以及中文/非 ASCII 路径。

## 架构

```text
Agent / MCP client
        │  MCP stdio (JSON-RPC)
        ▼
maya_mcp.server (宿主 Python 3.11+)
        │  token-protected TCP JSON / commandPort
        ├──────────────► Maya GUI：Script Editor 里启动的 bridge 或原生 commandPort
        └──────────────► mayapy headless：worker 进程
                              │
                              ▼
                         Maya cmds
```

- **GUI 模式**：Maya 处于打开状态，操作出现在当前场景/Outliner 中，适合人工检查、建模和真实 Viewport 2.0 截图。
- **headless 模式**：`MayaSession` 发现 `mayapy` 后启动 `python -m maya_mcp.worker`，启动时启用撤销队列并按需预加载 `fbxmaya`/`objExport`，适合自动测试和批量生成。
- **commandPort 模式**：连接 Maya 自带的 `cmds.commandPort` socket，不在 Maya 侧安装包，也不会启动 worker。
- 默认 `MAYA_MCP_MODE=auto`：先探测已有 bridge；没有则启动 headless worker。GUI 需设置 `MAYA_MCP_MODE=gui` 并在 Maya 内启动 bridge；原生 commandPort 使用 `commandport`。

## 环境要求

- 宿主：Python 3.11+。
- Maya：支持 Maya 2022–2026。
- Maya 2026 路径（本机示例）：
  - `D:\Maya 2026\Maya2026`
  - `D:\Maya 2026\Maya2026\bin\mayapy.exe`
- Python 依赖：`mcp==1.30.0`、`Pillow>=10,<13`（见 `pyproject.toml`）。

## 安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

如果使用 `uv` 管理虚拟环境：

```powershell
uv venv .venv --python 3.11
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
```

已有 `.venv` 时重新安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
# 或
uv pip install --python .\.venv\Scripts\python.exe -e ".[dev]"
```

## 快速开始

### 1. 启动 MCP 服务（支持环境变量或 CLI 参数）

```powershell
$env:MAYA_MCP_MODE = "headless"
$env:MAYA_MCP_PROJECT_ROOT = "D:\MayaProjects"
python -m maya_mcp.server
```

也可直接通过命令行参数配置根目录、附加目录与脚本开关（适合直接在客户端 `args` 中配置）：

```powershell
python -m maya_mcp.server --mode headless --project-root "D:\MayaProjects" --allowed-root "D:\Assets" --allow-external-paths --allow-code --allow-script
```

MCP 客户端通过 stdio 与该进程通信。不要把日志写到 stdout；stdout 只用于 MCP JSON-RPC。

### 2. GUI 模式

1. 打开 Maya，在 Script Editor（Python 标签页）中设置路径、非空 `MAYA_MCP_TOKEN` 与项目根目录，并启动 bridge：

```python
import os, sys
if r"D:\Maya-MCP" not in sys.path:
    sys.path.append(r"D:\Maya-MCP")
os.environ["MAYA_MCP_TOKEN"] = "your-token"
os.environ["MAYA_MCP_PROJECT_ROOT"] = r"D:\MayaProjects"
os.environ["MAYA_MCP_PORT"] = "9876"

from maya_mcp.bridge import start_gui
start_gui()
```

2. 在宿主侧设置 `MAYA_MCP_MODE=gui` 和一致的 `MAYA_MCP_HOST`/`MAYA_MCP_PORT`/`MAYA_MCP_TOKEN`/`MAYA_MCP_PROJECT_ROOT`。
3. 启动 `python -m maya_mcp.server`，再让 MCP 客户端连接。

### 3. commandPort 模式

在 Maya Script Editor（Python 标签页）中设置与宿主一致的 `MAYA_MCP_TOKEN`，**必须设置** `MAYA_MCP_PROJECT_ROOT`（除非请求的项目根目录与 Maya 当前工作目录完全一致），并打开一个启用 `echoOutput=True` 的 Python `commandPort`：

```python
import os
import maya.cmds as cmds

os.environ["MAYA_MCP_TOKEN"] = "your-token"
os.environ["MAYA_MCP_PROJECT_ROOT"] = r"D:\MayaProjects"
cmds.commandPort(name="127.0.0.1:7001", sourceType="python", echoOutput=True, bufferSize=65536)
```

然后在宿主侧启动 MCP 服务：

```powershell
$env:MAYA_MCP_MODE = "commandport"
$env:MAYA_MCP_COMMAND_PORT = "7001"
$env:MAYA_MCP_COMMAND_BUFFER_SIZE = "65536" # 可选，默认 65536，应与 Maya commandPort 的 bufferSize 一致
$env:MAYA_MCP_TOKEN = "your-token"
$env:MAYA_MCP_PROJECT_ROOT = "D:\MayaProjects"
.\.venv\Scripts\python.exe -m maya_mcp.server
```

commandPort 模式不启动 worker，也不需要在 Maya 侧 `pip install` 任何包；宿主每次发送一条单行 Python 表达式，由 Maya 自动将本仓库目录加入 `sys.path` 并调用 `maya_mcp.commandport.dispatch`。认证采用 fail-closed：Maya 侧未设置非空 `MAYA_MCP_TOKEN`（或兼容变量 `MAYA_MCP_COMMAND_TOKEN`）时请求会被拒绝。策略开关（`MAYA_MCP_ALLOW_CODE`、`MAYA_MCP_ALLOW_SCRIPT`、`MAYA_MCP_ALLOWED_ROOTS`、`MAYA_MCP_ALLOW_EXTERNAL_PATHS`）同样在 Maya 侧环境中生效。

> **安全提示**：Maya 原生 `commandPort` 在进入 `dispatch()` 之前即会直接执行收到的任意 Python 代码，因此 `MAYA_MCP_TOKEN` 仅提供应用层防误连与会话匹配，不能阻止本机其他进程直接向 `127.0.0.1:7001` 执行任意代码。需要更强边界隔离时，请使用默认的本地 TCP JSON bridge 或 `mayapy` headless 模式。

## 路径与安全策略

- **默认沙箱**：所有读写路径默认限制在 `MAYA_MCP_PROJECT_ROOT`（未设置时为服务进程 `cwd`）内，且拒绝 `.git`、`.agents`、`.codex`、`.aws` 等元数据目录、符号链接穿透与 NTFS 数据流。
- **配置项目根目录**：推荐在客户端配置中设置 `MAYA_MCP_PROJECT_ROOT` 或传入 `--project-root`，避免继承宿主客户端的工作目录；调用 `get_maya_info` 可直接查看当前生效的 `project_root`、`allowed_roots` 与策略开关。
- **附加允许目录**：通过 `MAYA_MCP_ALLOWED_ROOTS`（Windows 用 `;` 分隔，POSIX 用 `:` 分隔）或 `--allowed-root` 指定多个可直接读写的目录（支持中文/Unicode 路径）。
- **显式写出根目录外**：仅当服务端配置了 `MAYA_MCP_ALLOW_EXTERNAL_PATHS=1`（或 `--allow-external-paths`）**且**工具调用显式传入 `allow_outside_root=True` 并使用绝对路径时，才允许读写根目录以外的路径（仍会拦截元数据目录与符号链接，并遵守后缀白名单与 `overwrite=False` 默认值）。
- **中文 / 非 ASCII 路径**：所有保存、导入、导出、渲染、截图与烘焙路径均通过 ASCII 安全暂存目录与原子发布处理，避免 Maya 底层 C++ / CLI 工具在 Windows 中文路径下失败。

## 代码与脚本执行策略

| 工具 | 开关（默认关闭） | 能力与限制 |
|---|---|---|
| `execute_maya_code` | `MAYA_MCP_ALLOW_CODE=1` / `--allow-code` | 仅做 AST 字面量解析，**永不 `eval`/`exec`**；允许调用白名单内的建模/曲线/文字/变换/查询/材质/层/插件 `cmds.*` 函数（不含 `quit`、`file`、`sysFile` 等进程/宿主命令），单次上限 16,384 字符。 |
| `execute_script` | `MAYA_MCP_ALLOW_SCRIPT=1` / `--allow-script` | 执行完整 Maya Python（支持 `import`、循环、变量、函数、多语句；预注入 `cmds`、`mel`、`om`、`math`、`Path`、`PROJECT_ROOT`），将变量 `result` 与捕获的 `stdout`/`stderr` 返回；上限 65,536 字符、支持行级超时中断（默认 ≤60s，最高 300s），并自动包裹为单个撤销块。 |

> `execute_script` 拥有与当前 Maya 进程相同的权限，默认关闭，仅应在受信任的本地工作流中显式开启。

## MCP 客户端配置

以 DSH/Codex 一类 stdio MCP 客户端为例，配置结构通常如下（按客户端实际字段调整）：

```json
{
  "mcpServers": {
    "maya": {
      "command": "D:\\Maya-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "maya_mcp.server", "--project-root", "D:\\MayaProjects"],
      "cwd": "D:\\Maya-MCP",
      "env": {
        "MAYA_MCP_MODE": "headless",
        "MAYA_MCP_ALLOW_CODE": "1",
        "MAYA_MCP_ALLOW_SCRIPT": "1",
        "MAYA_MCP_ALLOW_EXTERNAL_PATHS": "1"
      }
    }
  }
}
```

首次连接后建议依次调用：

1. `get_maya_info`（确认 Maya 版本、模式、`project_root` 与开关状态）
2. `list_maya_installations`
3. `get_scene_info(brief=True)`
4. 一个小的 `create_object` 测试

## 工具组（48 个工具）

| 组 | 工具 |
|---|---|
| 场景/对象 | `get_maya_info`, `list_maya_installations`, `get_scene_info`, `get_object_info`, `create_object`, `modify_object`, `delete_object` |
| 材质/层 | `set_material`, `create_layer`, `assign_layer` |
| 建模/文字/曲线 | `create_curve`, `create_text`, `extrude_curve`, `loft_mesh`, `mesh_extrude`, `mesh_bevel`, `mesh_mirror`, `array_objects`, `mesh_boolean`, `mesh_combine`, `mesh_separate`, `group_objects` |
| 拓扑 | `mesh_topology`, `mesh_retopo`, `mesh_reduce`, `mesh_rebuild`, `mesh_snap`, `set_symmetry` |
| UV | `uv_project`, `uv_unfold`, `uv_layout`, `uv_sets`, `uv_inspect`, `uv_snapshot` |
| 烘焙 | `bake_map`, `bake_arnold_texture` |
| 渲染/截图 | `render_view`, `capture_viewport`, `compose_sheet` |
| 文件导入导出 | `save_scene`, `export_scene`, `import_asset` |
| 快照/撤销 | `scene_checkpoint`, `undo_scene` |
| 异步 | `submit_job`, `job_status` |
| 代码/脚本 | `execute_maya_code`, `execute_script` |

## 典型工作流

### 建模与文字/图标

1. `get_scene_info(brief=True)` 查看场景。
2. `create_object`（支持 `cube`/`sphere`/`cylinder`/`plane`/`torus`/`rounded_box`/`capsule`/`cone`/`pipe`）、`create_text`、`create_curve` + `extrude_curve` / `loft_mesh` 建立造型。
3. 复杂操作前调用 `scene_checkpoint(action="create", name="before_bool")`，失败时 `scene_checkpoint(action="restore")` 回滚。
4. `modify_object`、`mesh_extrude`、`mesh_bevel`、`mesh_boolean(..., delete_inputs=True)` 细化。
5. `set_material(material="Clay", names=[...], color=[...])` 批量赋材质并自动复用同名着色器。
6. `render_view` 或 `capture_viewport` 检查正交/透视轮廓；`save_scene` 保存 `.ma/.mb`。

### 拓扑、UV、烘焙与交付

1. `mesh_topology` 检查面数、非流形边、四边形比例。
2. `mesh_retopo` / `mesh_reduce` / `mesh_rebuild` 生成低模候选，`mesh_snap` 吸附表面。
3. `uv_project` → `uv_unfold` → `uv_layout` → `uv_inspect` → `uv_snapshot`。
4. `bake_map` 生成 normal/AO/curvature/color PNG；有 Arnold 许可时可用 `bake_arnold_texture`。
5. `import_asset` 导入外部 `.obj/.fbx/.ma/.mb`，`export_scene` 导出 FBX/OBJ，`compose_sheet` 拼图交付。

## 测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe tests/maya_smoke.py --year 2026
```

- 单元测试覆盖 bridge、commandPort、路径/脚本策略、非方形取景数学、Arnold 许可错误处理、快照/撤销、批量材质与 48 个工具 schema。
- Maya 2026 smoke 覆盖 MCP stdio → 建模原语、3D 文字与曲线挤出、布尔删原件、批量材质复用、快照回滚与撤销、精简返回、非方形渲染取景、中文路径读写、导入导出、拓扑、UV、bake、render、脚本通道与异步 job。

## 已知限制与环境说明

- **Arnold 许可（环境限制）**：Arnold 渲染与烘焙在 headless/`mayapy` 批处理模式下依赖本机 RLM / Arnold batch 许可；无许可时 `bake_arnold_texture` 和 `render_view(renderer="arnold")` 会明确失败且不输出虚假/水印文件。需要降级渲染时请显式传入 `render_view(..., renderer="arnold", fallback_to_software=True)` 或直接使用默认 `mayaSoftware`；烘焙请使用无需 Arnold 许可的 `bake_map`。
- **headless `capture_viewport`**：`mayapy` headless 模式没有 GUI OpenGL/Viewport 2.0 面板。默认（`allow_fallback=True`）会回退到 Maya Software 渲染并在返回中显式标记 `mode="headless"`、`is_gui_viewport=False`、`fallback="render"` 与警告；若希望严格要求真实 GUI 视口，请传 `allow_fallback=False` 或切换到 Maya GUI 模式。
- **commandPort**：原生 `commandPort` 允许本机任意进程直接执行 Python，需更强隔离时应优先使用默认 TCP JSON bridge。

## 目录

```text
maya_mcp/
  __init__.py      package metadata
  baking.py        bake tools (Transfer Maps, projected AO/curvature, Arnold EXR)
  bridge.py        TCP JSON bridge
  capabilities.py  Maya command/plugin capability probing and Arnold license detection
  client.py        host-side TCP bridge client and BridgeError
  commandport.py   Maya native commandPort transport
  composition.py   multi-image grid sheet composer
  config.py        environment/settings, ASCII staging temp root and Maya child env
  envelope.py      structured {ok, data, error, warnings, artifacts} helpers
  handlers.py      Maya-side scene/object/material/render/save/export/import/script/checkpoint commands
  images.py        render, viewport capture, aspect/film-fit framing and UV snapshot helpers
  jobs.py          bounded async job runner and status polling
  modeling.py      polygon, NURBS, 3D text and curve extrusion tools
  policy.py        project root/external path sandbox, AST cmds allowlist and bounded Python script runner
  protocol.py      length-limited strict finite-JSON framing
  server.py        FastMCP stdio entrypoint (48 tools) and CLI option parser
  session.py       transport/session selection and headless worker lifecycle
  topology.py      topology inspection, retopo, reduce, rebuild and snap
  uv.py            UV projection, unfold, layout, sets and inspection
  versions.py      Maya 2022-2026 installation discovery
  worker.py        mayapy standalone main-thread bridge worker
tests/
  test_bridge.py        bridge, framing, path/code/script policy, render, material, checkpoint and handler tests
  test_commandport.py   commandPort framing, auth, root sandbox, script switch and socket tests
  test_server.py        48-tool schema, CLI settings, MayaSession routing and smoke CLI tests
  maya_smoke.py         end-to-end MCP stdio smoke against real Maya (--year)
```

## 许可

本项目采用 [MIT License](LICENSE)。
