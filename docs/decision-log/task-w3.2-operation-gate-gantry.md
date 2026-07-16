# 任务卡 W3.2：operation 门闸 + 龙门端点

**发卡**：2026-07-17 · **执行者**：Codex · **前置**：W3.1 已合入 main

## 背景（一段话）

师兄测试版审计 A7 的教训：move/home/z_brake/disconnect 各自为政的三套互斥拼不出完整
覆盖，漏出"归零中锁刹车"的硬件对撞路径。本卡建**唯一的**原子 operation 门闸：所有写
硬件的端点（除 `/api/estop`）都必须过它；占用中一律 409。

## 硬边界

1. **禁止连接真实硬件**；测试全 mock。
2. 只许新增 `src/webapp/gate.py`、`src/webapp/routes_gantry.py`、
   `tests/test_webapp_gate.py`、`tests/test_webapp_gantry.py`；只许修改
   `src/webapp/app.py`（挂路由）。**禁改** `src/hardware/`、`src/webapp/estop.py`。
3. `scripts/check.sh` 全绿；全部 commit。

## 设计（定稿）

**OperationGate**（gate.py）：
- `try_start(device: str, action: str) -> Operation`：原子 test-and-set（一把内部锁只护
  这几行内存操作，绝不跨硬件调用持有）。已有 operation 运行 → 抛
  `OperationConflictError`（`src/hardware/errors.py` 已有该类）→ HTTP 409，响应体带
  当前 operation 详情（id/device/action/started_at/elapsed）。
- operation 在**后台线程**执行（`threading.Thread`，每 operation 一个），完成/异常写回
  operation 记录（result 或结构化 error），gate 释放。
- `GET /api/operations/current`：当前 operation 或 null；`GET /api/operations/{id}`：
  历史记录（内存环形缓冲 50 条足够）。
- 全局一次一个 operation（v1 不做每设备并行——一台机器一个人操作的调试面板，简单
  压倒吞吐；docstring 写明这是有意取舍）。

**龙门端点**（routes_gantry.py，全部过门闸，POST 返回 202 + operation id）：
- `POST /api/gantry/connect`、`/disconnect`（disconnect 也过门闸——A7 教训：运动中关串口）
- `POST /api/gantry/home`
- `POST /api/gantry/move`（body：x/y/z/feed，pydantic 校验，直接复用 backend 的
  `move_to` 签名语义）
- `POST /api/gantry/jog`（axis/distance/feed）
- `POST /api/gantry/recover`（recover_from_alarm）
- `GET /api/gantry/grbl-settings`（validate_grbl_settings 快照，只读也过门闸——它打串口）
- 请求体全部 pydantic 模型；返回统一 `{"operation_id": ..., "accepted": true}`。
- operation 完成事件推进 W3.1 的状态快照（poller 快照里加 `last_operation` 字段，
  gate 完成时写入，SSE 自然带出去）。

## 测试要求

1. 门闸原子性：两线程同时 try_start，恰一个成功一个 409（重复 50 轮）。
2. 409 响应体含当前 operation 的 device/action/elapsed。
3. operation 异常：假 backend 抛 L3Error → operation 记录含结构化 error，gate 已释放，
   下一个 operation 可启动。
4. `/api/estop` 在 operation 运行中**不受门闸影响**（假龙门 move 阻塞中调 estop 立即返回）。
5. move/jog 请求体校验：缺字段/类型错 → 422，不碰 backend。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] 汇报里列出"所有写硬件端点 → 是否过门闸"对照表（estop 是唯一例外）
