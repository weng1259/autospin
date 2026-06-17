# 📘 RaspberryPi5_自动化平台部署记录

## 项目名称：AutoSpin2026 自动化实验平台

## 设备：Raspberry Pi 5

## 系统：Raspberry Pi OS (64-bit, Bookworm)

## 时间：2026

---

# 一、系统目标

本项目用于构建一个基于 Raspberry Pi 5 的自动化实验控制平台，实现以下功能：

* RS485 温控器（宇电 AI-516）控制
* STM32 / Arduino XYZ 运动控制
* XR21B1411 串口扩展设备
* pipette 自动加液模块
* spin coater 控制
* 继电器 IO 控制
* Python 统一调度系统（Maestro）

# 连接树莓派：ssh pi@192.168.137.212
# 密码：emitlab2026
---

# 二、硬件接入状态

## 2.1 USB设备识别结果

```bash
ls -l /dev/serial/by-id/
```

输出：

```
usb-1a86_USB_Serial-if00-port0 → ttyUSB1
usb-Exar_Corp._XR21B1411_Q8554248371-if00-port0 → ttyUSB0
usb-STMicroelectronics_STM32_Virtual_ComPort_in_FS_Mode → ttyACM0
```

---

## 2.2 设备映射关系

| 设备           | 节点           | 功能                   |
| ------------ | ------------ | -------------------- |
| CH340 (1a86) | /dev/ttyUSB1 | 宇电 AI-516 温控器（RS485） |
| XR21B1411    | /dev/ttyUSB0 | 辅助串口设备               |
| STM32        | /dev/ttyACM0 | XYZ 运动控制             |

---

# 三、设备抽象（udev规则）

## 3.1 创建规则文件

```bash
sudo nano /etc/udev/rules.d/99-autospin.rules
```

## 3.2 写入规则

```txt
SUBSYSTEM=="tty", ATTRS{idVendor}=="1a86", SYMLINK+="autospin_heater"
SUBSYSTEM=="tty", ATTRS{idVendor}=="04e2", ATTRS{idProduct}=="1411", SYMLINK+="autospin_aux"
SUBSYSTEM=="tty", ATTRS{idVendor}=="0483", SYMLINK+="autospin_xyz"
```

---

## 3.3 规则生效

```bash
sudo udevadm control --reload-rules
sudo udevadm trigger
```

或重启：

```bash
sudo reboot
```

---

## 3.4 验证结果

```bash
ls -l /dev/autospin*
```

输出：

```
/dev/autospin_heater → ttyUSB1
/dev/autospin_aux    → ttyUSB0
/dev/autospin_xyz    → ttyACM0
```

---

# 四、Python开发环境搭建

## 4.1 创建项目结构

```bash
mkdir -p AutoSpin2026/{core,devices,test}
cd AutoSpin2026
```

---

## 4.2 创建虚拟环境

```bash
sudo apt install python3-venv python3-full -y
python3 -m venv venv
source venv/bin/activate
```

---

## 4.3 安装依赖

```bash
pip install pymodbus pyserial
```

---

# 五、项目代码结构

```
AutoSpin2026/
├── core/
│   └── maestro.py
├── devices/
│   ├── heater.py
│   ├── motion.py
│   ├── aux.py
├── test/
│   └── demo.py
```

---

# 六、设备控制层实现

## 6.0 写入程序方式：

# 1.进入对应环境
# cd ~/AutoSpin2026
# source venv/bin/activate

# 2.进入书写界面
# nano test/modbus_test.py

# 3.运行方式

#  python test/modbus_test.py



## 6.1 Heater（宇电 Modbus）

```python
from pymodbus.client import ModbusSerialClient

class Heater:
    def __init__(self, port="/dev/autospin_heater"):
        self.client = ModbusSerialClient(
            port=port,
            baudrate=9600,
            timeout=1
        )
        self.client.connect()

    def set_temp(self, value):
        self.client.write_register(0, int(value), slave=3)

    def read_temp(self):
        rr = self.client.read_holding_registers(74, 1, slave=3)
        return rr.registers[0] if rr else None
```

---

## 6.2 Motion（STM32）

```python
import serial

class Motion:
    def __init__(self, port="/dev/autospin_xyz"):
        self.ser = serial.Serial(port, 115200)

    def move_x(self, step):
        self.ser.write(f"X{step}\n".encode())
```

---

## 6.3 Aux（XR串口）

```python
import serial

class Aux:
    def __init__(self, port="/dev/autospin_aux"):
        self.ser = serial.Serial(port, 115200)

    def send(self, msg):
        self.ser.write(msg.encode())
```

---

# 七、主控制器（Maestro）

```python
from devices.heater import Heater
from devices.motion import Motion
from devices.aux import Aux

class Maestro:
    def __init__(self):
        self.heater = Heater()
        self.motion = Motion()
        self.aux = Aux()

    def heat(self, temp):
        self.heater.set_temp(temp)

    def move(self, x=0):
        self.motion.move_x(x)

    def log(self, msg):
        self.aux.send(msg)
```

---

# 八、测试程序

## 8.1 demo.py

```python
from core.maestro import Maestro
import time

m = Maestro()

print("=== AutoSpin Start ===")

m.heat(60)
time.sleep(1)

m.move(100)

m.log("experiment start")

print("=== Done ===")
```

---

## 8.2 运行方式（必须）

```bash
cd ~/AutoSpin2026
source venv/bin/activate
PYTHONPATH=. python test/demo.py
```

---

# 九、系统调试关键点

## 9.1 设备确认

```bash
ls -l /dev/autospin*
ls -l /dev/serial/by-id/
```

---

## 9.2 串口调试

```bash
dmesg | grep tty
```

---

## 9.3 权限配置

```bash
sudo usermod -a -G dialout pi
```

---

# 十、当前系统状态总结

## ✔ 已完成：

* Raspberry Pi OS 安装
* SSH 远程连接
* USB设备识别
* RS485（宇电）接入
* STM32 控制接入
* XR 串口接入
* udev设备映射
* Python控制框架
* 虚拟环境配置

---

## ⚠ 当前限制：

* Modbus通信未完成闭环验证
* 温控未确认实际写入成功

---

# 十一、系统架构（最终形态）

```
           PC
            │ SSH
            ▼
   Raspberry Pi 5 (AutoSpin2026)
 ┌──────────────────────────────┐
 │ Heater (RS485 Modbus)       │
 │ Motion (STM32 XYZ)          │
 │ Aux (XR21B1411 Serial)      │
 │ Pipette System              │
 │ Spin Coater                │
 │ Relay IO                   │
 └──────────────────────────────┘
```

---

# 十二、下一阶段（建议）

后续系统升级方向：

### 🚀 v1.1

* Modbus真实闭环控制
* SV/PV实时读取

### 🚀 v1.2

* spin coating 自动流程

### 🚀 v2.0

* 全实验自动化脚本系统（JSON）

---

# 十三、树莓派部署、网络、SSH、设备接入与故障排查全过程复盘

以下记录整理本次 Raspberry Pi 5 部署、网络连接、SSH 登录、USB 设备接入和故障排查全过程，可作为后续复现与交接依据。

---

## 13.1 项目目标

构建如下自动化控制架构：

```text
PC（上位机）
↓ SSH
Raspberry Pi 5（主控）
├── Arduino Mega2560（XYZ控制）
├── USB-RS485（宇电温控 + 旋涂电机）
├── USB移液枪
└── USB继电器

最终控制：
XYZ导轨 / 加热台 / 旋涂电机 / 夹爪 / 移液系统
```

---

## 13.2 系统安装与首次烧录

### 使用工具

* Raspberry Pi Imager
* SD 卡：SanDisk Ultra 64GB

### 初始配置（错误方式）

首次烧录时启用了：

* WiFi
* SSH
* Hostname
* 用户名密码

### 出现问题

```text
ping raspberrypi.local → 找不到主机
SSH失败
WiFi未连接
```

---

## 13.3 第一次问题总结

### 现象

* 绿灯亮一下后常亮
* 无网络设备出现
* 手机热点看不到树莓派

### 判断

系统未正确初始化或配置错误。

---

## 13.4 正确重装方式

### 操作

重新烧录系统：

* 不设置任何自定义选项
* 关闭 WiFi
* 关闭 SSH
* 不设置用户名
* 不设置 hostname

### 结果

```text
绿灯：持续闪烁（正常）
说明系统正常启动
```

---

## 13.5 网络连接方式（网线直连）

### 拓扑

```text
PC ←→ 网线 ←→ Raspberry Pi
```

### Windows 检测

```cmd
ipconfig
```

结果：

```text
169.254.x.x（自动分配IP）
```

说明：直连成功。

---

## 13.6 主机发现问题（.local）

### 命令

```cmd
ping raspberrypi.local
```

结果：

* 一开始成功（IPv6）
* 后期失败（解析丢失）

### 原因

Windows mDNS 不稳定。

---

## 13.7 SSH 配置与成功登录

### 正确方式

```cmd
ssh pi@AutoSpin2026.local
```

输入密码后成功进入：

```text
Linux AutoSpin2026 ...
```

---

## 13.8 SSH 随机断开问题

### 现象

```text
client_loop: send disconnect: Connection reset
```

---

## 13.9 核心问题诊断

### dmesg 输出

```text
Undervoltage detected!
Voltage normalised
```

### vcgencmd 输出

```text
throttled=0x50000
```

---

## 13.10 根本原因

### 供电不稳定

当时使用：

```text
HUAWEI Quick Charge + 普通 Type-C 线
```

导致：

* 电压波动
* USB 重置
* SSH 断开

---

## 13.11 解决方案（已执行）

已更换电源：

```text
DL-PD27W-CN
```

但仍建议：

* 使用 5V 5A 稳定电源
* 使用 E-Marker 线

---

## 13.12 USB 设备接入情况

### lsusb 结果

```text
STMicroelectronics → Arduino Mega2560
CH340 → RS485模块
XR21B1411 → 移液枪
```

### 串口映射

```text
/dev/ttyACM0 → Arduino
/dev/ttyUSB0 → RS485
/dev/ttyUSB1 → 移液枪
```

---

## 13.13 当前网络状态

```bash
nmcli device status
```

```text
eth0 → connecting
wlan0 → disconnected
```

说明：

* 没有互联网
* 仅局域网 SSH 可用

---

## 13.14 当前问题总结

### 已解决

* 树莓派系统启动
* SSH 连接成功
* USB 设备识别
* 串口分配完成

### 未完全解决

* 供电稳定性仍需优化
* WiFi 未配置
* apt 无法联网
* mDNS（.local）不稳定

---

## 13.15 当前正确架构状态

```text
PC
 ↓ SSH
Raspberry Pi 5
   ├── Arduino (/dev/ttyACM0)
   ├── RS485 (/dev/ttyUSB0)
   └── 移液枪 (/dev/ttyUSB1)
```

---

## 13.16 下一步开发目标

### 网络优化

* 配置 WiFi
* 或固定 IP

### 系统依赖安装

```bash
sudo apt update
pip install pyserial pymodbus numpy matplotlib
```

### 工业控制目标

* Arduino 控制 XYZ
* RS485 控制：
  * 宇电温控
  * 旋涂电机
* 移液枪自动控制
* 继电器控制夹爪

---

## 13.17 最终系统目标

实现：

```text
PC → 树莓派 → 多设备联动控制

闭环自动化平台：
- 薄膜制备
- 旋涂
- 加热退火
- 移液
- XYZ定位
```

后续可继续升级为 AutoSpin System v1.0 架构代码，覆盖 Python 串口、RS485、Arduino 调度器和多设备联动控制。
