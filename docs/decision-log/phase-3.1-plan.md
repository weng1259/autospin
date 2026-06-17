# Phase 3.1 施工计划：L3 大脑骨架 + 可点击验收

- **日期**：2026-04-20
- **状态**：待 PM 签字确认
- **关联**：[ADR-003](ADR-003-agent-first-vision.md)（Agent-first 愿景）· [ADR-004](ADR-004-l3-api-design-principles.md)（API 规范）· [migration-checklist Phase 3.1](migration-checklist.md)
- **适用记忆**：`feedback_vertical_slices_ui_validation.md`（每个里程碑都要 PM 能亲自点一下）

---

## 一句话目标

在 5 天内搭好 L3 Python 大脑的骨架（按 ADR-004 规范），**且每一天结束 PM 都能在浏览器点一个按钮验证当天进度**。

## 为什么这么切（跟原计划的差别）

**原 migration-checklist Phase 3.1 计划**（横向分层）：
```
types.py → errors.py → runlog.py → backend 抽象类 → constants.yaml
```
问题：前 4 天 PM 完全看不见任何东西动。代码再好，PM 都无法验收。

**本计划**（纵向 Slice）：每天完成一个"**PM 能点击的按钮 + 能看到的结果**"，即使功能小也完整。第一天就有可验收的产出。

这不违反 Agent-first 的"UI 降级为辅助工具"原则——ADR-003 给 Streamlit 保留了 ~10% 的预算，本 Phase 只用其中一小部分做验收页面。Agent demo（Phase 3.5）上线后，这个验收页面会被聊天界面替代。

## 前置准备（我负责，~1 小时）

- 在 `src/` 下搭好 ADR-004 规定的目录结构（空文件 + `__init__.py`）
- 给 `tools/spikes/.venv` 补装 `streamlit`（沿用现成 venv，不新建）
- 写一个 `tools/ui/emergency_dashboard.py` 空白首页
- 确认 `streamlit run tools/ui/emergency_dashboard.py` 能在浏览器打开 `localhost:8501`

**PM 验收前置**：我发你一张 localhost:8501 的截图，你看到一个空白页面写着"L3 应急控制台（Phase 3.1 搭建中）"即可。

---

## Slice 1：查状态按钮 → 看见机器当前状态

**预计耗时**：半天（3-4h）

### PM 在网页上能做什么

- 一个绿色按钮："🔍 查状态"
- 按下去 500ms 内，页面显示一张卡片：
  - **状态**：`Idle` / `Alarm` / `Run` / `Disconnected`（有颜色）
  - **位置**：X=0.0mm  Y=-15.0mm  Z=0.0mm
  - **是否归零过**：✅ / ❌
  - **最后更新**：`3ms 前`

### 背后的技术内容（PM 不必看懂）

- `src/hardware/types.py`：`Position` / `MachineStatus` / `MachineState` 枚举
- `src/hardware/gantry_backend.py`：`get_status()` 方法（从 spike A 复制过来）
- `tools/ui/emergency_dashboard.py`：添加一个"状态"板块

### PM 验收清单

- [ ] 我打开 `http://localhost:8501`，看见"🔍 查状态"按钮
- [ ] 点一下，500ms 内出结果
- [ ] 位置数字和机器实际位置对得上（可以用 bCNC 或 cncjs 交叉验证一次）
- [ ] **故意把 Arduino USB 拔掉**再点按钮，卡片显示红色"已断开"，不是 Python 栈追踪
- [ ] 我截图发给你，你能复现一遍

### 失败回滚

- 纯新代码，回滚就是 `git reset HEAD^`
- 不会影响 Arduino 固件和已有 `tools/spikes/`

### PM 签字位：签了才进 Slice 2

```
Slice 1 验收通过：✅  签字人：Kevin  日期：2026-04-20
```

详细验收记录：[verification/slice-1-status-query.md](verification/slice-1-status-query.md)
（实际位置：`docs/verification/slice-1-status-query.md`）

---

## Slice 2：归零按钮 → 机器真归零 + 历史记录

**预计耗时**：1 天

### PM 在网页上能做什么

- 新增一个红色按钮："🏠 归零（会动机器）"——按之前弹 confirm 对话框
- 按下去后：
  - 页面上方进度条："Z 轴归零中 → X/Y 轴归零中 → 完成"
  - 完成后卡片刷新，"是否归零过 ✅"
  - 页面下方**历史记录表格**新增一行：

| 时间 | 动作 | 结果 | 耗时 | 备注 |
|---|---|---|---|---|
| 18:32:15 | home | ✅ 成功 | 12.3s | event_id: a5b7… |

### 背后的技术内容

- `src/hardware/errors.py`：完整 L3Error 层级（7 种错误）
- `src/runlog.py`：SQLite 记录每次 API 调用
- `src/event_bus.py`：内存 pub/sub（给 UI 实时推进度）
- `src/observable.py`：`@observable` 装饰器（自动记录 + 广播）
- `GantryBackend.home(idempotency_key)` 完整实现（带 Z 刹车时序）
- `tools/ui/emergency_dashboard.py`：归零按钮 + 历史表格板块

### PM 验收清单

- [ ] 点归零按钮，机器**真的动**——Z 先归、然后 X/Y 归
- [ ] 页面进度条跟着变
- [ ] 完成后历史表格出现一行
- [ ] **故意断开 USB 再点**，红色错误弹窗："USB 未连接，请检查端口"——不是 Python 栈追踪
- [ ] **快速连点 5 次归零**，历史表格只出现 1 行（幂等性生效）
- [ ] 刷新页面，历史表格仍在（数据写 SQLite 不丢）

### 失败回滚

- Slice 2 独立 commit，回滚丢一天工作
- 不会破坏 Slice 1 的状态查询功能

### PM 签字位

```
Slice 2 验收通过：✅  签字人：Kevin  日期：2026-04-20
```

详细验收记录：`docs/verification/slice-2-home-and-history.md`

---

## Slice 3：输入坐标"去这里" → 机器移动 + 实时位置

**预计耗时**：1 天

### PM 在网页上能做什么

- 三个输入框：X / Y / Z（带范围提示 "-280 到 0"）
- 一个按钮："🎯 去这里"
- 按下后：
  - 页面顶部有一个**动态位置数字**，每 100ms 刷新（移动中你能看到它在走）
  - 右上角多一个红色"🛑 停"按钮，移动中可用
  - 完成后历史表格新增一行

### 背后的技术内容

- `GantryBackend.move_to(Position)` 完整实现（带 soft limit 预检）
- `GantryBackend.halt()` 实现
- `src/event_bus.py` 实时广播 status（100ms 一次）
- Streamlit 的 websocket / rerun 订阅 event_bus
- 安全边界：`constants.yaml` 存工作空间硬上限

### PM 验收清单

- [ ] 输入 `X=-100 Y=-100 Z=-10` 点"去这里"，机器真移动
- [ ] 顶部动态数字实时变（不是只有终点刷新）
- [ ] **移动中点"🛑 停"**，机器在 500ms 内停下
- [ ] **输入 X=-500**（超范围），按钮按下前输入框变红或弹警告，机器不动
- [ ] 历史表格完整记录

### PM 签字位

```
Slice 3 验收通过：✅  签字人：Kevin  日期：2026-04-20
```

详细验收记录：`docs/verification/slice-3-move-to.md`

---

## Slice 4：故意做错 → 中文错误 + 建议动作

**预计耗时**：半天（3-4h）

### PM 在网页上能做什么

- 故意制造 3-5 种错误场景，每种都弹出**黄色警告框**（不是红色，红色留给真 alarm）：

| 场景 | 我做什么 | 期望看到 |
|---|---|---|
| 没归零就 move | 断电重连 Mega，不归零直接点"去这里" | "机器未归零，请先点 🏠 归零按钮" |
| 坐标超限 | 输入 X=-500 | "X=-500 超出 [-280, 0]，请调整坐标" |
| 断连后 move | 拔 USB 点"去这里" | "USB 未连接，请检查 /dev/cu.wchusbserial110" |
| 运行中 move | 移动中再点"去这里" | "机器正在 Run 状态，请等 Idle 或先点 🛑" |

- 每条错误信息里都**带一个绿色"按建议操作"快捷按钮**（比如"立即归零"），点了自动执行建议动作

### 背后的技术内容

- `errors.py` 所有子类的 `suggested_action` 字段填好中文
- 错误从 backend 抛出 → Streamlit catch → 人话展示
- "按建议操作"按钮是一个小的规则引擎：`error_code → action_button`

### PM 验收清单

- [ ] 5 种场景每种都试一遍，错误消息都是中文 + 人能看懂
- [ ] Python 栈追踪**不会**出现在 UI 上
- [ ] 栈追踪仍在 `runlog.db` 和日志文件里（Agent 和开发者能查）
- [ ] "按建议操作"快捷按钮至少在 2 种错误下工作

### PM 签字位

```
Slice 4 验收通过：□  签字人：_______  日期：_______
```

---

## Slice 5：故意撞 alarm → 一键恢复

**预计耗时**：半天

### PM 在网页上能做什么

- 故意做一件会撞 alarm 的事（比如归零过程中我模拟拔限位传感器线）
- 页面整体**变红**，顶部出现大字"⚠️ ALARM:N — 具体原因"
- 弹出一个"🔧 清除并恢复"按钮
- 按下后：
  - 系统自动执行：unlock → home → 回到 idle
  - 页面颜色恢复，历史表格记录整个恢复过程

### 背后的技术内容

- `GantryBackend.unlock_alarm()` 实现
- Alarm 事件监听 → event_bus 广播 → UI 变红
- "清除并恢复"是组合动作：unlock + home（带 idempotency_key）

### PM 验收清单

- [ ] 故意撞 alarm，页面变红 + 文案正确
- [ ] 一键恢复按钮点了后整个流程完成，无需手动再点归零
- [ ] 恢复后历史表格看得到完整过程（alarm → unlock → home）
- [ ] **真撞硬限位而不是软件模拟**，也能同样恢复

### PM 签字位

```
Slice 5 验收通过：□  签字人：_______  日期：_______
```

---

## 整体验收（Phase 3.1 结束标志）

Slice 1-5 都签字后，再跑一次**端到端演示**：

**剧本**：PM 打开浏览器 → 查状态 → 归零 → 去 A 点 → 故意超限报错 → 去合法点 → 故意撞 alarm → 一键恢复 → 再去 B 点 → 查历史

**Phase 3.1 只有这一条路径通过才算结束**。

---

## 时间预算 + 风险

| 项 | 耗时 | 风险 |
|---|---|---|
| 前置准备 | 1h | 无 |
| Slice 1 | 3-4h | Streamlit 我不熟，可能超 1-2h |
| Slice 2 | 1 天 | runlog / event_bus 是新基建，可能踩坑 |
| Slice 3 | 1 天 | 实时推送机制可能需要 websocket |
| Slice 4 | 半天 | 纯文案工作，风险低 |
| Slice 5 | 半天 | 真撞 alarm 需要 PM 在场配合 |
| 整体演示 | 1h | — |
| **合计** | **4-5 天** | 原计划 4-6 天，节省 1 天 |

**已知坑**：
- Streamlit 的 rerun 模型和长连接 event_bus 接起来需要一点魔法（用 `st.session_state` + 一个轮询 hook）
- Z 轴运动前必须释放 DSTUR CH2 刹车——这个 Slice 2 的 `home()` 里必须正确处理
- 快速连点 jog 会触发 cncjs 那种死锁——但我们用自己的 char-counting 流控（Spike A），应该免疫

## 原则约束

- 每个 Slice 一个独立 git commit
- Slice 签字前不进下一个
- 若某 Slice 超时 50%，停下来和 PM 讨论是不是砍 scope
- Streamlit 页面"丑但能用"——Phase 3.5 后会被 Agent 聊天替代，不值得投入美化

## PM 最终签字

我（PM）批准按本计划执行 Phase 3.1：

```
签字：_______  日期：_______
```

---

## 计划外 / 延期到 Phase 3.2 的

- `GripperBackend`（夹爪）—— Slice 1-5 只碰 GantryBackend
- `RelayBackend`（继电器）—— 仅 Z 刹车在 GantryBackend 内部调用；完整 RelayBackend 留 Phase 3.3
- JSON schema 导出 + Agent tool spec —— 留 Phase 3.2 末尾
- `mypy --strict` 全绿 —— 尽力而为，若某个类型太复杂允许局部放宽（Phase 3.4 前收齐）
