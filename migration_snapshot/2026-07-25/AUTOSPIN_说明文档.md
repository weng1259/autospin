# autospin 说明文档

## 项目定位

`autospin` 是 AutoSpinmotorSystem 迁移后的主软件框架。它负责：

- 后端硬件抽象
- 统一配置
- DeviceRegistry 设备组合
- 实验 routine
- Web API 与网页控制
- Mock、测试、日志和安全急停

硬件行为来源仍是经过树莓派验证的 `AutoSpinmotorSystem` 实现。迁移采用
Backend/Adapter/Driver 分层，不直接复制旧项目目录。

## 当前架构

```text
Web / Routine / System
          |
          v
DeviceRegistry
          |
          v
Backend APIs
          |
          v
Adapters
          |
          v
Verified drivers
          |
          v
Serial / Modbus / GRBL / Relay IO
```

主要目录：

```text
src/
  config.py
  system_estop.py
  routine.py
  hardware/
    spincoater_backend.py
    heater_backend.py
    pipette_backend.py
    linearstage_backend.py
    relay_backend.py
    gantry_backend.py
    gripper_backend.py
    autospinmotor_adapters/
    drivers/
  webapp/
    app.py
    registry.py
    routes_*.py
    static/

config/
  hardware.yaml
  devices.yaml

tools/
  run_webserver.py
  *_smoke.py
```

## 硬件模块

### Spin Motor

- 型号：DBLS400
- 协议：Modbus RTU
- 从站 ID：2
- 默认波特率：9600
- 保留控制字、寄存器、极对数和实际转速补偿行为
- Backend：`src/hardware/spincoater_backend.py`

### Heater

- 型号：AI-516
- 协议：Modbus RTU
- 从站 ID：3
- 默认波特率：9600
- 保留 PV/SV、Srun、PID 和 SSR 行为
- Backend：`src/hardware/heater_backend.py`

### Pipette

- 协议：Modbus RTU
- 从站 ID：1
- 默认波特率：115200
- 支持归位、吸液、吐液、退枪头和停止
- Backend：`src/hardware/pipette_backend.py`

### Linear Stage

- 当前设备为 RS485 单轴滑台，不再使用 Z2 名称
- 地址：4
- 默认波特率：115200
- Backend：`src/hardware/linearstage_backend.py`

### Relay

- 设备：DSTUR-T80/兼容 8 通道继电器
- 协议帧：`[0xA0, channel, state, checksum]`
- 最小写间隔：0.3 秒
- CH1：夹爪
- CH2：Gantry Z 刹车
- CH3：真空
- Backend：`src/hardware/relay_backend.py`

### Gantry

- 控制器：grbl-Mega-5X
- 串口：`/dev/ttyUSB1`
- 波特率：115200
- 归零：`$H`
- 解锁：`$X`
- 运动：`$J=G90 X... Y... Z... F...`
- 急停：Ctrl-X (`0x18`)
- 软件限位：

  ```text
  X: -310..-5 mm
  Y: -310..-5 mm
  Z: -110..-5 mm
  ```

- Backend：`src/hardware/gantry_backend.py`
- Driver：`src/hardware/drivers/gantry/grbl_controller.py`

### Gripper

- 纯 IO 控制
- CH1 ON：闭合
- CH1 OFF：打开
- 闭合稳定等待：1.0 秒
- 急停策略：释放夹爪
- 无位置、夹持、力和样品检测反馈
- 与 Gantry 共享同一个 RelayBackend

## 配置

运行参数集中在：

```text
config/hardware.yaml
config/devices.yaml
```

加载路径：

```text
YAML -> src/config.py -> DeviceRegistry.from_config() -> Backends
```

坐标、软限位、串口、从站 ID、波特率和安全参数不得重新硬编码到 UI
或实验脚本中。

## Web 服务

autospin 没有名为 `server.py` 的当前入口，但有等效的 FastAPI 服务：

```bash
python tools/run_webserver.py --mock
```

真实配置模式：

```bash
python tools/run_webserver.py --host 127.0.0.1 --port 8800
```

启动后终端会输出 Bearer token，浏览器访问：

```text
http://127.0.0.1:8800/
```

当前网页支持：

- 设备状态轮询
- 系统急停
- Gantry 归零、点动、绝对坐标移动和恢复
- Heater SV
- Spin Motor 转速和启停
- Pipette 动作
- Linear Stage 位置
- Relay/Gripper 动作
- Routine 录制、保存、列表、重放、删除和中止

当前网页不支持：

- 按轮数自动生成多轮实验
- 编辑并保存 process coordinate registry
- 批量编辑一轮模板后生成 N 轮
- 图形化修改全部 recipe 参数
- 样品位编号自动展开

这些能力仍需要新的 Experiment Builder 页面和后端生成 API。

当前网页 Gantry 数值输入框仍使用旧范围：

```text
X/Y: -275..-5
Z:   -90..-5
```

Backend 当前配置范围是：

```text
X/Y: -310..-5
Z:   -110..-5
```

Backend 校验仍是最终安全边界，但网页暂时无法输入完整配置范围。后续应让页面从
配置/API 读取范围，不能继续在 HTML 中硬编码。

## 测试

软件回归：

```bash
python -m pytest -q
```

硬件验收必须遵循：

- `HARDWARE_ACCEPTANCE_TEST_PLAN.md`
- `HARDWARE_ACCEPTANCE_RESULT.md`

默认优先运行无动作或 dry-run 模式。真实动作必须由现场人员确认串口、
供电、限位、路径、样品和急停条件。

## 安全原则

- Backend 是安全边界，不能只依赖网页按钮限制。
- 未归零 Gantry 不执行绝对实验坐标。
- XY 转移前必须到达安全 Z。
- 夹爪闭合后等待 1 秒，再抬 Z。
- 急停先停止 Gantry，再释放夹爪。
- 任何 Relay/RS485/GRBL 失败都不能伪装成成功。
- Gripper 状态仅表示最后命令，不表示物理到位。
- 运行日志和实验数据不写入 CHANGELOG。
