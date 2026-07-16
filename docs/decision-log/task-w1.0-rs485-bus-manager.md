# 任务卡 W1.0：Rs485Bus 共享总线管理器

**发卡**：2026-07-16 · **执行者**：Codex（无会话记忆，本卡自包含）· **前置**：W0-R 不阻塞本卡
**设计已由架构会话定稿，按本卡实现，不重新设计。**

## 背景（一段话）

一条 USB→RS485 半双工总线上挂多台设备（移液枪 / 旋涂电机 / 加热台，波特率可能不同）。
历史教训（测试版仓库审计 2026-07-16）：①按端口字符串字面判断是否共享，
`/dev/autospin_rs485` 与 `/dev/ttyUSB0` 混写即判"不共享"，多个 Serial 实例裸奔一条总线必串包；
②每次调用 close/reopen 串口，轮询期每秒约 20 次开关，是 USB 抖动源。本模块一次性解决。

## 硬边界

1. **禁止连接真实硬件**，测试全部用 fake serial。
2. 只许新增 `src/hardware/rs485_bus.py` + `tests/test_rs485_bus.py`；**禁改**其它 `src/` 文件
   （若发现必须改别处才能落地，停下在卡末尾写"发现"，不要自行扩权）。
3. 开始/结束都跑 `scripts/check.sh`，全绿才算完。

## 设计（定稿）

```python
class Rs485Bus:
    """一条物理 RS485 总线 = 一个常驻 pyserial 实例 + 一把事务锁。"""
    def __init__(self, port: str) -> None: ...
        # port 立即 os.path.realpath() 归一；保存归一后路径
    def connect(self) -> None: ...   # 打开一次，常驻；重复调用幂等
    def close(self) -> None: ...
    @contextmanager
    def transaction(self, device: str, baudrate: int) -> Iterator[serial.Serial]: ...
        # 拿锁 → 若 baudrate 与当前不同则切换 → yield 串口 → finally 释放锁
        # docstring 必须写明：事务 = 一次"发令+收回应"，禁止在事务内做长轮询；
        # 轮询循环应每次迭代单独开 transaction

_BUSES: dict[str, Rs485Bus] = {}
def get_bus(port: str) -> Rs485Bus:
    # realpath 归一后查表——两个不同写法的同一物理口必须拿到同一个 Rs485Bus 实例
```

- 错误：串口打不开/中途断开 → 抛本仓库 `src/hardware/errors.py` 的 `L3ConnectionError`
  （human_message 中文 + agent_message 英文，模仿 `gantry_backend.py` 现有用法）。
- 类型：mypy --strict 零错误；pyserial 无 stubs 时按仓库现有做法处理（参考 gantry_backend）。
- 不做：优先级/抢占、异步、跨进程锁（单进程假设，docstring 写明）。

## 测试要求（fake serial，参考 tests/test_gantry_backend_unit.py 的 fixture 风格）

1. realpath 归一：tmp 目录建符号链接指向同一目标，两个路径 `get_bus()` 返回同一实例。
2. 事务互斥：两线程各做 50 次 transaction（内写标记字节序列），断言无交错。
3. 波特率切换：两设备不同 baud 交替事务，断言切换次数最小化（相同 baud 不重设）。
4. 常驻连接：连续 100 次 transaction，底层 open() 只发生 1 次。
5. 异常释放锁：事务内抛异常后，下一个事务能立即拿到锁。

## 验收

- [ ] `scripts/check.sh` 全绿（新测试并入）
- [ ] 上述 5 条测试存在且通过
- [ ] 无真实串口路径出现在测试中

## 回滚

纯新增文件，`git rm` 即回滚。
