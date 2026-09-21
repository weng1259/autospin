# autospin Experiment Runbook

## 1. 使用范围

本 Runbook 用于迁移后 `autospin` 平台的软件检查、网页操作、routine
录制与重放，以及受控硬件实验。

真实硬件前必须先完成 `HARDWARE_ACCEPTANCE_TEST_PLAN.md`，并在
`HARDWARE_ACCEPTANCE_RESULT.md` 中记录结果。

## 2. 启动前检查

```bash
cd /home/pi/autospin
git rev-parse HEAD
python --version
python -m pytest -q
```

确认：

- `config/hardware.yaml` 中串口和地址正确
- 没有其他进程占用串口
- Gantry、Relay、RS485 使用稳定设备路径
- Gantry 行程无障碍
- Gripper 下方有安全承接区域
- Spin Motor 防护罩已安装
- Heater 周围无易燃物
- Pipette 使用正确 tip 和液体

串口检查：

```bash
lsof /dev/rs485_bus /dev/ttyUSB1 2>/dev/null || true
ls -l /dev/serial/by-id/
```

## 3. Web 服务

### Mock 模式

```bash
python tools/run_webserver.py --mock
```

### 真实配置模式

```bash
python tools/run_webserver.py --host 127.0.0.1 --port 8800
```

终端会输出 Bearer token。浏览器访问：

```text
http://127.0.0.1:8800/
```

把 token 填入网页“连接设置”。

如果需要从另一台电脑访问，必须先配置受控网络、鉴权和防火墙，再显式
绑定局域网地址。不要直接暴露到公网。

## 4. 当前网页能力

网页可以：

- 查看设备状态
- 执行系统急停
- Gantry 归零、点动、绝对坐标移动
- 设置 Heater 温度
- 设置 Spin Motor 转速并启停
- 执行 Pipette 和 Linear Stage 动作
- 控制 Gripper 和 Relay
- 开始/停止 routine 录制
- 保存、查看、重放、删除和中止 routine

Routine 录制方式：

1. 输入程序名称。
2. 点击“开始录制”。
3. 在设备面板按顺序执行动作。
4. 点击“停止录制并保存”。
5. 在程序列表中检查步数和运动标记。
6. 首次重放先使用 Mock 或空载硬件。
7. 确认路径后再执行真实重放。

## 5. 坐标操作

网页可直接填写 Gantry 的 X/Y/Z 绝对坐标，但目前不提供持久化坐标库编辑器。

注意：当前网页输入框仍是旧范围 `X/Y -275..-5、Z -90..-5`，Backend
配置为 `X/Y -310..-5、Z -110..-5`。后端安全检查正确，但超出网页旧范围的
合法坐标需要等待 UI 改为动态读取配置，不能通过修改 HTML 或绕过 Backend
临时解决。

坐标必须区分：

```text
机器/GRBL 行程
软件软限位
实验工位坐标
工具偏移
```

不得把工位坐标直接写成新的软件限位，也不得从网页绕过 Backend 的限位检查。

安全移动顺序：

```text
Z 到安全高度
  -> XY 移动
  -> Z 下降到工艺位置
```

Pick 顺序：

```text
安全 Z 到目标 XY
  -> Z 下降
  -> Gripper close
  -> 等待 1.0 秒
  -> Z 上升
  -> XY 转移
```

## 6. 多轮实验现状

当前 autospin Web 页面没有多轮生成器。

已有 Routine 功能解决的是“录制并重放一段固定动作”，不是：

- 输入轮数
- 修改一轮模板参数
- 自动展开样品位
- 自动更新每轮坐标
- 批量生成 N 轮 recipe

legacy `AutoSpinmotorSystem` 中存在：

```text
generate_multi_round_sequence.py
protocol/sequence_generator.py
config/process_coordinates.yaml
```

这些逻辑尚未作为 autospin Web API 和页面迁移。不要让网页直接执行 legacy
硬件控制；后续应只迁移 recipe 生成与验证逻辑，并让执行仍经过
DeviceRegistry 和 Backend。

## 7. 推荐的 Experiment Builder

后续网页建议新增独立的“实验构建器”，而不是扩张单设备控制面板。

最小功能：

- 选择单轮模板
- 输入轮数
- 设置样品起始编号和样品位映射
- 编辑工位坐标引用
- 编辑 Spin RPM/时间
- 编辑 Pipette 体积和延迟
- 编辑 Heater SV/时间
- 显示展开后的完整步骤
- Dry-run 验证软限位、体积、温度和设备能力
- 人工确认后保存 recipe
- 通过 Routine/System API 执行
- 支持中止、恢复和运行记录

配置和 recipe 文件应由后端做 schema 校验和原子写入，不能由浏览器直接修改
YAML。

## 8. 单设备预检

Gantry 默认无动作检查：

```bash
python tools/gantry_runtime_smoke.py
```

其他设备使用对应 smoke：

```text
tools/spincoater_smoke.py
tools/heater_smoke.py
tools/pipette_smoke.py
tools/linearstage_smoke.py
```

详细命令和恢复步骤见 `HARDWARE_ACCEPTANCE_TEST_PLAN.md`。

## 9. 真实运行

真实 routine 前：

1. 检查配置和 Git commit。
2. 确认硬件验收仍有效。
3. Mock 重放。
4. 空载真实重放。
5. 检查 Gantry 已归零。
6. 检查所有坐标和工具偏移。
7. 检查液体、tip、样品、加热和旋转防护。
8. 确认急停和 Gripper 掉样承接区域。
9. 开始运行并持续监控。

## 10. 故障处理

### Gantry

- 立即停止运动。
- 记录 GRBL alarm。
- 检查限位、Z 刹车和障碍物。
- 解锁前先确认故障原因。
- 恢复后重新归零。

### Relay/Gripper

- 先停止 Gantry。
- 支撑 Z 和被夹样品。
- 检查 CH1/CH2、USB、电源、接地和 EMI。
- 记录 `dmesg -T`。
- 不允许第二个 RelayBackend 占用同一串口。

### RS485

- 停止所有设备动作。
- 关闭所有总线客户端。
- 检查端口占用、A/B、GND、终端和波特率。
- 从单设备只读测试重新开始。

### Heater

- 温度失控时切断 SSR/Heater 电源。
- 不依赖软件 SV=0 作为唯一保护。

### Spin Motor

- 旋转异常时使用物理停机/断电。
- 等待完全停止后才能打开防护。

## 11. 停机

1. 中止正在运行的 routine。
2. 执行系统急停/安全停止。
3. 确认 Gantry 和 Linear Stage 停止。
4. 确认 Spin Motor 为零速。
5. 确认 Heater 停止加热。
6. 确认 Gripper 已按当前策略释放。
7. 关闭 Web 服务。
8. 关闭硬件连接和电源。
9. 保存日志与实验结果。
