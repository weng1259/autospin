# 任务卡 W1.4：LinearStageBackend（Emm RS485 丝杆滑台 ADR-004 合规壳）

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W1.0-W1.3 已合入 main

## 背景（一段话）

移液枪的单轴丝杆滑台 = 张大头 ZDT Emm42 闭环步进，2026-06-30 起**替换了原 GRBL Z2/A 轴方案**
（gantry_backend 里的 move_z2_to 等是旧物理状态的死代码，本卡不碰，另卡退役）。
挂共享 RS485 总线（与加热台/旋涂/移液同一物理口），站址 4、波特率 115200、行程 100mm、
2mm 导程、**无限位开关**（原生碰撞归零，方向 1、限速 ≤300 RPM）。
参考实现（真机验证过）：`docs/references/bro-linear-stage-20260716/linear_stage.py`
（**只读**，从测试版仓库收编的快照）。协议细节（帧格式、0x6B 固定校验尾、指令码、
标志位）全部以该文件为准，实现处注明抄自的行号。
**API 形状以 Agent 工具面为准（ADR-004/006）。**

## 硬边界

1. **禁止连接真实硬件**；`tools/linearstage_smoke.py` 只写不跑。
2. 只许新增 `src/hardware/linearstage_backend.py`、`tests/test_linearstage_backend.py`、
   `tools/linearstage_smoke.py`，以及注册性修改：`src/schema_export.py`、`constants.yaml`
   （linear_stage 段）、`docs/api-v1.json` 重导出。**禁改**其余 `src/hardware/*`（含
   gantry_backend 的 Z2 遗留）与 `docs/references/*`。
3. 开始/结束跑 `scripts/check.sh`，全绿才算完；结束必须全部 git commit（小步、中文一行）。

## API

```python
class LinearStageBackend:
    def __init__(self, bus: Rs485Bus, address: int, config: LinearStageConfig) -> None: ...
    def connect(self) -> None: ...   # 幂等；连上读一次状态/位置验证在线
    def close(self) -> None: ...     # 只标记断开，不关共享总线（见 heater/spincoater 同款）
    def home(self, *, idempotency_key: str | None = None,
             dry_run: bool = False) -> LinearStageActionResult: ...
    def move_to(self, position_mm: float, *, idempotency_key: str | None = None,
                dry_run: bool = False) -> LinearStageActionResult: ...
    def stop(self) -> LinearStageActionResult: ...   # 立即停，不做幂等缓存
    def status(self) -> LinearStageStatus: ...
```

- **归零门槛（修测试版审计 F6）**：未 home 成功过，`move_to` 直接拒绝
  （`MachineNotHomedError` 子类，建议动作=先 home）；home 抛错/超时后 homed 标志必须复位。
- **到位等待内置（修测试版审计 F7）**：`move_to` 发令后在 backend 内轮询到位
  （位置容差与轮询间隔进 config，初始 0.25mm / 0.1s），**轮询每次迭代单独开
  `Rs485Bus.transaction()`**，绝不持锁等待；轮询同时检查堵转/掉电等标志位
  （参考实现的 flags 读取），异常标志 → 先发 stop → 抛结构化错误。
- **所有等待有界**：home 与 move 各有超时（config，初始 home 35s / move 15s，
  对齐参考实现），超时 → stop → `L3Error` 上抛。
- 行程钳制：`position_mm` 超出 `[0, travel_mm]` 拒绝零字节；`travel_mm: 100.0`
  加 `# TODO PM 实测确认`。
- 幂等 / dry_run / L3Error 中英：模仿已合入的 heater/pipette backend 模式
  （注意 dry_run 不进幂等缓存已在 observable 层处理）。
- `LinearStageStatus` / `LinearStageActionResult` 注册 schema_export，合同重导出。

## 测试要求（fake bus，模仿 tests/test_pipette_backend.py 的结构）

1. home/move/stop/读状态帧字节与参考实现一致（0x6B 校验尾；注明抄自行号）。
2. 未 home 的 move_to 拒绝零字节；home 失败后 homed 复位（再 move 仍拒绝）。
3. move 轮询超时 → stop 帧发出（断言是最后一帧）→ 结构化错误；堵转标志置位 → 同上。
4. 行程外拒绝零字节；幂等同 key 单帧；dry_run 零 transaction。
5. 校验尾错误/短读 → L3ConnectionError 上抛，无吞错。

## 验收

- [ ] `scripts/check.sh` 全绿（含 schema drift）
- [ ] 上述 5 条测试通过
- [ ] 全部改动已 commit，工作树无新增未跟踪项

## 回滚

新增文件 `git rm`；注册性三处按 commit 整体 revert。
