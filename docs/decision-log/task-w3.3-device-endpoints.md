# 任务卡 W3.3：外设端点（加热/旋涂/移液/滑台/继电器/夹爪）

**发卡**：2026-07-17 · **执行者**：Codex · **前置**：W3.2 已合入 main

## 背景（一段话）

把剩余六个 backend 按 W3.2 已定型的模式（pydantic 请求体 → operation 门闸 → 后台线程 →
202 + operation id）机械展开成端点。**照抄 routes_gantry.py 的形状，不发明新模式。**

## 硬边界

1. **禁止连接真实硬件**；测试全 mock。
2. 只许新增 `src/webapp/routes_devices.py`、`tests/test_webapp_devices.py`；只许修改
   `src/webapp/app.py`（挂路由）与 `src/webapp/registry.py`（若 mock 注册表缺假设备）。
   **禁改** `src/hardware/`、`src/webapp/gate.py`、`src/webapp/estop.py`。
3. `scripts/check.sh` 全绿；全部 commit。

## 端点清单（全部过门闸；方法签名语义 = `docs/api-v1.json` 对应 backend 方法）

| 设备 | 端点 |
|---|---|
| heater | `POST /api/heater/connect` `/set-sv`（body: sv_c）`GET /api/heater/pv`（read_pv，打硬件，过门闸） |
| spincoater | `POST /api/spincoater/connect` `/start`（body: rpm）`/stop`（body: use_brake 默认 true）`GET /api/spincoater/fault`（read_fault，过门闸） |
| pipette | `POST /api/pipette/connect` `/home` `/aspirate`（body: volume_ul）`/dispense` `/eject-tip` |
| linear_stage | `POST /api/linearstage/connect` `/home` `/move`（body: position_mm）`/stop`（**不过门闸**，急停语义，同 estop 直通并在文件顶注释说明） |
| relay | `POST /api/relay/ch`（body: channel 3-8, on: bool；**channel 1/2 一律 422 拒绝**——CH1 夹爪 CH2 Z刹车只能走各自专用路径，不给通用开关误触） |
| gripper | `POST /api/gripper/open` `/close` |

- 设备未接入（registry 对应 None）→ 503 结构化 JSON（error_code `L3.DEVICE_NOT_ATTACHED`
  风格，本地定义即可，不改 errors.py）。
- 幂等 key：每个写端点接受可选 `idempotency_key` 字段，原样传给 backend。

## 测试要求

1. 每设备最少 2 条：正常路径（假 backend 收到正确参数）+ 门闸占用时 409。
2. relay channel 1/2 → 422 且不碰 backend（安全项，必测）。
3. linearstage `/stop` 在 operation 运行中仍立即执行（直通断言）。
4. 未接入设备 → 503 结构化 JSON。

## 验收

- [ ] `scripts/check.sh` 全绿
- [ ] 汇报附端点全表（路径/方法/过不过门闸/请求体字段）
