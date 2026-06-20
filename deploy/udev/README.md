# AutoSpin 串口稳定命名 (udev)

把树莓派上三个 USB 串口设备映射成稳定名，供 `autospin_system/config/system_config.yaml` 引用。

## 设备 → 稳定名

| 物理 USB 口 | 芯片 | 设备 | 稳定名 |
|---|---|---|---|
| 1-1 | CH340 `1a86:7523` | grbl-Mega-5X 运动控制器 | `/dev/autospin_xyz` |
| 1-2 | CH340 `1a86:7523` | USB-RS485 模块（加热台/移液/电机共享总线）| `/dev/autospin_rs485`（别名 `/dev/autospin_heater`）|
| 3-2 | STM32 `0483:5740` | DSTUR-T80 USB 继电器 | `/dev/autospin_relay` |

## 为什么按物理 USB 口（不是 hwid）

grbl 和 RS485 模块**同为 CH340，hwid 完全相同（`1a86:7523`）且无唯一序列号**，
没法用 vendor:product 区分，只能按**物理 USB 口**（udev `KERNELS`）锁定。
继电器是 STM32、hwid 唯一，按 hwid 即可。

## 两个坑（改规则前必读）

1. **`KERNELS` 用 USB 设备层 `1-1`/`1-2`，不是接口层 `1-1:1.0`**。
   接口层节点没有 `idVendor`/`idProduct`，`KERNELS` 与 `ATTRS{idVendor}` 落在不同节点
   会**静默匹配失败**（不报错、软链不生成）。
2. **物理口绑定**：grbl 必须插 **1-1** 口、RS485 模块必须插 **1-2** 口。
   换口软链会跟着错位，须更新规则里的 `KERNELS`
   （`udevadm info -a -n /dev/ttyUSBx` 看 `KERNELS==` 值；`ID_PATH` 里 `usb-0:1` 是 1-1 口、`usb-0:2` 是 1-2 口）。

## 安装 / 刷新

```bash
deploy/udev/install.sh
```

幂等、自愈：装规则 → 清残留软链 → 重载 → 触发 add 事件重建 → 列结果。
**若某次插拔后软链没刷新（时间戳是旧的、或 `autospin_rs485` 缺失），重跑本脚本即可修复。**

## 验证软链指对设备

- grbl：对 `/dev/autospin_xyz` 发 grbl `?`（DTR 复位后）应回 `Grbl 1.x` 横幅 / `<...>` 状态
- 加热台：读 `/dev/autospin_rs485`（slave 3 / reg 74 / 9600 8N1 holding）应回 PV ≈ 室温
  - 现成脚本：`.venv/bin/python -m autospin_system.test_heating_stage_smoke`（只读默认）
- 回归护栏（不连硬件）：`.venv/bin/python -m pytest autospin_system/test_port_mapping.py`
