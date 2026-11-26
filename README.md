# Maya MCP

一个通用的 Autodesk Maya MCP stdio 服务：让 Agent 在真实 Maya 会话或 `mayapy` headless 进程中完成建模、拓扑、UV、烘焙、渲染、截图和导出。

Maya MCP 不内置作业模板、固定题目或特定交付流程。课程作业只是它最常见的使用场景；题目、命名、参考图和验收标准由外部 Agent/客户端组织。

## 设计来源

参考了 [mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) 的“MCP server + DCC 内桥接”思路，并调研了这些 Maya MCP 项目：

- [PatrickPalmer/MayaMCP](https://github.com/PatrickPalmer/MayaMCP)：Maya `commandPort`、Maya 侧动态工具、低层 MCP。
- [Xxx91n/mcp-for-maya](https://github.com/Xxx91n/mcp-for-maya)：`commandPort`、自动发现端口、多 Maya 实例、场景/相机/资产工具。
- [GimbalGoats/GG_MayaMCP](https://github.com/GimbalGoats/GG_MayaMCP)：`commandPort`、大量类型化工具、Maya 2024+。
- [dcc-mcp/dcc-mcp-maya](https://github.com/dcc-mcp/dcc-mcp-maya)：可发现工具、UV/烘焙专题、Maya 2022/Python 3.7 兼容提示。
- [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server)：`commandPort`、多会话资源。

本仓库没有复制这些项目的代码。当前实现支持两种传输：默认的**本地回环 TCP + token 的 JSON 桥接**，以及 Maya 原生 `commandPort`（`MAYA_MCP_MODE=commandport`）；同时支持 Maya GUI 和 `mayapy` headless。commandPort 模式不需要在 Maya 侧安装 Python 包。更多对比见 [Maya 参考调研](docs/MAYA_REFERENCES.md)。

## 特性

- 标准 MCP stdio 服务，可接入 DSH、Codex、Claude 等 MCP 客户端。
- 42 个工具，覆盖：
  - Maya 安装发现、场景/对象检查、对象与 NURBS 建模；
  - 拓扑检查、重拓扑、减面、重建、吸附、对称；
  - UV 投影、展开、排布、UV 集与 UV 检查；
  - 高模到低模的 native bake（normal/AO/curvature/color）和 Arnold texture bake（需 Arnold 许可）；
  - 渲染、GUI viewport 截图、多图拼版、UV 截图；
  - 保存 `.ma/.mb`、导出 FBX/OBJ；
  - 长任务提交与轮询。
- 支持 Maya 2022–2026 的**发现与版本选择**；当前机器只在 Maya 2026 上完成实测，其他版本会明确标记为 `UNTESTED`。
- 服务进程不 `import maya`；Maya 相关代码只在 Maya 的 Python 进程里执行。
- 路径限制、token 校验、命令白名单、超时与 job 轮询。

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

- **GUI 模式**：Maya 处于打开状态，操作出现在当前场景/Outliner 中，适合人工检查、建模和截图。
- **headless 模式**：`MayaSession` 发现 `mayapy` 后启动 `python -m maya_mcp.worker`，适合自动测试和批量生成。
- **commandPort 模式**：连接 Maya 自带的 `cmds.commandPort` socket，不在 Maya 侧安装包，也不会启动 worker。
- 默认 `MAYA_MCP_MODE=auto`：先探测已有 bridge；没有则启动 headless worker。GUI 需设置 `MAYA_MCP_MODE=gui` 并在 Maya 内启动 bridge；原生 commandPort 使用 `commandport`。

## 环境要求

- 宿主：Python 3.11+。
- Maya：2026 已实测；2022–2025 仅实现发现/选择逻辑，尚未在本机实测。
- Maya 2026 路径（本机示例）：
  - `D:\Maya 2026\Maya2026`
  - `D:\Maya 2026\Maya2026\bin\mayapy.exe`
- Python 依赖：`mcp==1.30.0`、`Pillow>=10,<13`（见 `pyproject.toml`）。

## 安装

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

已有 `.venv` 时只需重新安装：

```powershell
.\.venv\Scripts\python.exe -m pip install -e .
```

## 快速开始

### 1. 只启动 MCP 服务

```powershell
$env:MAYA_MCP_MODE = "headless"
$env:MAYA_MCP_PROJECT_ROOT = "D:\MayaProjects"
python -m maya_mcp.server
```

MCP 客户端通过 stdio 与该进程通信。不要把日志写到 stdout；stdout 只用于 MCP JSON-RPC。

### 2. GUI 模式

1. 打开 Maya，在 Script Editor 中启动 bridge（或使用下面的 commandPort 模式）。
2. 设置 `MAYA_MCP_MODE=gui` 和对应的 `MAYA_MCP_HOST`/`MAYA_MCP_PORT`/`MAYA_MCP_TOKEN`。
3. 启动 `python -m maya_mcp.server`，再让 MCP 客户端连接。

### 3. commandPort 模式

在 Maya 中打开一个 commandPort，例如：

```python
import maya.cmds as cmds
cmds.commandPort(name="127.0.0.1:7001", sourceType="python")
```

然后：

```powershell
$env:MAYA_MCP_MODE = "commandport"
$env:MAYA_MCP_COMMAND_PORT = "7001"
$env:MAYA_MCP_TOKEN = "your-token"
python -m maya_mcp.server
```

commandPort 模式不启动 worker；它直接连接 Maya 的 line socket。Maya 侧需要能导入 `maya_mcp.commandport.dispatch`，或由现有 bridge 注入 dispatch。

## MCP 客户端配置

以 DSH/Codex 一类 stdio MCP 客户端为例，配置结构通常如下（按客户端实际字段调整）：

```json
{
  "mcpServers": {
    "maya": {
      "command": "D:\\Maya-MCP\\.venv\\Scripts\\python.exe",
      "args": ["-m", "maya_mcp.server"],
      "cwd": "D:\\Maya-MCP",
      "env": {
        "MAYA_MCP_MODE": "headless",
        "MAYA_MCP_PROJECT_ROOT": "D:\\MayaProjects"
      }
    }
  }
}
```

首次连接后建议依次调用：

1. `get_maya_info`
2. `list_maya_installations`
3. `get_scene_info`
4. 一个小的 `create_object` 测试

## 工具组

| 组 | 代表工具 |
|---|---|
| 场景/对象 | `get_maya_info`, `list_maya_installations`, `get_scene_info`, `get_object_info`, `create_object`, `modify_object`, `delete_object` |
| 材质/层 | `set_material`, `create_layer`, `assign_layer` |
| 建模 | `create_curve`, `loft_mesh`, `mesh_extrude`, `mesh_bevel`, `mesh_mirror`, `array_objects`, `mesh_boolean`, `mesh_combine`, `mesh_separate`, `group_objects` |
| 拓扑 | `mesh_topology`, `mesh_retopo`, `mesh_reduce`, `mesh_rebuild`, `mesh_snap`, `set_symmetry` |
| UV | `uv_project`, `uv_unfold`, `uv_layout`, `uv_sets`, `uv_inspect`, `uv_snapshot` |
| 烘焙 | `bake_map`, `bake_arnold_texture` |
| 渲染/截图 | `render_view`, `capture_viewport`, `compose_sheet` |
| 保存/导出 | `save_scene`, `export_scene` |
| 异步 | `submit_job`, `job_status` |
| 代码执行 | `execute_maya_code`（默认关闭，白名单限制） |

## 典型工作流

### 建模

1. `get_scene_info` 查看场景。
2. `create_object` / `create_curve` / `loft_mesh` 建立大形。
3. `modify_object`、`mesh_extrude`、`mesh_bevel`、`mesh_boolean` 细化。
4. `set_material`、`create_layer` 整理材质与显示层。
5. `render_view` 或 `capture_viewport` 检查轮廓。
6. `save_scene` 保存 `.ma/.mb`。

### 拓扑与 UV

1. `mesh_topology` 检查面数、非流形边、四边形比例。
2. `mesh_retopo` / `mesh_reduce` / `mesh_rebuild` 生成低模候选。
3. `uv_project` → `uv_unfold` → `uv_layout` → `uv_inspect`。
4. `uv_snapshot` 输出 UV 图。
5. `mesh_snap` 将低模吸附到高模表面。

### 烘焙与交付

1. 确认高低模命名和对应关系。
2. `bake_map` 生成 normal/AO/curvature/color PNG；有 Arnold 许可时可用 `bake_arnold_texture`。
3. `compose_sheet` 合并多视角/高低模对比图。
4. `export_scene` 导出 FBX/OBJ。
5. 用 `render_view` 生成最终检查图。

课程作业只是上述流程的一个使用场景：题目、命名、截图视角和提交格式应由调用方提供，不应写进 MCP 核心代码。

## 测试

```powershell
$env:PYTHONPATH = "D:\Maya-MCP"
python -m pytest -q tests
python tests/maya_smoke.py --year 2026
```

- 单元测试覆盖 bridge、commandPort、模式和工具 schema。
- Maya 2026 smoke 覆盖 MCP stdio → 建模、拓扑、UV、bake、render、export、job。
- Maya 2022–2025 会显示 `UNTESTED`，不会伪装通过。
- Arnold 烘焙需要有效 Arnold 许可；无许可时应明确报错。

## 已知限制

- **Maya 版本**：当前只在 Maya 2026 实测；2022–2025 的发现/降级逻辑存在，但未在本机验证。
- **commandPort**：有单元测试和协议实现，但尚未在真实 Maya GUI 中完成 E2E；standalone `mayapy` 不能替代 GUI commandPort。
- **Arnold**：Arnold license 不可用时 `bake_arnold_texture` 会失败；native bake 仍可使用。
- **`execute_maya_code`**：默认关闭，仅允许白名单 `cmds` 调用；不要用它绕过项目根目录或安全策略。
- **作业交付物**：本仓库不生成特定课程的模型、截图或报告；这些由 Agent 按用户提供的题目和资产完成。

## 目录

```text
maya_mcp/
  bridge.py        TCP JSON bridge
  commandport.py   Maya commandPort transport
  client.py        client-side connection errors
  config.py        environment/settings
  handlers.py      Maya-side commands
  modeling.py      modeling tools
  topology.py      topology tools
  uv.py            UV tools
  baking.py        bake tools
  images.py        render/capture helpers
  jobs.py          async jobs
  server.py        FastMCP entrypoint
  session.py       transport/session selection
  versions.py      Maya version discovery
tests/
docs/
```

## 许可

见仓库根目录 `LICENSE`（如存在）；未声明时按仓库默认策略处理。
