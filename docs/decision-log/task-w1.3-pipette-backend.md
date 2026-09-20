# 任务卡 W1.3：PipetteBackend（电动移液枪 ADR-004 合规壳）

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W1.0 Rs485Bus 已合入 main

## 背景（一段话）

电动移液枪，Modbus RTU，挂共享 RS485 总线。真机完整 gate 已于 2026-06-29 通过
（home + aspirate + dispense），driver 两处关键修复已入库：动作码低半区 +1（home=0x01）、
home 轮询 homed 标志 + 超时发 IMM_STOP 刹车、位置原子读 signed、运动默认参数
50/1250/1250（手册建议值）。唯一权威旧行为来源：`AutoSpinmotorSystem/` 内 pipette driver
（git log 搜 "移液 driver" 两个 commit）。退 tip 机构硬件已修好（2026-07-16 PM 确认，
顶得脱），按正常动作实现。**API 形状以 Agent 工具面为准（ADR-004/006）。**

## 硬边界

1. **禁止连接真实硬件**；`tools/pipette_smoke.py` 只写不跑。
2. 只许新增 `src/hardware/pipette_backend.py`、`tests/test_pipette_backend.py`、
   `tools/pipette_smoke.py`，以及注册性修改：`src/schema_export.py`、`constants.yaml`
   （pipette 段）、`docs/api-v1.json` 重导出。**禁改**其余 `src/hardware/*` 与
   `AutoSpinmotorSystem/`（权威旧行为来源只读）。
3. 开始/结束跑 `scripts/check.sh`，全绿才算完。

## API

```python
class PipetteBackend:
    def __init__(self, bus: Rs485Bus, unit_id: int, config: PipetteConfig) -> None: ...
    def connect(self) -> None: ...     # 幂等；连上读一次状态验证在线
    def close(self) -> None: ...
    def home(self, *, idempotency_key: str | None = None,
             dry_run: bool = False) -> PipetteActionResult: ...
    def aspirate(self, volume_ul: float, *, idempotency_key: str | None = None,
                 dry_run: bool = False) -> PipetteActionResult: ...
    def dispense(self, volume_ul: float, *, idempotency_key: str | None = None,
                 dry_run: bool = False) -> PipetteActionResult: ...
    def eject_tip(self, *, idempotency_key: str | None = None,
                  dry_run: bool = False) -> PipetteActionResult: ...
    def status(self) -> PipetteStatus: ...
```

- **安全规则**：
  - `volume_ul` 超出 `constants.yaml` 的 `pipette.max_volume_ul` 拒绝（L3Error）。
    初始值从参考实现/手册取，取不到写 **1000** 加 `# TODO PM 确认`。
  - **所有等待必须有界**（参考实现的模式：wait 超时后主动发 IMM_STOP 再抛错，
    不允许无限轮询）。轮询在 `Rs485Bus.transaction()` 外分片，每次迭代单独开事务。
  - 未 home 时 aspirate/dispense 拒绝（结构化错误，建议动作 = 先 home）。
- 幂等 / dry_run / L3Error 中英：模仿 `gantry_backend.py` 现有模式。
- `PipetteStatus` / `PipetteActionResult` 注册进 schema_export，合同重导出。

## 测试要求（fake bus）

1. home/aspirate/dispense/eject 帧与参考实现一致（含动作码 +1 修复，注明抄自的行号）。
2. 超量程拒绝且不发字节；未 home 的 aspirate 拒绝。
3. home 轮询超时 → 发 IMM_STOP → 抛结构化错误（断言 IMM_STOP 帧确实发出）。
4. 幂等同 key 只发一次；dry_run 不发字节。
5. CRC/超时 → L3ConnectionError；所有异常路径不吞错、不返回 False。

## 验收

- [ ] `scripts/check.sh` 全绿（含 schema drift）
- [ ] 上述 5 条测试通过

## 回滚

新增文件 `git rm`；注册性三处按 commit 整体 revert。
