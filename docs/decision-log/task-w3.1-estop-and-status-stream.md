# 任务卡 W3.1：急停通道 + 状态快照流（W3 安全核心）

**发卡**：2026-07-17 · **执行者**：Codex · **前置**：W3.0 已合入 main

## 背景（一段话）

本卡是整个 Web 层的安全核心，对应师兄测试版审计的两个 P0 反面教材：急停被大锁排队
（A6）、状态查询挤进硬件锁（A6）。设计不变量：**急停路径上不允许出现任何本服务自建的
锁或队列**；状态读取只碰内存快照，永不直接打硬件。

## 硬边界

1. **禁止连接真实硬件**；测试全用假 backend/假 SystemEstop。
2. 只许新增 `src/webapp/estop.py`、`src/webapp/poller.py`、`tests/test_webapp_estop.py`、
   `tests/test_webapp_poller.py`；只许修改 `src/webapp/app.py`（挂路由）与
   `src/webapp/registry.py`（暴露 poller）。**禁改** `src/hardware/` 与 `src/system_estop.py`。
3. `scripts/check.sh` 全绿；全部 commit。

## 设计（定稿）

**急停端点** `POST /api/estop`：
- handler 内**直接**调 `registry.estop.halt_all()`，前面不查 operation、不拿任何应用层锁、
  不做幂等缓存；返回 `EstopReport`（已在合同里）。
- 该端点保留 token 鉴权（和其它路由一致），但**必须**在 app 路由表中先于任何中间件的
  重逻辑注册；不允许后续卡给它加"运行中禁止"之类的门闸——在文件顶注释写死这条禁令。

**状态快照** `StatusPoller`：
- 后台线程按设备轮询（默认 0.5s 一轮，可配），每台设备调各自 `status()`（不打硬件的
  快照方法）+ 周期性调 gantry `get_status()`；结果写进一个带锁的内存 dict（snapshot +
  单调递增 seq + 时间戳）。设备抛错→快照里记 error 字符串，poller 不死（循环内逐设备
  try/except）。
- `GET /api/status`：返回当前快照（读内存，微秒级，永不阻塞在硬件上）。
- `GET /api/status/stream`：SSE（`text/event-stream`），快照 seq 变化即推送一帧 JSON；
  客户端断开要干净退出 generator。用 FastAPI 的 `StreamingResponse` + asyncio 队列或
  轮询 seq 实现，二选一，注释说明选择理由。
- poller 生命周期挂 app lifespan：startup 启动，shutdown 停线程 + **best-effort 调一次
  `halt_all()`**（服务退出=停机，注释说明这是有意的安全语义；mock 模式跳过）。

## 测试要求

1. `/api/estop`：假 estop 记录调用次数；即使 poller 线程被人为卡死（假设备 status()
   sleep 阻塞），estop 端点仍立即返回（并发测试：TestClient 线程 + 计时断言 < 1s）。
2. `/api/status` 返回快照结构（含 seq/ts/per-device）；设备 status() 抛错时快照含 error
   字段且后续轮询继续（seq 递增）。
3. SSE：读流拿到至少 2 帧、seq 递增；断开后 generator 结束（无线程泄漏——poller 线程数
   前后一致）。
4. lifespan shutdown 调了 halt_all（假 estop 断言）。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] estop 路径代码评审点：handler 函数体内除 halt_all 与序列化外无任何其它调用（写进汇报）
