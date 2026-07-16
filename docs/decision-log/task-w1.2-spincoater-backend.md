# 任务卡 W1.2：SpincoaterBackend（DBLS400 旋涂电机 ADR-004 合规壳）

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W1.0 Rs485Bus 已合入 main

## 背景（一段话）

旋涂电机 = DBLS400 无刷驱动器，Modbus RTU，挂共享 RS485 总线。真机 bring-up 已于
2026-06-20 过 gate（母线 23.25V，100RPM 低速真转）。可信参考实现：
`autospin_system/hardware/spin_motor/`（driver_communication.py + motor_controller.py，
读写逻辑经过真机验证，工程规范不合格）。本卡产出正式版 L3 backend。
**API 形状以 Agent 工具面为准（ADR-004/006）。**

## 硬边界

1. **禁止连接真实硬件**；真机 smoke 脚本 `tools/spincoater_smoke.py` 只写不跑。
2. 只许新增 `src/hardware/spincoater_backend.py`、`tests/test_spincoater_backend.py`、
   `tools/spincoater_smoke.py`，以及注册性修改：`src/schema_export.py`、`constants.yaml`
   （spincoater 段）、`docs/api-v1.json` 重导出。**禁改**其余 `src/hardware/*`。
3. 开始/结束跑 `scripts/check.sh`，全绿才算完。

## 物理事实（写死进代码注释，来源已真机验证）

- ~~`speed_factor = 2.5`：指令值→实际 RPM 的换算~~ **（2026-07-16 审查纠正：此表述错误，
  Codex 曾照此实现出 P0）**：0x8005 写入**直接收 RPM 原值**（手册通讯举例 + 参考实现
  `set_speed()` 双证）；2.5 只是 0x8018 实际转速**回读**的解码系数（raw×20÷8 极）。
- 故障寄存器 `0x801B`：非零 = 驱动器故障，读回后按位解码进结构化错误。
- **无角度反馈**：纯调速，做不了定向停转——在类 docstring 和 api-v1.json 的方法
  docstring 里显式声明这个限制，Agent 需要知道。

## API

```python
class SpincoaterBackend:
    def __init__(self, bus: Rs485Bus, unit_id: int, config: SpincoaterConfig) -> None: ...
    def connect(self) -> None: ...            # 幂等；连上读一次故障寄存器验证在线
    def close(self) -> None: ...
    def start(self, rpm: float, *, idempotency_key: str | None = None,
              dry_run: bool = False) -> SpinActionResult: ...
    def stop(self, *, use_brake: bool = True, idempotency_key: str | None = None,
             dry_run: bool = False) -> SpinActionResult: ...
    def read_fault(self) -> SpinStatus: ...   # 0x801B 解码
    def status(self) -> SpinStatus: ...
```

- **安全钳制**：`rpm` 超出 `constants.yaml` 的 `spincoater.max_rpm` 直接拒绝（L3Error）。
  初始值写 **3000**，标注 `# TODO PM 按工艺需求确认`。方向固定 CCW（bring-up 验证的
  平稳方向），v1 不暴露方向参数。
- 所有 Modbus 读写经 `Rs485Bus.transaction()`；事务内只做一次请求-响应；转速爬升等待
  用锁外分片轮询。
- 幂等 / dry_run / L3Error 中英 + suggested_action_zh：模仿 `gantry_backend.py` 现有模式。
- `SpinStatus` / `SpinActionResult` pydantic 模型注册进 schema_export，合同重导出。

## 测试要求（fake bus）

1. start 帧字节与参考实现一致（注明抄自 `autospin_system/hardware/spin_motor/` 的行号），
   rpm→指令值换算含 speed_factor=2.5。
2. 超 max_rpm 拒绝且不发字节。
3. stop 默认带刹车；`use_brake=False` 路径帧不同。
4. 幂等同 key 只发一次；dry_run 不发字节。
5. 故障寄存器非零 → 结构化错误（含解码位）；CRC/超时 → L3ConnectionError 上抛。

## 验收

- [ ] `scripts/check.sh` 全绿（含 schema drift）
- [ ] 上述 5 条测试通过
- [ ] docstring 含"无角度反馈/不能定向停转"声明

## 回滚

新增文件 `git rm`；注册性三处按 commit 整体 revert。
