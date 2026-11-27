# Maya MCP — 产品要求

## 目标

做一个**通用的 Autodesk Maya MCP 服务**：Agent 通过 MCP 在真实 Maya 会话或 `mayapy` headless 进程中完成建模、拓扑、UV、烘焙、渲染、截图和导出。服务本身不绑定某一类题目、某个模型或某套交付物；课程作业只是最常见的使用场景之一。

## 功能范围

- **Maya 连接**：支持本地回环 TCP + token 桥接、Maya 原生 `commandPort`，以及 `mayapy` headless worker。
- **场景与对象**：查询 Maya 版本/插件/场景信息，创建和修改变换、层级、显示层、材质。
- **建模**：多边形基础体、曲线、放样、挤出、倒角、镜像、阵列、布尔、合并/分离、分组。
- **拓扑**：拓扑检查、自动/手动重拓扑辅助、减面、重建、吸附、对称。
- **UV**：投影、展开、排布、UV 集、UDIM、UV 检查与截图。
- **烘焙**：高模到低模的 native bake（normal/AO/curvature/color）与 Arnold texture bake（有许可时）。
- **渲染与导出**：相机渲染、GUI viewport 截图、多图拼版、`.ma/.mb` 保存、FBX/OBJ 导出。
- **异步任务**：耗时操作可提交为 job 并轮询状态，避免阻塞 MCP 请求。
- **安全与可控性**：项目根目录约束、token 校验、命令白名单、超时、结构化错误。
- **结构化返回**：所有工具统一返回 `{ok, data, error, warnings, artifacts}` 形式，便于 Agent 判断和串联。
- **版本适配**：发现 Maya 2022–2026 的 `mayapy`，按版本能力降级；未实测版本必须明确标记，不伪装成功。

## 非目标

- 不内置某一门课程、某一套作业模板、某个具体模型或固定命名规则。
- 不把 HONOR 遥控器等示例写进核心代码或工具 schema。
- 不替代人工审美判断；不声称未实际渲染/未实际保存的产物已完成。

## 兼容性

- 宿主 Python：3.11+。
- Maya 侧代码：尽量兼容 Maya 2022 的 Python 3.7；现有主机测试覆盖 Python 3.11。
- 当前实测：Maya 2026。Maya 2022–2025 只实现发现/选择逻辑，标记 `UNTESTED`。

## 验收

- `.\.venv\Scripts\python.exe -m pytest -q tests`（全部用例通过，无跳过）通过。
- Maya 2026 smoke 通过：MCP stdio → 建模、拓扑、UV、bake、render、export、job。
- `get_maya_info`、`list_maya_installations`、至少一个建模/拓扑/UV/烘焙/导出工具可用。
- 仓库不包含作业模板、固定题目或作业验收专用工具；作业流程只能作为文档示例或外部编排。
