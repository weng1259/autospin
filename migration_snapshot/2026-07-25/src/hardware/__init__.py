"""硬件适配器层：gantry / gripper / spincoater / relay。

每个 backend 按 ADR-004 七原则实现：严格类型 + 结构化错误 + 幂等
+ 状态可查 + 安全边界 + 可观测 + dry-run。
"""
