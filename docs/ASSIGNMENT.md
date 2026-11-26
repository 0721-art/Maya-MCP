# 作业工作流

这是一套**通用作业模板**，不是只针对 HONOR 遥控器。HONOR 只是 `examples/honor_remote/` 里的一个示例；换题目时修改 JSON 配置即可。

以 [通用配置模板](../examples/assignment.template.json) 为起点：

1. 填写题目、作品名、学号/姓名、参考图和交付项；
2. 用 `assignment_plan` 获取阶段、命名和预计交付路径；
3. 按计划建模、拓扑、UV、烘焙、截图/渲染和导出；
4. 用 `assignment_check` 核对真实文件及可选的实时低模拓扑。

缺失的参考图或身份信息会以警告/占位符呈现，`ready` 不会在缺少输入时假装为 `true`。工具参数、截图和导出流程见 [README](../README.md#作业工作流)。

课程原始题目已归档于 [HONOR 遥控器示例](../examples/honor_remote/COURSE_REQUIREMENTS.md)，仅用于演示模板如何承载真实作业。
