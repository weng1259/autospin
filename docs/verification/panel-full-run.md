# 面板全设备集成 —— PM 手动旋涂任务 验收记录

> 任务卡：[task-panel-full-device-integration.md](../decision-log/task-panel-full-device-integration.md)
> 宿主：Pi（`ssh pi-spin`，`~/autospin`）· 面板跑 Pi、浏览器 Tailscale `http://100.90.0.36:8501`
> 执行：2026-06-20 · Claude 增量改面板 + 跑非动作 smoke；**PM 现场动作 6 步另记（见 §4）**

## 结论

- **代码 + 架构 + 装载：✅ 已验证**（纯净增 423 行 0 删、Pi `py_compile` 过、`AppTest` 渲染 0 异常）。
- **真机接线（只读，非动作）：✅ 通过**——单一共享 relay + 单一共享 RS485 lock 在真机坐实，加热 PV / 旋涂状态都能在同一条 RS485 上读到，龙门同时在线。
- **PM 物理动作 6 步（夹取→旋涂→退火→放回）：⏳ 待 PM 现场点**（§4 清单，需人在急停旁）。

集成进面板的设备（除移液枪外全部）：🦾 夹爪(CH1) · 🔌 工艺通道(CH3-8) · 🔥 加热台(AI-516) · 🌀 旋涂电机(DBLS400)。移液枪按任务卡**完全不构造**。

---

## 1. 做了什么（只在 `tools/ui/emergency_dashboard.py` 增量）

- **git diff 纯净增**：`1 file changed, 423 insertions(+)`，0 删除、0 改写已有行。复用现有龙门 UI（jog/move/home/Z2/急停/恢复）完全没动。
- 新增 5 个面板区块：`🔗 全设备连接` / `🦾 夹爪(CH1)` / `🔌 工艺继电器通道(CH3-8)` / `🔥 加热台 AI-516` / `🌀 旋涂电机 DBLS400`。

**接线架构（照任务卡 §1，已真机坐实）**：

```
autospin_xyz  (CH340)  ── GantryBackend(干净L3, relay 注入)   [复用既有 UI]
autospin_relay(STM32)  ── RelayBackend(干净L3, 单一共享实例)
        ├─ 注入进 GantryBackend(relay=...) → CH2 Z刹车
        ├─ GripperBackend(relay=同一个) → CH1 夹爪 开/合
        └─ CH3-8 工艺通道 ch_on/ch_off（裸通道 + 可编辑标签）
autospin_rs485(CH340)  ── SharedRs485DeviceProxy ×2，共用 1 把 threading.Lock()
        ├─ HeatingStageController (师兄原始驱动, slave3)
        └─ MotorController        (师兄原始驱动, slave2)
        └─ (PipetteController 完全不构造)
```

连接逻辑放 `_connect_full_devices()`，实例存 `st.session_state` 跨 rerun 存活；relay/龙门同步连，加热/旋涂 **懒连**（首次读/写才开 RS485 口）。

**守住的硬规则（任务卡 §2）**：
- `rs485_lock` session 内**唯一一把**，两个 proxy 共用 → 同一时刻只占 RS485 口（真机 `heater._lock is spin._lock = True`）。
- 加热 PV **不进** `@st.fragment(run_every="0.1s")`（那是龙门专用、不同口）；PV 只在点按钮时读。
- relay **单实例**注入龙门，绝不 new 第二个（真机 `gantry._relay is relay = True`、`gripper._relay is relay = True`）。
- 旋涂**独立急停**按钮（`emergency_stop`=stop+抱闸），和龙门急停（feedhold，不停电机）分开；默认 100 RPM、上限保守夹到 500（见 §3 偏差）。

---

## 2. Agent 已验证证据（2026-06-20，Pi 真机）

### 2.1 装载 / 渲染（无硬件）
```
# 语法
Mac py_compile: OK   Pi .venv py_compile: OK
# git diff
tools/ui/emergency_dashboard.py | 423 +++++  (1 file changed, 423 insertions(+), 0 deletions)
# Streamlit AppTest（in-process 跑脚本，抓渲染期异常）
EXCEPTION: (none)
subheaders(15): 🔗 全设备连接 / 🎯 实时位置 / 机器状态 / 🏠 归零 / 🕹️ Jog / 🔽 Z2 /
                🎯 去这里 / 🦾 夹爪 / 🔌 工艺继电器通道 / 🔥 加热台 / 🌀 旋涂电机 / 📜 历史记录 / ...
RENDER OK
```

### 2.2 真机接线 smoke（`tools/panel_wiring_smoke.py`，**全只读、零动作**）

脚本逐字镜像 `_connect_full_devices()` 的构造逻辑，只跑读路径（不 toggle 继电器、不开合夹爪、不启旋涂、不写 SV）：

```
[relay ] connect OK  state={1: False, 2: False, 3: False, 4: False, 5: False, 6: False, 7: False, 8: False}
[gantry] connect OK  state=alarm pos=(0.0,0.0,0.0) homed=False
[arch  ] gantry._relay is relay : True          ← 单一共享 relay
[arch  ] gripper._relay is relay: True          ← 夹爪复用同一 relay
[gripper] commanded_state=unknown (read-only)
[arch  ] heater._lock is spin._lock: True        ← 单一共享 RS485 lock
[heater] read_pv OK  PV=33.1 C                   ← 加热台真机读到 PV（共享总线）
[spin  ] get_status OK  actual_speed=0 RPM status=unknown  ← 旋涂真机应答（同口 slave2）
=== SMOKE DONE ===
```

**意义**：三条串口（grbl / STM32 继电器 / CH340 RS485）+ 四个设备同时活在一个进程里，零端口冲突；加热(slave3)与旋涂(slave2)在**同一条 RS485** 上靠一把 lock 串行化、各读各的。龙门冷启进 `alarm`（`$22=1` 归零锁，正常、可恢复）。

### 2.3 面板在线
```
HTTP 200  @ http://100.90.0.36:8501   (Tailscale; 局域网 http://192.168.1.207:8501)
```

---

## 3. 已知偏差 / 留给 PM 现场敲定

- **旋涂上限夹到 500 RPM（而非任务卡示例的 2000）**：spinmotor bring-up 仅在 **100 RPM** 确认过卡盘平衡（`docs/verification/spinmotor-bringup.md`）。面板默认 100、上限 500，并红字提示「升速前先在 100 RPM 确认无异常振动」。确认更高速平衡后改常量 `SPIN_MAX_RPM_PANEL` 即可放开。
- **工艺通道 CH3-8 映射未定**：师兄 config 有冲突（`nitrogen=2` 撞 `z_brake`、`pump=3` 撞 `vacuum=3`）。面板做成「裸通道 on/off + 可编辑标签」（best-guess：CH3 vacuum/pump? · CH4 spin_power? · CH5 aux_light? · CH6-8 备用）。**PM 逐个开关、听吸合声 + 看执行器，敲定后在面板回填标签**，并把结论补到本节。
- **加热 SV 默认 40℃ + 红字「会真加热」**；写 SV 后 driver `run_on_sv_write=True` 自动启动控温。PM 在旁、设低温。
- **不用 sidebar 的「断开并重连」**（那只管龙门）——整套流程的连/断都用 `🔗 全设备连接` 区的两个按钮。

---

## 4. PM 物理动作 6 步清单 ⏳（需人在急停旁，逐项打勾 + 补证据）

浏览器开 `http://100.90.0.36:8501`，按序点：

- [ ] **0 连接**：`🔗 连接全部设备`（龙门串口默认 `/dev/autospin_xyz`）→ 会话设备 5 个全 ✅。龙门若 `Alarm`/`Hold` → 点状态卡的 `🔧 清除并恢复` → `🏠 归零`。
- [ ] **1 夹取样品**：Jog/「去这里」到样品位 → `🦾 夹爪 → ✊ 夹紧` → 主 Z 抬起（Z 刹车自动时序）。
- [ ] **2 送到旋涂台**：「去这里」到旋涂卡盘上方 → 下降放置 →（若用真空吸盘：`🔌 工艺通道` 开对应 CH 并回填标签）。
- [ ] **3 旋涂**：`🌀 旋涂 → 1️⃣ 解锁 → 2️⃣ 启动(forward) → 设速 100 → 读实际转速 →（确认平稳再逐级升）→ ⏹ 停止 → 🔒 锁定`。⏳ 滴液本次手动/省略。急停随时可按 `🛑 旋涂急停`。
- [ ] **4 退火**：（关真空）夹取 → 「去这里」到加热台 → 放置 → `🔥 加热 → 设 SV 60℃` → `🌡 读 PV` 看缓升 → 保持。
- [ ] **5 放回收尾**：夹取 → 回托盘 → `🖐 松开` → 龙门 park、各工艺通道关、旋涂 `🔒 锁定`、`⛔ 断开全部设备`。

**验收 = PM 点完上面、样品被夹起→旋涂→退火→放回。** 证据（截图/短视频/读数）补到这里：

> _（待 PM 现场补）_

---

## 5. 复跑 / 运维

```bash
# 启动面板（Pi）
ssh pi-spin
cd ~/autospin
.venv/bin/streamlit run tools/ui/emergency_dashboard.py \
  --server.headless=true --server.address=0.0.0.0 --server.port=8501 \
  --browser.gatherUsageStats=false
# 浏览器：http://100.90.0.36:8501

# 重跑非动作接线 smoke（全只读，从 repo 根跑）
.venv/bin/python tools/panel_wiring_smoke.py
```

⚠️ 改 `src/*` 或面板后**必须重启 streamlit 进程**（sys.modules 缓存 + session_state 持旧实例，浏览器刷新无效）。

---

## 6. 不在本次范围

移液枪集成（坏、排除）· 加热/旋涂的 ADR-004 合规壳（Chunk 5）· Z2 物理修复（DM320）· Agent 自动跑（Chunk 6，本面板是其人肉预演）。

---
*2026-06-20 制定。代码 + 真机只读接线 = Claude 验；物理动作 6 步 = PM 现场补 §4。*
