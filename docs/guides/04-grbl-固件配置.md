# 04 - grbl-Mega-5X 固件配置

> 项目当前固件栈：[grbl-Mega-5X](https://github.com/fra589/grbl-Mega-5X) commit `a5596ef`（2024-10-20）
> 自研固件已退役并归档到 [`docs/legacy/`](../legacy/)，背景与决策见 [ADR-001](../decision-log/ADR-001-migrate-to-grbl-stack.md)
> Phase 1 spike 验收通过日期：2026-04-13

---

## 1. 仓库与文件位置

| 路径 | 用途 | 是否入 git |
|---|---|---|
| `firmware/grbl_spike/grbl-Mega-5X/` | 上游 clone（嵌套 git repo） | ❌（已加入 `.gitignore`） |
| `firmware/grbl_spike/our_config/cpu_map.h.orig` | 上游基线副本 | ✅ |
| `firmware/grbl_spike/our_config/config.h.orig` | 上游基线副本 | ✅ |
| `firmware/grbl_spike/our_config/cpu_map.h` | 我们改后的副本 | ✅ |
| `firmware/grbl_spike/our_config/config.h` | 我们改后的副本 | ✅ |

**重要**：上游仓库本身不入 git，所有改动通过 `our_config/` 副本追踪。下次 `git clone` 上游后，把 `our_config/` 的 `cpu_map.h` 和 `config.h` 复制覆盖到 `grbl-Mega-5X/grbl/` 即可恢复。

## 2. cpu_map.h 引脚映射改动

默认 `CPU_MAP_2560_RAMPS_BOARD` 段的 RAMPS 引脚布局**完全不适用我们的接线**。所有改动列表如下，bit 编号对应 ATmega2560 各 PORT。

### 2.1 STEP / DIR 引脚

| 信号 | 板上 D-pin | PORT | BIT | 旧值（RAMPS） | 备注 |
|---|---|---|---|---|---|
| X STEP | D2 | E | 4 | F0 (D54) | |
| X DIR | D3 | E | 5 | F1 (D55) | |
| Y STEP | D4 | G | 5 | F6 (D60) | ⚠ |
| Y DIR | D5 | E | 3 | F7 (D61) | ⚠ |
| Z STEP | D6 | H | 3 | L3 (D46) | |
| Z DIR | D7 | H | 4 | L1 (D48) | |

⚠ Y 引脚同时是 RAMPS 默认的 spindle pin（`SPINDLE_ENABLE_BIT=PG5/D4`、`SPINDLE_DIRECTION_BIT=PE3/D5`），必须把 spindle pin 搬走（见下）。

### 2.2 限位引脚（MIN_LIMIT = - 限位，MAX_LIMIT = + 限位 = 用作 homing 触发）

| 轴 | 信号 | D-pin | PORT | BIT |
|---|---|---|---|---|
| X | X- 限位 | D22 | A | 0 |
| X | X+ 限位 | D24 | A | 2 |
| Y | Y- 限位 | D25 | A | 3 |
| Y | Y+ 限位 | D27 | A | 5 |
| Z | Z- 限位 | D28 | A | 6 |
| Z | Z+ 限位 | D30 | C | 7 |

**关键约束**：上述 PORT A / PORT C 在 ATmega2560 上**没有 PCINT 中断**支持（PCINT 只在 PORTB/E/J/K）。因此**必须开启 `ENABLE_RAMPS_HW_LIMITS`**——这会让 grbl 改成在 stepper ISR 内轮询限位 pin，牺牲一点最大 step rate 换取兼容性。

```c
// cpu_map.h, line ~211
#define ENABLE_RAMPS_HW_LIMITS  // 智能旋涂仪: 限位在 PORTA/C（无 PCINT），必须开
```

### 2.3 SPINDLE 引脚搬迁（避开 D4/D5 撞 Y）

```c
#define SPINDLE_ENABLE_PORT     PORTG
#define SPINDLE_ENABLE_BIT      1   // D40 (PG1) — 我们没有 spindle 硬件，纯占位
#define SPINDLE_DIRECTION_PORT  PORTG
#define SPINDLE_DIRECTION_BIT   0   // D41 (PG0) — 同上
```

**还要在 `config.h` 把 spindle PWM 输出从 D8 (Timer4) 搬到 D9 (Timer2)**，避开 Z STEP D6=PH3=OC4A 同属 Timer4 的潜在干扰：

```c
// config.h
//#define SPINDLE_PWM_ON_D8
//#define SPINDLE_PWM_ON_D6
#define SPINDLE_PWM_ON_D9   // 智能旋涂仪: Timer2，避开 Timer4 与 Z STEP 冲突
```

### 2.4 未做改动的部分

- **STEPPER_DISABLE 引脚（ENA）**：保留默认值（D38/A2/A8）。我们的 2HSS57-C 驱动器 ENA 物理上未接，驱动器自启用。grbl 会写这几个 pin 但物理上无连接。
- **CONTROL 引脚**（Reset/Hold/CycleStart/SafetyDoor）：保留默认 PORTK，我们没有物理急停按键接到这些引脚。
- **PROBE 引脚**：保留默认。Phase 3 可能用 probe + diode-OR 接中间原点传感器实现两段式归零（见已知问题 §6）。
- **ALM 报警引脚 D31/D32/D33**：物理已接（Phase 0 完成），但**未接入 grbl**。Phase 3 由 Python orchestrator 轮询。

## 3. config.h 改动

```c
#define N_AXIS 3        // 默认 5 改 3
#define N_AXIS_LINEAR 3 // 默认就是 3，未动
```

加上前面 §2.3 的 `SPINDLE_PWM_ON_D9`。

## 4. `$` 参数表（已写入 EEPROM，永久持久化）

```
$0=10            脉冲宽度 us（默认 10 即可，太大撞最大 step rate）
$1=255           idle hold 时间 = 永久通电（电机一直锁住，Z 刹车释放也不会掉）
$2=0             step 端口反相掩码
$3=6             方向反相掩码：bit1=Y bit2=Z 都取反，X 不取反
$4=0             enable 反相
$5=1             ⚠ 限位 pin 反相：DS-ES61 NPN 亮通是"无遮挡=LOW，遮挡=HIGH=触发"
$10=2            状态报告 mask（保留 Bf/FS，便于 backend 判断是否真 Idle）
$6=0             probe 反相
$10=2            状态报告 mask（包含 limit pin 状态）
$20=1            软限位
$21=1            硬限位
$22=1            归零启用
$23=0            ⚠ 归零方向"反转"掩码：0 = 全部 + 方向归零（默认值就是我们要的，不要乱改）
$24=25           归零慢速触发速度 mm/min
$25=500          归零快速搜索速度 mm/min
$26=250          去抖时间 ms
$27=5            ⚠ 归零退避距离 mm（默认 1 太小，DS-ES61 槽宽要求 ≥5；可能还要更大，见 §6）
$30=12000        spindle 最大速度（无意义，我们没 spindle）
$31=550          spindle 最小速度
$32=0            laser 模式
$100=682.670     X steps/mm（导程 75，51200 步/转）
$101=682.670     Y steps/mm
$102=682.670     Z steps/mm
$110=3000        X 最大速度 mm/min（约 50 mm/s，理论上限 ~58 mm/s by 40 kHz step ISR）
$111=3000        Y 最大速度
$112=3000        Z 最大速度
$120=200         X 加速度 mm/s²
$121=200         Y
$122=200         Z
$130=280         X 最大行程 mm
$131=280         Y 最大行程
$132=95          Z 最大行程
```

### 4.1 几条容易踩的坑

- **`$3` 是反相掩码**，不是方向掩码。bit=1 表示**那一根轴的 DIR 信号取反**。`$3=0` 不代表"全部默认方向"，而是**所有 DIR pin 都不反相**。这次配置下 Y 和 Z 物理上需要反相，X 不需要——和自研固件**完全相反**。
- **`$23` 也是反相掩码**，不是"方向掩码"。bit=1 表示**那一根轴 home 时朝 - 方向**。grbl 默认朝 + 方向归零（用 MAX_LIMIT pin），所以**全部 + 归零应该是 `$23=0`**——不要被 grbl 老 wiki 上的"setting bit X to 1 causes Grbl to look for the limit switch in the negative direction"误导。
- **`$5=1` 不能省**。DS-ES61 是 NPN 亮通（光通过 → 输出 LOW，遮挡 → 输出 HIGH），刚好与 grbl 默认 `$5=0`（LOW = 触发）相反。不设的话开机就 `Pn:XYZ`。
- **`$24=25` / `$25=500` 是当前可靠归零参数，不要随手提速**。2026-04-24 现场复现：`$25=1500` 时 `$H` 约 8s 后稳定 `ALARM:9`；人工遮挡 Z/X/Y 传感器都能看到 `Pn:Z/X/Y`，说明传感器链路通，问题是归零搜索速度太激进。降回 `$24=25`、`$25=500` 后同一台机器 `$H` 成功回到 `MPos:-4.999,-4.999,-4.999`。
- **`$0=10` / `$4=0` 也必须守住**。2026-04-24 现场再次复现：`$0` 漂到 `130` 时，`grbl` 仍会报 `ok` 和目标坐标到位，但驱动器的 step pulse 宽度已经大到不再可靠，真实机械会明显少走或不走；`$4=1` 会把 enable 极性带歪，进一步放大失配风险。启动前应把这两项和 `$100/$101/$102` 一并校验。
- **`$27=5` 还可能不够**。我们撞 + 限位归零、退 5 mm，用 `$H` 验收时静止瞬间 `Pn` 干净；但运动过的几次，挡片在槽边缘 5 mm 处 `Pn` 仍然偶发触发，进 alarm。如果再现，把 `$27` 加到 10 或 15 重试。
- **`$130/$131=280`** 是基于 GH40 D75-X300S-Y300S 的 300 mm 行程留 20 mm 余量。如果未来发现实际行程不到 280 会撞极限，就降到实测值减 5 mm。

## 5. 烧录流程

```bash
cd firmware/grbl_spike/grbl-Mega-5X
arduino-cli compile -u -p /dev/cu.wchusbserial110 \
  --fqbn arduino:avr:mega:cpu=atmega2560 \
  --library grbl \
  grbl/examples/grblUpload
```

烧录前**必须关闭**所有占用串口的程序（旧 web 控制面板、cncjs、Arduino IDE 串口监视器）。

烧录后大小应该是 ~33 KB flash (13%) + ~3.5 KB RAM (42%)。

固件烧录后所有 `$` 参数会**保留在 EEPROM**，下次开机不丢。但**如果你 reflash 的 grbl 版本和 EEPROM 里旧版本不兼容**，需要手动 `$RST=*` 恢复出厂默认再重新写入参数。

## 6. 已知问题 / 故障排查

### 6.1 限位传感器 5V 接线松动会导致 `Pn:XYZ` 误触发

**症状**：grbl 静止时 `?` 显示 `Pn:XYZ`（三个轴的限位都被认为触发）；任何 jog 立刻硬限位 alarm；Ctrl-X 软复位后 grbl 启动报 `[MSG:Check Limits]`。

**根因**：9 个 DS-ES61 共用 Arduino 的 5V 母线。任何一根 +5V 或 GND 杜邦线接触不良 → 输出口浮空 → 内部上拉/外部状态读到 HIGH → 配合 `$5=1` 反相 → grbl 看到"触发"。**所有传感器同时闪 LED** 是这个问题的典型征象。

**排查**：
1. 看 9 个 DS-ES61 的 LED 是不是同时闪烁（同时闪 = 共用电源问题；个别闪 = 单根线问题）
2. 按一遍 5V 母线和 GND 母线的所有连接点
3. 万用表测 Arduino 5V pin 到 GND，应在 4.95 ~ 5.05 V
4. 用 `$10=2` 把 limit pin 状态加进 `?` 报告，背景 polling 监控

**长期方案**：把 5V/GND 杜邦改成压接端子或焊接，加去耦电容到每个传感器电源端。

### 6.2 `$H` 完成后 `Pn` 持触 → 后续运动立刻硬限位 alarm

**症状**：第一次 `$H` 成功，状态 `Idle WPos:-1,-1,-1`，但同时 `Pn:XYZ`。任何下一条移动命令立刻触发 hard limit alarm。

**根因**：`$27=1` 默认退避距离对 DS-ES61 槽宽不够。挡片只退了 1 mm 还插在槽里，传感器一直输出"被遮挡"。

**修复**：`$27=5` 是最低值；可能需要 10 或 15 mm。

**警告**：如果 Z 已经在顶部附近，加大 `$27` 然后 `$H` **不会让 Z 撞顶**（已经在顶了，撞限位后就退避），但**会让 Z 在 + 方向"立刻撞顶"**——视觉上就像"弹射"。这是因为 Z 物理上离顶只有 1 mm，加速度 `$120=200` 让它瞬间到达顶。Z 接近顶时跑 `$H` 一定要心里有数。

### 6.3 Ctrl-X 软复位后 grbl 内部坐标可能变 0,0,0

grbl-Mega-5X 在某些条件下软复位会清位置（不同版本和不同前置状态行为不一样）。**软复位后 grbl 的坐标可能和物理位置脱钩**。后续动作需要重新 `$H` 才能可靠依赖 MPos。

恢复期间用 `$J=G91` 相对 jog 是安全的——grbl 的 DIR 取反（`$3`）配置是 EEPROM 持久化的，物理方向感不会变。

### 6.4 `$J` jog 收到 `!` 是"取消"，不是"暂停"

grbl 1.1+ 规定：`$J` 在执行中收到 feedhold（`!`），动作是**减速并丢弃剩余 jog**，state 直接回 `Idle`，**`~` 没东西可恢复**。

如果你需要"真 hold + resume"语义，必须用 G-code 移动指令（`G0` / `G1`）而不是 `$J`，才能进 `Hold:0` 状态。

### 6.5 spindle PWM 不要用 D8

D8 是 OC4C，和我们的 Z STEP D6 = OC4A 同属 Timer4。即使我们不接 spindle，启用 `SPINDLE_PWM_ON_D8` 会让 grbl 配置 Timer4 为 PWM 模式，**理论上**不影响 OC4A 的 GPIO 功能（COM bits 不会被置位），但**为了零风险**已切到 `SPINDLE_PWM_ON_D9`（OC2B / Timer2）。

### 6.6 `$H` 很快失败并报 `ALARM:9`

**症状**：`$H` 发出后不是 90s 超时，而是几秒到十几秒内返回 `ALARM:9`。2026-04-24 现场记录里，`$25=1500` 时约 8.2s 失败；状态停在 `Alarm`，位置靠近 `MPos:3.937,3.937,-4.999` 或归零中间态。

**先区分传感器故障和速度问题**：

1. 跑只读轮询：`tools/spikes/.venv/bin/python tools/grbl_stability_test.py health --duration 30`
2. 人工依次遮挡 Z+ / X+ / Y+，每个保持约 3s
3. 终端应出现 `PIN Pn change: '' -> 'Z'`、`X`、`Y`

若人工遮挡都没有 `Pn`，先查传感器供电、信号线和 grbl pin 映射。若人工遮挡有 `Pn`，但自动 `$H` 仍 `ALARM:9`，优先检查 `$24/$25` 是否被写成激进值。

**当前可靠修复**：

```gcode
$X
$24=25.000
$25=500.000
```

然后把 X/Y 退离 +端 20mm 左右，再重试 `$H`。2026-04-24 复测：上述参数下 `$H` 成功，最终 `<Idle|MPos:-4.999,-4.999,-4.999|...>`。

### 6.7 USB 串口偶尔会断（`Device not configured`）

**症状**：脚本运行中突然报 `OSError: [Errno 6] Device not configured`，`/dev/cu.wchusbserial110` 暂时消失。

**怀疑因素**：刹车继电器开关 + 电机大电流切换产生的 EMI 反扑 USB 控制器，macOS 防御性把 USB 子系统重置。

**临时缓解**：把刹车继电器操作和 grbl 串口操作**分到不同的 Python 进程/连接**，中间隔 0.5 秒空白。本项目 Python 脚本里采用这个模式：每个阶段 open → 用 → close，不复用同一个 serial 实例。

**长期方案**：传感器/继电器线远离电机线，加 USB 隔离器，或换更高质量的 USB 线。

### 6.7 `$23` 设错方向的事故

**事故**：第一次 `$H` 设 `$23=7`（误以为 = "全部 + 方向"），实际让所有轴朝 - 方向归零。Z 撞 Z- 底限位（不严重，撞速度 500 mm/min），X 因为初始位置不利在 locate phase 失败 → ALARM:9。

**教训**：`$23` 是反相掩码不是方向掩码。**默认方向（+ 归零）就是 `$23=0`**，要往 - 方向归零某根轴才置位对应 bit。

## 7. Phase 1 验收结果（2026-04-13）

| # | 验收项 | 结果 |
|---|---|---|
| 1 | `$H` 三轴归零 | ✅ 第一次 `$H` 成功，但 `$27` 退避距离仍待调优（见 §6.2） |
| 2 | `G0 X100` 点动 | ✅ `$J` 等价验证 |
| 3 | `G1 X50 Y50 F500` 协同运动 | ✅ X/Y 严格同步（每个采样点 ΔX = ΔY） |
| 4 | 手动触发限位 alarm | ✅ 6 个限位 + 3 个未接的中间原点全部行为符合预期 |
| 5 | `!` `~` `Ctrl-X` 软件控制 | ✅ G1 motion 真 feedhold + resume + soft reset 三件套都行 |
| 6 | 尺子量 10 mm 精度 | ⏸ 现场无尺子，沿用 2026-03-26 自研固件下校准的 682.67 步/mm |
| 7 | 5 分钟连续无丢步 | ✅ 4 分钟 / 20 cycles / ~2.84 m / 起终点漂移 = (0, 0, 0) |

**结论**：grbl-Mega-5X 路线**spike 验证通过**，Phase 1 决策点 = 继续迁移，**不回滚到自研 2c**。

## 8. 与其它系统的边界

- **Z 刹车（DSTUR-T80 CH2）**：grbl **不知道**刹车存在。运动 Z 之前必须由上位机先释放刹车（CH2 ON），运动结束后锁回（CH2 OFF）。`$1=255` 让电机常通电，刹车释放期间 Z 不会掉。当前由 Python 脚本协调；Phase 3 移到 orchestrator 层。
- **ALM 报警 D31/D32/D33**：grbl 不读，物理已接（Phase 0 完成）。Phase 3 由 orchestrator 轮询。
- **3 个中间原点传感器 D23/D26/D29**：grbl 不读。Phase 3 计划用 probe + diode-OR 实现"撞 + 限位 → 反向找中间原点"两段式归零（见 ADR-001）。
- **9 路传感器 5V 供电**：见 §6.1，物理可靠性是当前已知薄弱点。

## 9. 参考

- 上游 grbl-Mega-5X README：`firmware/grbl_spike/grbl-Mega-5X/README.md`
- ADR-001：[`docs/decision-log/ADR-001-migrate-to-grbl-stack.md`](../decision-log/ADR-001-migrate-to-grbl-stack.md)
- 迁移 checklist：[`docs/decision-log/migration-checklist.md`](../decision-log/migration-checklist.md)
- 旧自研固件文档（仅供回滚参考）：[`docs/legacy/04-自研固件开发-archived.md`](../legacy/04-自研固件开发-archived.md), [`docs/legacy/05-自研固件测试-archived.md`](../legacy/05-自研固件测试-archived.md)
- 驱动器手册（ALM/PEND/PUL/DIR 电气特性）：`hardware/龙门架/驱动器/2HSS57-C (V2.01-T) (1).pdf`
