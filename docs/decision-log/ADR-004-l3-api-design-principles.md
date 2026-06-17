# ADR-004：L3 Python Orchestrator API 设计规范（Agent-ready）

- **日期**：2026-04-20
- **状态**：已决定，**必须先于任何 L3 代码实现**
- **决策人**：Kevin
- **影响范围**：整个 `src/` 目录的 API surface、所有后续硬件适配器（gantry / gripper / spincoater / relay / ...）、Agent 集成层
- **关联**：[ADR-003](ADR-003-agent-first-vision.md)（愿景）· [ADR-002](ADR-002-replace-cncjs.md)（L2 选型）

---

## 背景

ADR-003 定下 "API 是产品，不是 UI" 的原则。那就要求 API 的设计标准**高于传统"给开发者用"的 API**——因为它的主要 consumer 是 LLM Agent，既没有"开发者的经验直觉"可以脑补缺失的上下文，也没有"看错了再改"的低成本试错能力。

本文档是具体规范。要求：所有 L3 代码 merge 前对照本文档 review。

## 七条核心原则

### 原则 1：严格类型签名（Typed Everything）

所有公共 API 方法必须有完整的类型注解：参数、返回值、异常。优先用 `pydantic.BaseModel` 或 `dataclasses`，而不是 `dict`。

**反面教材**：
```python
def move_to(self, position, speed=None, wait=True):
    """移动到指定位置"""
    ...
```

问题：Agent 要猜 `position` 是 `(x,y,z)` tuple 还是 dict 还是 numpy array？`speed` 的单位是什么？`wait=True` 返回啥？

**正确做法**：
```python
from pydantic import BaseModel, Field
from typing import Literal

class Position(BaseModel):
    x_mm: float = Field(..., ge=-280.0, le=0.0, description="X 坐标，归零后合法范围 [-280, 0]")
    y_mm: float = Field(..., ge=-280.0, le=0.0)
    z_mm: float = Field(..., ge=-95.0, le=0.0)

class MoveResult(BaseModel):
    success: bool
    final_position: Position
    duration_ms: float
    event_id: str  # 可查 runlog

def move_to(
    self,
    target: Position,
    feed_mm_min: float = Field(default=2000, ge=1, le=3000),
    wait_for_idle: bool = True,
) -> MoveResult:
    """Move gantry head to an absolute position in machine coordinates.
    
    Raises:
        MachineNotHomedError: 机器未归零
        SoftLimitExceededError: 目标超出工作空间
        AlarmStateError: grbl 当前处于 alarm
        TimeoutError: wait_for_idle=True 且 30s 内没到达
    """
```

这样 Agent 拿到方法 schema 直接就知道合法范围、默认单位、可能抛的错。

### 原则 2：结构化错误（Agent-readable）

错误**不能是** `raise Exception("something went wrong")`。必须是：

```python
class L3Error(Exception):
    """所有 L3 自定义错误的基类"""
    error_code: str         # 稳定的标识符，Agent 按此决策
    human_message: str      # 给人看的
    agent_message: str      # 给 Agent 看的（可能更详细，含建议动作）
    recoverable: bool       # 是否可以重试
    suggested_action: str   # "call home() first" 之类

class MachineNotHomedError(L3Error):
    error_code = "L3.MACHINE_NOT_HOMED"
    recoverable = True
    suggested_action = "Call gantry.home() before attempting motion commands."
```

**反面教材**：
```python
raise Exception(f"Move failed: {grbl_raw_response}")
```

Agent 拿到这串看不懂的字符串没法决策。

**正确做法**：在 catch grbl 原始错误时翻译成 L3 语义错误：
```python
try:
    self._send_gcode(cmd, wait_ok=True)
except GrblAlarmError as e:
    if e.alarm_code == 11:
        raise MachineNotHomedError(
            human_message="机器未归零，请先 home",
            agent_message=f"grbl returned ALARM:11 (homing required). "
                         f"Call gantry.home() to recover. Current grbl state: {self.get_state()}",
        )
```

### 原则 3：幂等性 / Idempotency Key

所有"触发动作"的方法必须**可以幂等地重试**。方式有二：

**(a) 天然幂等**的设计优先——例如 `move_to(absolute_position)` 调两次和调一次结果一样。避免"相对移动"作为主 API。

**(b) 不能天然幂等**的方法必须接受 `idempotency_key` 参数：
```python
def start_spin_recipe(
    self,
    recipe: SpinRecipe,
    idempotency_key: str,  # Agent 生成 UUID，重试时复用
) -> SpinRunHandle:
    """如果同 key 已经在跑或刚跑完，直接返回之前的结果，不重复执行"""
```

实现上 L3 维护一张 `(key → result)` 缓存，24h TTL。

**这条是专为 Agent 设计**：Agent 调 API 网络超时后不知道动作是否真执行了，重试不能造成物理后果叠加（比如涂两次胶、旋两次）。

### 原则 4：状态可查询（Observable State）

任何时候 Agent 都可以问：
- 机器在哪？→ `gantry.get_position()`
- grbl 是什么状态？→ `gantry.get_machine_state()` → `MachineState` 枚举
- 夹爪有东西吗？→ `gripper.get_state()`
- 当前有命令在执行吗？→ `orchestrator.get_pending_operations()`
- 最近 10 条运动历史？→ `runlog.query(limit=10)`

**所有 state 方法必须**：
1. 只读、无副作用（side-effect-free）
2. 不占用 serial 关键路径（独立 state cache）
3. 返回时间戳（"这个状态是 X 毫秒前读的"）

**反面教材**：
```python
# 让 Agent 自己维护机器状态
if last_move_was_successful:  # Agent 的变量
    do_next()
```

Agent 不该"记得"状态——它崩了重启就丢了。状态是 L3 的责任。

### 原则 5：硬编码安全边界（Safety by Default）

Agent 会犯错（幻觉、数字换算错、recipe 抄错）。L3 必须在参数校验层拦住**物理不可行**的请求，不能让 Agent 有"试一下"的机会。

三层防线：

```python
# 层 1：pydantic Field 静态校验（原则 1 的副产品）
x_mm: float = Field(..., ge=-280.0, le=0.0)

# 层 2：方法内的运行时校验
def start_spin_recipe(self, recipe: SpinRecipe):
    if recipe.max_rpm > self.config.spincoater.max_rpm_hard_limit:
        raise OutOfRangeError(
            error_code="L3.SPINCOATER_RPM_HARD_LIMIT",
            agent_message=f"Requested {recipe.max_rpm} rpm but hardware limit is "
                         f"{self.config.spincoater.max_rpm_hard_limit}. "
                         f"This is a hard limit, not adjustable at runtime."
        )

# 层 3：运行时 watchdog（独立线程监控）
# 运动超时 / 温度超限 / 震动异常 → 强制 halt 并进 alarm
```

**重要**：安全边界配置读自 `constants.yaml`，在代码 review 时由人工审——**不允许 Agent 通过 API 修改自己的安全边界**。

### 原则 6：每个操作都 Observable

每一次 API 调用产生至少一条结构化事件：

```python
# 写入 runlog.db (SQLite)
{
    "event_id": "uuid",
    "timestamp": "2026-04-20T18:30:00.123Z",
    "method": "gantry.move_to",
    "params": {...},
    "result": "success" | "error" | "timeout",
    "duration_ms": 1234,
    "caller": "agent:claude-opus" | "human:streamlit" | "test:pytest",
    "machine_state_before": {...},
    "machine_state_after": {...},
    "error_detail": null | {...},
}
```

同时：
- 结构化日志（JSON lines）到 stdout
- 关键事件通过 pub/sub broadcast（给实时 UI 和 Agent 的长连接）

**为什么**：
- Agent 做错了决策，能复盘
- 调试"上周三那次实验失败了"能追溯
- 将来 fine-tune Agent 时这就是训练数据
- 可验证性：新手用户相信系统，是因为"每一步都有记录"

### 原则 7：Dry-run 支持

破坏性操作必须支持 `dry_run=True` 模式：

```python
def start_spin_recipe(
    self,
    recipe: SpinRecipe,
    dry_run: bool = False,
) -> SpinPlan | SpinRunHandle:
    """
    dry_run=True: 返回 SpinPlan（每个 step 的 gcode + 时间 + 预期坐标），不发任何命令
    dry_run=False: 真的执行
    """
```

Agent 可以先 dry_run 把 recipe 给人审批，批准后再真执行。也方便 Agent 自己在决策前做 "what-if" 推理。

## 接口示例：GantryBackend 完整草案

给出一个完整样例（其他 backend 照此扩展），后续 Phase 3 实现时对照：

```python
# src/hardware/gantry_backend.py
from pydantic import BaseModel, Field
from typing import Literal, Optional
from enum import Enum

class MachineState(str, Enum):
    IDLE = "idle"
    RUN = "run"
    JOG = "jog"
    HOME = "home"
    ALARM = "alarm"
    HOLD = "hold"
    DISCONNECTED = "disconnected"
    UNKNOWN = "unknown"

class Position(BaseModel):
    x_mm: float
    y_mm: float
    z_mm: float
    
    # 机器坐标系说明：归零后原点在 +极限方向，
    # 所有合法位置 x_mm/y_mm ∈ [-280, 0]，z_mm ∈ [-95, 0]

class MachineStatus(BaseModel):
    state: MachineState
    position: Position
    alarm_code: Optional[int] = None
    alarm_message: Optional[str] = None
    is_homed: bool
    planner_buffer_free: int  # 0-35
    rx_buffer_free: int       # 0-255
    last_update_ms_ago: float

class MoveResult(BaseModel):
    success: bool
    final_position: Position
    duration_ms: float
    event_id: str

class HomeResult(BaseModel):
    success: bool
    position_after_pulloff: Position
    duration_ms: float
    event_id: str

class GantryBackend:
    """抽象接口，抄 pylabrobot SCARABackend 签名。
    具体实现（GrblDirectBackend / OpenBuildsBackend）见 ADR-002 选型结果。
    """
    
    # ── State queries（只读）──
    
    def get_status(self) -> MachineStatus: ...
    def get_position(self) -> Position: ...
    def is_connected(self) -> bool: ...
    def is_homed(self) -> bool: ...
    
    # ── Safe operations ──
    
    def home(self, *, idempotency_key: str) -> HomeResult: ...
    """XYZ 归零。Z 先归（向 +），然后 X+Y 并行。
    
    自动处理 Z 刹车时序（前释放、后锁回）。
    如果机器已在 homed 且最近 10 分钟内归过，返回缓存结果（idempotent）。
    
    Raises:
        AlarmStateError: grbl 当前 alarm（先 unlock 或 reset）
        BrakeError: Z 刹车继电器响应异常
        HomingTimeoutError: 90s 未完成
    """
    
    def move_to(
        self,
        target: Position,
        *,
        feed_mm_min: float = 2000,
        wait_for_idle: bool = True,
        timeout_s: float = 60,
    ) -> MoveResult: ...
    
    # （2026-04-22 修正：jog(axis, distance_mm) 方法删除 —— `move_to` 内部已
    # 改用 `$J=G90` jog 命令实现，外观接口保持"绝对坐标"。相对运动 Agent/UI
    # 自行计算 current + delta 再调 move_to；少一个方法降低 Agent tool schema
    # 复杂度，且 move_to 天然幂等 (§原则 3) 而 jog 非幂等，合并更合理。详见
    # Slice 5 讨论 / verification/slice-5-recovery.md。）
    
    # ── Control ──
    
    def halt(self) -> None: ...
    """立刻停止（feedhold + cancel jog）。幂等。"""
    
    def unlock_alarm(self) -> MachineStatus: ...
    """发 $X 清 alarm。返回清完后的状态。幂等。"""
    
    def soft_reset(self) -> None: ...
    """发 ctrl-X（0x18）。重置 grbl 但保留 EEPROM 参数。幂等。"""
    
    # ── Low-level escape hatch ──
    
    def send_raw_gcode(
        self,
        gcode: str,
        *,
        require_confirmation: bool = True,
    ) -> str:
        """绕过类型校验直接发命令。仅供调试。
        require_confirmation=True 时：人类必须在 CLI 确认，Agent 无法绕过。"""
```

**几个设计细节说明**：

1. **kwargs-only 参数**（`*,` 之后）：防止 Agent 通过位置参数调错，强制 named。
2. **`unlock_alarm` 返回 `MachineStatus`**：Agent 调完立刻能看到结果，不用再查一次。
3. **`send_raw_gcode` 带 `require_confirmation` 兜底**：留给人类，Agent 默认无法使用，防止 Agent "灵机一动"发乱命令。
4. **所有可能阻塞的方法都有 `timeout_s`**：API 永远不会"永久 hang"——Agent 的最大敌人。

## 可观测性基础设施（必须）

L3 启动时初始化：

```python
# src/runlog.py
class RunLog:
    def __init__(self, db_path: Path): ...
    def record(self, event: Event): ...
    def query(self, filter: ...) -> list[Event]: ...

# src/event_bus.py
class EventBus:
    """内存 pub/sub，用于实时订阅
    - Streamlit 实时 dashboard
    - Agent 的长连接（SSE/WebSocket）
    """
    def subscribe(self, event_types: list[str]) -> Iterator[Event]: ...
    def publish(self, event: Event): ...

# src/logging.py
# JSON Lines format
# stdout → journalctl (Pi) 或 terminal (Mac)
```

所有 backend 方法装饰：

```python
@observable  # 自动 record to runlog + publish to bus + structured log
def move_to(self, target: Position, ...) -> MoveResult:
    ...
```

## 测试标准

L3 代码 merge 前必须通过：

1. **Type 检查**：`mypy --strict src/` 全绿
2. **Schema 一致性**：用 `pydantic` 能 export 所有公共方法的 JSON schema，人工审过"Agent 看懂了"
3. **错误覆盖**：每个方法的所有抛错路径都有 unit test，且错误消息里包含 `suggested_action`
4. **幂等测试**：同 `idempotency_key` 连调 3 次，结果等价（runlog 里只有 1 条新事件）
5. **Dry-run 隔离**：`dry_run=True` 运行 100 次，断言"没向硬件发任何字节"
6. **状态查询不阻塞**：`get_status` 在机器执行 30s 长运动时可以连续调 100 次，每次 < 50ms 响应

## 参考规范

- [pydantic](https://docs.pydantic.dev/)：Python 类型校验标准
- [pylabrobot.arms.backend](https://github.com/PyLabRobot/pylabrobot/blob/main/pylabrobot/arms/backend.py)：backend 抽象参考
- [PASCAL frgpascal/hardware/](https://github.com/fenning-research-group/PASCAL/tree/main/frgpascal/hardware)：文件组织参考
- [Anthropic MCP 规范](https://modelcontextprotocol.io/)：给 LLM 暴露工具的标准——L3 API 应该能 1:1 映射成 MCP tools

## 回滚 / 演进条件

**什么时候放宽这些规范**：
- 某条规则明显拖慢开发超过 30% 且实证证明没带来 Agent 可靠性提升
- Agent 技术本身演化到不再需要某一项（例如模型自己能处理 dict 输入比 pydantic 更好）

**当前不认为有任何条可以放宽**。

## 执行计划

Phase 3 开工前：
- [ ] 建 `src/` 目录结构（见 migration-checklist Phase 3）
- [ ] 先把 `GantryBackend` 抽象接口按本文档写出来（纯接口，无实现）
- [ ] 写 `Event / RunLog / EventBus` 基础设施
- [ ] 写第一个 backend 方法（`home`）和它的完整测试集——这是模板
- [ ] 后续所有 backend 方法按这个模板扩展

Phase 3 验收标准（本文档新增）：
- 所有已实现方法的 pydantic schema 可 export
- mypy --strict 全绿
- 至少一条端到端 trace（从 Agent tool call → L3 method → 硬件 → runlog → Agent 看到结果）成功
