# Issue #027 — 视觉/测距自动对准 backend（未来开发）

**日期**：2026-04-24
**提出人**：PM + Claude（工具粒度讨论时浮现）
**状态**：open / future（**非当前 phase 目标**）
**优先级**：低（Phase 4 旋涂流水线跑通后再评估）
**类型**：硬件扩展 / L3 新 backend
**涉及文件**（未来）：
- `src/hardware/vision_backend.py`（新建）
- `src/agent/tools.py`（加 `vision_locate_sample` / `vision_measure_z_offset`）
- `constants.yaml`（样品位硬编码 → 视觉识别的迁移路径）
- `docs/decision-log/architecture-roadmap.md` §"Phase 4 前 PM 要拍的决定" §三

## 背景

讨论 Agent tools 粒度（低层原子 vs 高层复合）时浮现：如果机器能"看"，
Agent 就可以从"按 `constants.yaml` 硬编码坐标移动"升级到"扫一眼台面、
找到样品实际位置再下爪"，大幅提升自主度和容错性。

当前路线图已在两处提到但未立项：
- `architecture-roadmap.md` §"Phase 4 前 PM 要拍的决定" §三：样品位
  "用 `constants.yaml` 硬编码还是用摄像头视觉识别？后者是大工程。**初版
  建议硬编码**"——已明确 Phase 4 初版不上视觉
- §"Phase 6 及以后"：把"膜厚测量仪 / 显微镜"列为扩展 backend 范例

本 issue 是把上述"将来要做"的想法落成一条可追踪的条目，方便 Phase 4 真
跑起来时评估是否升级。

## 三档方案（按工作量 / 收益梯度）

### A. 激光测距 / 电容探针（Z 对准专用）——性价比首选

**硬件**：一个串口激光测距或电容距离传感器（几百元），挂在 Z 轴头部
**软件**：`vision_backend.measure_z_offset(x, y)` → 移到 (x, y) → 读距离 → 返回 Z 偏移
**工具**：`vision_measure_z_offset(x_mm, y_mm) → float`

适用：旋涂前"确认样品高度"、取样前"确认样品是否在位"。**XY 位置仍硬编码**。

**触发条件**：Phase 4 跑了 5-10 次旋涂后发现 Z 每次要手调，或样品高度不一致
导致夹爪打滑。

### B. 固定摄像头 + ArUco / fiducial 标记（XY 定位）

**硬件**：USB 工业相机（200-500 元）固定在龙门架顶梁
**软件**：OpenCV detect ArUco → 像素坐标 → 标定矩阵 → 机器坐标
**工具**：`vision_locate_sample(marker_id) → Position`

要做**相机-机器坐标系标定**（9 点标定或手眼标定），一次标完写进 `constants.yaml`。

适用：样品托盘上贴 ArUco，Agent 能"看到所有样品在哪"后再决定抓哪个。

**触发条件**：多样品批量自动化（不是单样品重复旋涂），或托盘位置会变动。

### C. 完整 CV（任意样品 / 孔板识别）

**硬件**：同 B 或更高分辨率
**软件**：模板匹配 / 轻量 CNN / SAM 分割 / etc
**工具**：`vision_scan_workspace() → list[DetectedObject]`

适用：科研场景样品外观多变，或"随便放上去 Agent 自动识别"的交互。

**触发条件**：有真实需求的时候再说。不要为了好玩做。

## 架构契合度

三档都**不需要改现有架构**——就是在 L3 加一个 `vision_backend.py`（照 `gantry_backend.py` 同样的 `connect/get_*/observable` 模板），再把几个工具注册到 MCP server。Agent 自动能用。

这验证了 ADR-003 "每新增一个硬件模块只是加一个 backend + tool"的预期。

## 决策门槛（何时把本 issue 升级成 ADR）

以下任一触发即转 ADR：

- Phase 4 跑完一个完整旋涂 recipe 后，PM 发现手工调 Z / 手工放样品是明显瓶颈
- 组员/老板要求"多样品批量自动化"，硬编码坐标的工作量超过视觉系统
- 采购到了膜厚仪 / 显微镜等视觉类仪器，顺便把定位视觉一起做

## 明确不做（scope 保护）

- **Phase 3.x 阶段不做任何视觉工作**——当前重点是 Agent + 基础运动流水线打通
- **不做"漂亮的可视化摄像头画面"**——如果只是 live view，浏览器直连 MJPEG stream
  就够，不是 L3 backend 的职责
- **不追求通用 CV 框架**——我们是做旋涂仪，不是做 lab vision framework

## 相关

- `architecture-roadmap.md` §"Phase 4 前 PM 要拍的决定" §三：样品位置来源决策
- `architecture-roadmap.md` §"Phase 6 及以后"：膜厚仪 / 显微镜扩展思路
- ADR-003 §Agent-first 愿景：每个新硬件 = backend + tool 的复用模式
- 本 issue 讨论原始对话：2026-04-24 关于"Agent tools 粒度 + 摄像头"的会话
