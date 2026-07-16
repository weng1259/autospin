# 智能旋涂仪

> **当前架构（2026-07-16）**：Arduino Mega 2560 运行
> **grbl-Mega-5X**；产品核心是 `src/` 中的 **L3 Python orchestrator**，通过
> DIY `pyserial` 适配器直连 grbl，并以类型化 API、结构化错误、安全边界和
> runlog 对外提供能力；`tools/ui/emergency_dashboard.py` 是 **Streamlit
> 应急/监控面板**。Agent/API 是长期主入口，UI 不是产品中心。
>
> cncjs、自研 `motor_control` 固件和旧 WebSerial 网页面板均为历史或回滚资产，
> **不是当前控制栈**；见下文“历史沿革”。

这是以 GH40 XYZ 龙门架为底座的智能实验仪器项目。旋涂仪是第一台 proof of
concept，长期目标是让 LLM Agent 通过安全、可审计的 L3 工具控制实验室设备。

## 新会话必读

先遵循仓库根目录的 [`AGENTS.md`](AGENTS.md)，再按以下顺序建立上下文：

1. [`docs/decision-log/architecture-roadmap.md`](docs/decision-log/architecture-roadmap.md) — 目标架构与阶段路线
2. [`docs/decision-log/ADR-003-agent-first-vision.md`](docs/decision-log/ADR-003-agent-first-vision.md) — Agent-first 产品方向
3. [`docs/decision-log/ADR-004-l3-api-design-principles.md`](docs/decision-log/ADR-004-l3-api-design-principles.md) — L3 API 强制规范
4. [`docs/decision-log/ADR-005-claude-agent-sdk-for-l3.md`](docs/decision-log/ADR-005-claude-agent-sdk-for-l3.md) — Agent SDK 与工具白名单决策
5. [`docs/decision-log/web-control-catchup-plan.md`](docs/decision-log/web-control-catchup-plan.md) — 2026-07-16 起的 W0–W6 当前施工计划
6. [`docs/decision-log/migration-checklist.md`](docs/decision-log/migration-checklist.md) — grbl/L3 迁移的详细阶段记录

历史上下文需要核对时，再看
[`docs/claude-memory-migration.md`](docs/claude-memory-migration.md) 和
[`docs/claude-code-migration-audit.md`](docs/claude-code-migration-audit.md)。

## 当前架构

```text
自然语言 / CLI                 Streamlit 应急与监控
       │                              │
       └──────────────┬───────────────┘
                      ▼
          L3 Python orchestrator（src/）
          typed API / L3Error / dry-run
          idempotency / runlog / event bus
                      │
        ┌─────────────┼──────────────────┐
        ▼             ▼                  ▼
  grbl-Mega-5X   DSTUR-T80 继电器      RS485 外设
  XYZ 龙门架       夹爪 / Z 刹车       旋涂/移液/加热…
```

| 层 | 当前实现 | 定位 |
|---|---|---|
| L1 固件 | Arduino Mega 2560 + grbl-Mega-5X | XYZ 运动、归零、限位与 grbl 状态机 |
| L3 产品核心 | `src/hardware/` + `src/routine.py` | 直连 `pyserial`；类型化设备 API 与编排 |
| 可观测性 | `src/runlog.py`、`src/event_bus.py`、`src/observable.py` | SQLite 事件、内存事件流、调用审计 |
| L5 应急 UI | `tools/ui/emergency_dashboard.py` | 状态、应急操作和 PM 验收；不是主产品入口 |
| L6 Agent | Claude Agent SDK 已做 CLI smoke；正式工具层仍按路线图推进 | 只暴露设备语义级工具，不暴露 raw gcode/relay/串口写入 |

L3 是产品本体。UI、Agent 和调试 CLI 都应消费同一套后端能力，安全检查必须在
backend 内成立，不能只依赖前端按钮或 Agent hook。

## 当前状态

截至 2026-07-16：

- grbl-Mega-5X 迁移和 L3 的 Gantry/Relay/Gripper backend 已完成；Z 轴运动由
  backend 负责先释放 CH2 刹车、运动后再锁回。
- schema 合同、`mypy --strict`、结构化错误、幂等、dry-run、并发状态查询和
  runlog 基础设施已经落地。
- `src/routine.py` 提供示教/重放编排 v1；Streamlit 应急面板用于当前人工验收。
- W0 开工时的已提交软件基线为 `154 passed`、mypy strict 0、
  `docs/api-v1.json` diff 为空；统一入口是 `scripts/check.sh`。
- 当前工作是 W0 底座收口；其后依次补齐 RS485 总线与加热/旋涂/移液/滑台
  backend、系统级急停、Web 控制服务和 recipe 编排。以
  [`web-control-catchup-plan.md`](docs/decision-log/web-control-catchup-plan.md)
  为当前排期来源。

## 工程入口

`scripts/check.sh` 固定使用仓库根目录的 `.venv`。当前合同基线使用 Python 3.13；
Python 3.14 会改变部分 `typing` 反射字符串，不应拿来重写 API 合同。

```bash
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt PyYAML==6.0.3
scripts/check.sh
```

该脚本依次执行：

1. `.venv/bin/python -m pytest tests -q`
2. `.venv/bin/python -m mypy --strict src`
3. 重新导出 schema 到临时文件并与 `docs/api-v1.json` 比较

每个软件任务开始前和提交前都应全绿。真机 smoke、串口诊断和固件上传不属于
普通软件回归；只有任务明确要求且确认端口、占用进程与安全条件后才可执行。

## 目录结构

```text
智能旋涂仪/
├── src/                         # L3 产品核心
│   ├── hardware/                # gantry / relay / gripper backend + 类型/错误
│   ├── routine.py               # 示教与重放编排 v1
│   ├── runlog.py                # SQLite 事件记录
│   ├── event_bus.py             # 内存 pub/sub
│   ├── observable.py            # 可观测与幂等装饰器
│   └── schema_export.py         # docs/api-v1.json 合同导出
├── tests/                       # 纯软件/mock 回归测试
├── tools/
│   ├── ui/emergency_dashboard.py# Streamlit 应急/监控面板
│   └── spikes/                  # Agent/硬件 spike；不得当作普通测试自动运行
├── firmware/
│   ├── grbl_spike/our_config/   # 当前 grbl 配置的受控副本
│   └── motor_control/            # 已退役的自研固件回滚快照
├── docs/
│   ├── decision-log/            # ADR、路线图与任务卡
│   ├── verification/            # 简短、证据导向的验收记录
│   ├── guides/                  # 当前操作/硬件指南
│   └── legacy/                  # 已退役路线文档
├── hardware/                    # 手册、BOM、照片与机械资料
├── scripts/check.sh             # 统一软件绿基线
└── docs/api-v1.json             # L3 API v1 合同
```

## 硬件与安全速查

- 龙门架：GH40-D75-X300S-Y300S-Z100S-57，物理行程 X/Y/Z =
  300/300/100 mm。
- 驱动器：2HSS57-C，51200 pulses/rev，682.67 steps/mm；grbl `$3=6` 表示
  Y/Z 方向反相。
- 传感器：DS-ES61 NPN light-on；grbl 必须使用 `$5=1` 做限位输入反相。
- Z 刹车：DSTUR-T80 CH2；运动前释放，运动后锁回，断电保持锁定。
- L3 软限位必须比 grbl 物理行程至少内缩 `$27` pull-off 距离，不能直接照抄
  `$130/$131/$132`。
- 固件参数曾因 Mega EEPROM 不可靠而固化到 grbl 默认配置；持久参数变更必须先
  阅读手册、修改受控配置、编译上传并留下记录，不能靠运行时 `$xxx=yyy` 假定持久。

权威资料：

- [`docs/guides/04-grbl-固件配置.md`](docs/guides/04-grbl-固件配置.md) — grbl 引脚、参数和烧录
- [`docs/guides/02-电控接线.md`](docs/guides/02-电控接线.md) — 电控接线
- [`docs/hardware-specs.md`](docs/hardware-specs.md) — 硬件规格
- [`hardware/采购清单.md`](hardware/采购清单.md) — BOM 与接线参考
- [`docs/troubleshooting.md`](docs/troubleshooting.md) — 已知故障与排查

## Agent 工具边界

允许给 Agent 的是语义级工具，例如 `gantry_get_status`、`gantry_home`、
`gantry_move_to`、`gantry_halt`、`gripper_open`、`gripper_close` 和未来的
`spincoater_run_recipe`。

以下能力永不进入 Agent allowlist：raw gcode、raw relay channel、直接写
`/dev/cu.*`、以及修改 `constants.yaml` 安全边界。完整规则见
[`ADR-005`](docs/decision-log/ADR-005-claude-agent-sdk-for-l3.md)。

## 历史沿革

- **2026-03，自研阶段**：`firmware/motor_control/motor_control.ino` 与
  `web_control/*.html` 曾承担 XYZ 控制、归零、状态显示和 Z 刹车联动。它们验证了
  硬件接线与早期协议，但现已退役；固件只保留为 rollback snapshot，旧网页只作
  历史/调试参考。归档见
  [`docs/legacy/`](docs/legacy/) 和
  [`firmware/motor_control/README.md`](firmware/motor_control/README.md)。
- **2026-04，grbl 迁移**：项目迁到 grbl-Mega-5X，原因、验收和回滚条件见
  [`ADR-001`](docs/decision-log/ADR-001-migrate-to-grbl-stack.md)。
- **2026-04，cncjs 评估**：cncjs 1.11.0 曾作为 Phase 2 候选，但因 Feeder/ALARM
  行为和 API 信息损失未通过验收；Phase 2 已废弃。cncjs 仅可作为历史性的人工
  应急工具，不是产品依赖。详见
  [`ADR-002`](docs/decision-log/ADR-002-replace-cncjs.md)。
- **当前路线**：DIY `pyserial` L3 backend + Streamlit 应急面板；Agent 与未来 Web
  服务复用同一套 L3 API。

## 快速导航

| 目的 | 入口 |
|---|---|
| 看产品终态和阶段路线 | [`architecture-roadmap.md`](docs/decision-log/architecture-roadmap.md) |
| 看当前 W0–W6 施工顺序 | [`web-control-catchup-plan.md`](docs/decision-log/web-control-catchup-plan.md) |
| 看 L3 API 合同 | [`docs/api-v1.json`](docs/api-v1.json) |
| 看验证证据 | [`docs/verification/`](docs/verification/) |
| 查问题 | [`docs/issues/README.md`](docs/issues/README.md) |
| 查硬件接线与固件 | [`docs/guides/`](docs/guides/) |
| 查历史开发记录 | [`docs/devlog/`](docs/devlog/) |
