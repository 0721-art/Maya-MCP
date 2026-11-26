# Maya MCP 参考调研

这份文档记录本项目为什么采用当前的 bridge 结构，以及它与常见的 Maya MCP 项目有什么差异。它不声称本仓库已经实现下面所有项目的功能。

## 调研对象

| 项目 | 大致规模 | 传输/接入 | 值得借鉴 |
|---|---:|---|---|
| [PatrickPalmer/MayaMCP](https://github.com/PatrickPalmer/MayaMCP) | 124 stars | Maya `commandPort` | Maya 侧动态工具、零 Maya 包安装、Maya 2023/2025 验证、工具命名空间隔离 |
| [Xxx91n/mcp-for-maya](https://github.com/Xxx91n/mcp-for-maya) | 9 stars | `commandPort` + stdio | 自动扫描端口、多 Maya 实例、场景图/相机/检查点/资产导入 |
| [GimbalGoats/GG_MayaMCP](https://github.com/GimbalGoats/GG_MayaMCP) | 37 stars | `commandPort` | 71 个类型化工具；服务端不 import Maya；Maya 2024+ |
| [dcc-mcp/dcc-mcp-maya](https://github.com/dcc-mcp/dcc-mcp-maya) | 81 stars | streamable HTTP / sidecar | 79+ 工具、29 个渐进式 skill、`maya-uv-ops`/`maya-texture-bake`、Maya 2022/Python 3.7 兼容提示 |
| [chadrik/maya-mcp-server](https://github.com/chadrik/maya-mcp-server) | 43 stars | `commandPort` | 多会话资源、session key 定向 |

## 关键差异

Blender 的 MCP 桥接通常依赖 Blender 内置 Python 和 add-on；Maya 的常见做法则是：

1. 在 Maya 中打开 `commandPort`（Maya 自带的 MEL/Python socket），无需安装 Maya 侧 Python 包；
2. MCP server 通过 socket 执行 Maya 侧代码；
3. 工具发现和多实例切换由 server 负责。

`commandPort` 的优点是零 Maya 侧安装、对 Maya 版本相对宽容；缺点是端口管理、并发、超时和安全性需要额外设计。

## 本项目的选择

当前仓库默认采用**本地回环 TCP + token 的 JSON bridge**，并新增了 Maya 原生 `commandPort` transport：

```text
MCP server ── token TCP JSON ──► Maya GUI bridge / mayapy headless worker
MCP server ── commandPort ────► Maya GUI（无 Maya 侧包安装）
```

原因：

- 同一套协议可以同时服务 GUI 和 headless；
- 每个请求有明确的 token、超时、错误 envelope，job 可轮询；
- 服务端进程不需要 import Maya，便于在普通 Python 3.11 环境运行；
- 路径策略、命令并发和错误分类更容易统一测试；
- `commandport` 模式复用同一工具层，零 Maya 侧包安装，适合已经打开 commandPort 的 Maya。

代价：

- GUI 模式需要在 Maya Script Editor 里运行 `maya_mcp.bridge.start_gui()`；
- `commandPort` 模式也需要显式开启 `cmds.commandPort(...)`，并设置匹配的 token；
- Maya 2022 的 Python 3.7 兼容性需要在目标机补 smoke；
- 当前没有自动发现多个 GUI Maya 实例；commandPort 也只支持显式端口，headless 由 server 启动一个 worker。

## 后续可做的适配

1. 多实例发现：扫描本地端口/进程，把 `session_id` 暴露给 MCP 客户端。
2. Maya 2022–2025 目标机 smoke：至少验证 Python 3.7 语法、`polyRetopo`/`surfaceSampler`/Arnold 差异。
3. 把当前 GUI 启动步骤封装成 Maya shelf 按钮或 `.mod` 插件，但不能假装它已经是零配置。

## 结论

本项目借鉴了 `commandPort` 项目的工具分类、Maya 侧隔离、UV/烘焙专题和类型化工具思路，同时保留自有 bridge 作为默认传输，并实现了可选的 `commandPort` transport；这样既能先把真实 Maya 执行、建模/拓扑/UV/烘焙闭环跑通，也能在已有 commandPort 的 Maya 环境中零安装接入。
