# 问题跟踪

> **⚠️ 2026-04-13 重要变更**：项目从自研固件栈迁移到 **grbl-Mega-5X + cncjs + 薄 Python orchestrator** 开源栈。详见 [ADR-001](../decision-log/ADR-001-migrate-to-grbl-stack.md)。**Phase 1 spike 验收通过**（2026-04-13），下列 issue 已被 grbl 直接解决（保留原 issue 文件供历史参考）：
>
> - **#001** 闭环脉冲时序 → grbl 硬件定时器 ✅ resolved-by-migration-phase1
> - **#002** 限位保护 → grbl hard/soft limits ✅ resolved-by-migration-phase1
> - **#007** 降速不生效 → grbl realtime override ✅ resolved-by-migration-phase1
> - **#008** 归零流程 → grbl 两阶段 homing ✅ resolved-by-migration-phase1
> - **#016** 命令解析阻塞 → grbl 非阻塞 parser ✅ resolved-by-migration-phase1
> - **#018** 未回零坐标 → grbl alarm 状态 ✅ resolved-by-migration-phase1
> - **#023** 串口协议简陋 → 换 G-code ✅ resolved-by-migration-phase1
>
> 待 Phase 2 cncjs 接管的 issue：**#009**（网页面板统一）、**#020**（网页断开按钮）。
>
> 下列 issue **不受迁移影响**，仍需独立处理：#003（Z 刹车联动责任上移到 Python 层）、#006（硬件 ALM/ENA 接线）、#010/#011（夹爪 RS485）、#021（Git）、#022（24V 接线）、#024（实验记录持久化在 orchestrator 层做）。
>
> **回滚条件已失效**：Phase 1 spike 已验收通过，不再回滚到自研固件路线。当前固件配置见 [`docs/guides/04-grbl-固件配置.md`](../guides/04-grbl-固件配置.md)。
>
> **新增 Phase 1.5 待办**：限位传感器 5V 接线物理可靠性（详见 04-grbl-固件配置.md §6.1），高速运动后偶发 `Pn:XYZ` 误触发——已识别根因为接线松动，物理修复后症状消失，但需要长期解决方案（压接端子/焊接）。

## 待处理

### 高优先级

| #   | 标题                                           | 类型 | 状态 | 日期  | 备注 |
| --- | ---------------------------------------------- | ---- | ---- | ----- | ---- |
| 001 | 闭环模式下脉冲时序缺陷导致电机不平稳           | bug  | resolved-by-migration-phase1 | 03-24 | grbl 硬件定时器 |
| 002 | 固件缺少限位保护，连续运动可撞机               | bug  | resolved-by-migration-phase1 | 03-24 | grbl `$20`/`$21` |
| 006 | 硬件级安全联锁（ALM 停机 + ENA 急停）          | 安全 | open | 03-24 | ALM 物理接线已 Phase 0 完成 2026-04-13；ENA 联锁待 Phase 3 orchestrator 接管 |
| 007 | 连续运动中降速命令不生效                       | bug  | resolved-by-migration-phase1 | 03-24 | grbl realtime override |
| 008 | 回零流程完善（两阶段寻边 + 坐标复位）          | bug  | resolved-by-migration-phase1 | 03-24 | grbl `$H` 两阶段；中间原点两段式 home 留待 Phase 3 用 probe + diode-OR 实现 |
| 009 | 网页面板公共模块抽取与行为统一                  | 架构 | open | 03-24 | 待 Phase 2 cncjs 接管 |
| 010 | 夹爪面板串口通信架构修复                       | bug  | open | 03-24 | 合并原 #010+#012+#013 |
| 011 | 夹爪开/闭/停按钮仍为占位实现                   | 功能 | open | 03-24 | |
| 017 | 多模块协同架构：整合师兄代码，搭建统一控制系统  | 架构 | open | 03-24 | 待 Phase 3 orchestrator 取代 |
| 018 | 未回零坐标被当成绝对毫米坐标展示               | 安全 | resolved-by-migration-phase1 | 03-24 | grbl alarm/lock 状态保护 |
| 019 | 教程把"拔USB线"当急停方式                      | 安全 | open | 03-24 | 来自Codex审查 |

### 中优先级

| #   | 标题                                           | 类型 | 状态 | 日期  | 备注 |
| --- | ---------------------------------------------- | ---- | ---- | ----- | ---- |
| 003 | Z 轴刹车自动联动与状态确认                     | 优化 | open | 03-24 | 合并原 #003+Codex#024 |
| 005 | 驱动器到位信号（PEND）未利用                   | 优化 | open | 03-24 | |
| 014 | 传感器测试可被"常高"故障误判通过               | bug  | open | 03-24 | |
| 016 | 命令解析存在阻塞等待，异常字节会造成顿挫       | 优化 | resolved-by-migration-phase1 | 03-24 | grbl 非阻塞 parser |
| 020 | 网页缺少断开按钮和串口释放                     | 工程 | open | 03-24 | 待 Phase 2 cncjs 接管 |
| 021 | 项目未纳入 Git 版本管理                        | 工程 | open | 03-24 | 来自Codex审查 |
| 022 | 24V 配电杜邦线拧接，长期可靠性不足             | 安全 | open | 03-24 | 来自Codex审查 |
| 028 | 工程入口与 API 合同漂移收口                    | 工程 | open | 04-24 | pytest 入口 / api-v1.json / README 当前状态；Claude 2026-04-24 补充 |
| 029 | 最小 CI 基建（pytest + mypy + schema diff）    | 工程 | open | 04-24 | 与 #028 同批收口；GitHub Actions 三个 gate |

### 低优先级

| #   | 标题                                           | 类型 | 状态 | 日期  | 备注 |
| --- | ---------------------------------------------- | ---- | ---- | ----- | ---- |
| 023 | 串口协议过于简陋，后续需升级                   | 架构 | resolved-by-migration-phase1 | 03-24 | 改用标准 G-code |
| 024 | 缺少实验记录持久化                             | 工程 | open | 03-24 | 来自Codex审查 |

## 已关闭

| #   | 标题                                           | 原因 | 日期  |
| --- | ---------------------------------------------- | ---- | ----- |
| 004 | architecture.md 拨码开关记录未更新为闭环        | 已修复 | 03-24 |
| 012 | 二进制串口数据被文本解码/再编码污染             | 合并入 #010 | 03-24 |
| 013 | 波特率扫描后恢复连接流程存在端口状态错误        | 合并入 #010 | 03-24 |
| 015 | 未利用 ENA 输入构建硬件级急停链路               | 合并入 #006 | 03-24 |

## 参考

- `codex-review-prompt.md` — 提交给 Codex 的审查提示词
- `codex-review-result.md` — Codex 审查完整结果（原 #017-#030，部分已提升为正式 issue）
- `drafts/` — 合并前的旧版 issue 文件存档
