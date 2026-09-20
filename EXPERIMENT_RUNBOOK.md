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

网页可直接填写 Gantry 的 X/Y/Z 绝对坐标，用于教导和空载检查；
网页输入范围从 `config/hardware.yaml` 动态读取。持久化工艺坐标的唯一
真值文件是 `config/coordinates.yaml`，当前静态页面没有可视化坐标编辑器。
修改、校验、上传和重启流程见
`docs/guides/coordinates-and-spin-deceleration.md`。

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
config/coordinates.yaml
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

### 7.1 反溶剂与旋涂启动顺序

含定时反溶剂的语义实验按以下顺序执行：

```text
吸取反溶剂完成
→ 打开/确认旋涂真空
→ 立即启动第一段旋涂并建立计时零点
→ 移液器移动到旋涂仪滴加位置
→ 等待到 antisolvent_at_s 后滴加
→ 完成后停止旋涂并退枪头
```

因此，移液器前往旋涂仪的移动时间会与第一段旋涂重叠，不再推迟电机启动。
如果移液器到位时间已经超过 `antisolvent_at_s`，系统会在安全到位后立即滴加，
不会在移动过程中提前滴液。首次上机应使用低速、空载方式确认移动路径不会与
旋转部件干涉，并确认 `antisolvent_at_s` 大于正常到位时间。

### 7.2 从热台边界离开

热台首行当前位于 `Y=-5 mm`，接近已验证工作区上界。自动化从热台前往下一个
基片位置时采用以下路径：

```text
Z 抬升到 safe_travel
→ Y 以 boundary_feed_mm_min 单轴退让到 -15 mm
→ 执行常规 XY 转移
→ Z 下降到目标工位
```

退让距离由 `config/hardware.yaml` 的 `boundary_escape_mm` 配置，当前为 `10 mm`。
这是从 `AutoSpinmotorSystem` 实机修改日志迁移的 `ALARM:1 / Pn:XYZ` 边界离开
保护。报警后程序不会自动 `$X` 或自动续跑，必须检查 `Pn:`、重新归零并确认
坐标可信后再开始实验。

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

## 9. autospin recipe 启动流程

当前完整实验运行路径是项目本身的 recipe 存储与语义执行接口：

```text
recipes/*.json + POST /api/experiments/multi-round/execute
```

### 9.1 Dry-run 检查

启动当前 Web 服务：

```bash
cd ~/autospin
source .venv/bin/activate
python3 tools/run_webserver.py --host 127.0.0.1 --port 8800
```

在网页实验构建器中生成并保存 recipe，然后点击“运行已保存 recipe（Dry-run）”。
Dry-run 必须通过，并完成以下人工检查后，才允许真实运行：

- 坐标与工具偏移
- 枪头、样品和液体
- 真空与继电器通道
- 夹爪开合与掉样承接区域
- 热台温度与周边防护
- 旋涂台、转盘防护和零速状态
- 急停、断电和现场监控条件

### 9.2 树莓派环境准备

```bash
ssh pi@192.168.50.2
cd ~/autospin
source .venv/bin/activate
python3 -m pip install -r requirements.txt
```

### 9.3 上传坐标配置

在 Windows 电脑本地项目目录上传已确认的坐标文件：

```bash
cd D:\study\pythonlearning\study\history_version\AutoSpinMerge\autospin
scp -O config\coordinates.yaml pi@192.168.50.2:/home/pi/autospin/config/coordinates.yaml
```

上传后重启 Web/自动化进程，再在树莓派上执行 mock 检查，
确认使用的是最新坐标。

### 9.4 生成多轮 recipe

在网页“实验构建器”中设置参数组和每组重复次数，先预览，再点击“生成并保存多轮
recipe”。文件写入 `/home/pi/autospin/recipes/`；如果设置了
`AUTOSPIN_RECIPES_DIR`，则写入该变量指定的目录。生成后检查轮数、枪头编号、样品位、
坐标和动作顺序。

### 9.5 真实运行入口

在同一网页中选择刚保存的 recipe，点击“正式运行已保存 recipe”。后端调用
`POST /api/experiments/multi-round/execute`，并以 `dry_run=false` 执行。

真实运行期间必须有人在场监控，并保持急停、断电和防护可用。

### 9.6 临时 recipe 清理

不需要的单个生成文件可以按明确路径逐个删除，例如：

```bash
rm recipes/one_round_from_process_coordinates_4_rounds.json
```

不要使用批量删除命令；删除前确认路径中包含 `/`，避免误写为 `examplesone_round...`。

## 10. 真实运行

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

## 11. 故障处理
### 提取本次完整动作顺序，方便查询报错原因
python3 - <<'PY'
import json
from src.runlog import RUNLOG

methods = ("LinearStageBackend", "GantryBackend", "PipetteBackend")
events = [
    event for event in RUNLOG.query_recent(500)
    if any(name in event["method"] for name in methods)
]

for event in reversed(events[:50]):
    print(json.dumps(event, ensure_ascii=False, indent=2))
PY

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

## 12. 停机

1. 中止正在运行的 routine。
2. 执行系统急停/安全停止。
3. 确认 Gantry 和 Linear Stage 停止。
4. 确认 Spin Motor 为零速。
5. 确认 Heater 停止加热。
6. 确认 Gripper 已按当前策略释放。
7. 关闭 Web 服务。
8. 关闭硬件连接和电源。
9. 保存日志与实验结果。
