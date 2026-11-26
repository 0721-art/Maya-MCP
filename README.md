# Maya-MCP

Maya 2026 的多 Agent 建模 MCP 服务，参考 [mcp-for-blender](https://github.com/ahujasid/mcp-for-blender) 的“MCP Server + DCC 内 Python 桥接”架构，面向“在对话中驱动 Maya 完成建模作业”的场景。

## 目标

- 提供标准 MCP stdio 服务，可被 DSH、Codex、Claude 等 Agent 调用。
- 通过 Maya 内 Python 命令端口操作 Maya 2026，支持场景检查、对象/变换、材质、渲染、截图、保存与导出。
- 附带 HONOR 遥控器作业的建模、低模拓扑、UV、三视图与对比截图产物规范，以及可复现的 Maya 构建脚本。

## 作业要求

详见 `docs/ASSIGNMENT.md`。核心交付：

1. 高质量中模 `.mb` 源文件，图层和大纲视图清晰。
2. 正/侧/背三角度合并为一张截图。
3. 低模重拓扑 + UV 展开，提交 FBX/OBJ；主/顶/左三视角高低模对比图 + 低模 UV 截图。
4. 文件命名：`学号-姓名-作品名称.mb` / `学号-姓名-作品名称.fbx`（学号、姓名待填）。

## 环境

- Maya 2026：`D:\Maya 2026\Maya2026`
- `mayapy.exe`：`D:\Maya 2026\Maya2026\bin\mayapy.exe`
- 开发目录：`D:\Maya-MCP`

## 状态

仓库已初始化；MCP 服务与作业资产由 Agents-MCP 模式 A 流程开发中。
