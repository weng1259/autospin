# 任务卡 W1.1：HeaterBackend（AI-516P 加热台 ADR-004 合规壳）

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W1.0 已合入

## 背景（一段话）

加热台 = 宇电 AI-516P 温控器，Modbus RTU，挂共享 RS485 总线。真机 bring-up 已于
2026-06-19 通过（能读 PV）。唯一权威旧行为来源：`AutoSpinmotorSystem/hardware/heating_stage/`
（师兄栈，读写逻辑可信，工程规范不合格）。本卡产出正式版 L3 backend，为 Web 面板
和 Agent 工具面提供加热能力。**API 形状以 Agent 工具面为准（ADR-004/006），
不为任何 UI 便利变形。**

## 硬边界

1. **禁止连接真实硬件**；真机 smoke 脚本只写不跑（W5 由 PM gate 执行）。
2. 只许新增 `src/hardware/heater_backend.py`、`tests/test_heater_backend.py`、
   `tools/heater_smoke.py`（只写不跑），以及：`src/schema_export.py` 注册新模型、
   `constants.yaml` 增加 heater 段、`docs/api-v1.json` 重导出。**禁改**其余 `src/hardware/*`。
3. 开始/结束跑 `scripts/check.sh` 全绿。

## API（ADR-004 合规，先读 docs/decision-log/ADR-004-l3-api-design-principles.md）

```python
class HeaterBackend:
    def __init__(self, bus: Rs485Bus, unit_id: int, config: HeaterConfig) -> None: ...
    def connect(self) -> None: ...          # 幂等；连上后读一次 PV 验证在线
    def close(self) -> None: ...
    def read_pv(self) -> HeaterStatus: ...  # 当前温度快照（含时间戳）
    def set_sv(self, sv_c: float, *, idempotency_key: str | None = None,
               dry_run: bool = False) -> HeaterActionResult: ...
    def status(self) -> HeaterStatus: ...   # 不打硬件的最近快照 + PV/SV
```

- **安全钳制**：`sv_c` 超出 `constants.yaml` 的 `heater.sv_max_c` 直接拒绝（L3Error，
  不是 clamp 后静默执行）。`sv_max_c` 初始值写 **150**，标注
  `# TODO PM 按 AI-516P 手册与实验需求确认`。
- Modbus 读写全部经 `Rs485Bus.transaction()`（W1.0 产物），事务内只做一次请求-响应。
- 幂等：同 `idempotency_key` 的重复 `set_sv` 返回缓存结果不重发（参考
  `gantry_backend.py` 现有幂等实现与 TTL）。
- dry_run：校验 + 返回将执行的动作描述，不发字节。
- 错误：`L3Error` 家族，human_message 中文 + agent_message 英文 + suggested_action_zh
  （模仿 `src/hardware/errors.py` 现有注册表模式）。
- pydantic 模型 `HeaterStatus` / `HeaterActionResult` 进 `src/hardware/types.py` 同风格
  新文件或就地（跟随仓库现状），并注册进 `src/schema_export.py`，重导出
  `docs/api-v1.json`（schema drift 测试必须绿）。

## 测试要求（fake bus / fake serial）

1. set_sv 正常路径：Modbus 帧字节与 AI-516P 协议一致（寄存器地址从
   `AutoSpinmotorSystem/hardware/heating_stage/` 权威旧实现确认，注明来源行号）。
2. 超上限拒绝：`sv_c > sv_max_c` 抛 L3Error 且不发字节。
3. 幂等：同 key 两次 set_sv 只发一次帧。
4. dry_run 不发字节。
5. CRC 错误 / 超时 → 结构化错误上抛（不吞、不返回 False）。

## 验收

- [ ] `scripts/check.sh` 全绿（含 schema drift 测试）
- [ ] 上述 5 条测试通过
- [ ] `tools/heater_smoke.py` 存在、有 `--port` 参数、顶部注释写明"仅 W5 gate 时由 PM 运行"

## 回滚

新增文件 `git rm`；`schema_export.py`/`constants.yaml`/`api-v1.json` 三处按 commit 整体 revert。
