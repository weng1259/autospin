# 坐标修改、旋涂减速与树莓派部署

## 1. 旋涂加速与减速

旋涂仪的启动加速和正常停止减速都是上位机软件通过
`0x8005` 速度寄存器每 `200 ms` 分步改变指令转速实现的。程序不写入
未经手册确认的 DBLS400 加减速寄存器。

- 默认启动加速：`500 RPM/s`。
- 默认停止减速：`500 RPM/s`。
- 网页“旋涂台”面板可分别设置两个值，范围均为 `50..6000 RPM/s`。
- 普通“停止”与自动化流程结束会先按减速度降到 `0 RPM`，再执行制动或滑停。
- 全局急停始终跳过减速斜坡，立即写入制动停机命令。

网页上修改的加减速只对当前服务进程有效。要修改每次启动的默认值，
编辑 `config/hardware.yaml`：

```yaml
hardware:
  spincoater:
    acceleration_rpm_per_s: 500
    deceleration_rpm_per_s: 500
```

## 2. 坐标的唯一修改位置

当前自动化使用的唯一工艺坐标真值文件是：

```text
config/coordinates.yaml
```

不要修改 `migration_snapshot/`、`autospin_system/`或旧
`AutoSpinmotorSystem/config/process_coordinates.yaml` 中的副本；当前 `autospin`
自动化不从这些文件加载坐标。

`coordinates.yaml` 的主要区域：

- `coordinate_system.verified_limits`：已验证机器坐标边界，不是普通工位坐标。
- `global_safe_positions`：全局安全高度和安全位置。
- `stations`：旋涂台、热台、样品盘、移液和枪头等工位。
- `safe_above`：进入工位前的安全上方点。
- `operation`：真正夹取、放置或操作点。
- `retract`：完成操作后的撤离点。
- `tool_offsets`：工具几何信息；当前已教导工位不会在运行时再重复叠加偏移。
- `calibration`：独立滑台位置、间隙等标定值。

坐标使用 GRBL 负坐标系，所有可执行点必须在软限位内。

### 2.1 只修改首点即可整体平移

规则工位已使用 `grid` 描述行列数和间距。通常只需修改下列首点：

| 工位 | 只需修改的首点 | 自动更新内容 |
| --- | --- | --- |
| 旋涂仪 | `stations.spin_coater.operation.xyz` | `safe_above`、`retract` 的 X/Y；安全 Z 保持 -30 mm |
| 热台 | `stations.hotplate.place.xyz` | 3x3 热台全部槽位及其安全点 |
| 基片架 | `stations.substrate_rack.pickup.xyz` | 4x4 共 16 个槽位 |
| 枪头架 | `stations.tip_rack.pickup.xyz` | 8x12 共 96 个槽位及 `pickup_2` |
| 试剂架 | `stations.reagent_rack.precursor_1.xyz` | 其余前驱体位置及反溶剂位置 |

例如，修改枪头架时只改：

```yaml
stations:
  tip_rack:
    pickup:
      xyz: [-74.5, -92.0, -80.0]
```

`grid.step_xyz_mm` 是相邻列 X、相邻行 Y、相邻行 Z 的偏移量。只有实测架子
孔距或排列方向变化时才修改它；普通整体校准不要修改步距。枪头架和基片架
使用从左到右、再进入下一行的 `row_major` 编号。

`spin_coater.pipette_dispense` 是移液工具对准点，不等于夹爪放片点，仍需独立
标定。各首点的 `linear_stage_position_mm` 也仍需按对应机构单独标定。

新坐标在完成空载教导前应先设为：

```yaml
status: modified_after_verification
requires_hardware_confirmation: true
executable: false
```

确认绝对移动、安全上方点、撤离路径和滑台位置均正确后，再改为
`requires_hardware_confirmation: false` 和 `executable: true`。两个字段不能在
“待硬件确认”时同时为 `true`，配置校验会拒绝加载。

## 3. 本地修改与校验

在 Windows PowerShell 中进入：

```powershell
cd "D:\study\pythonlearning\study\history_version\AutoSpinMerge\autospin"
```

编辑 `config\coordinates.yaml` 后先执行：

```powershell
D:\python\python3.11.0\python.exe -c "from src.coordinates import load_coordinates; c=load_coordinates(); print('coordinates OK:', len(c.all_points()))"
D:\python\python3.11.0\python.exe -m pytest tests\test_coordinates.py tests\test_multi_round.py tests\test_experiment_generation.py -q
```

首次修改工位时，先用网页的龙门架绝对移动和“仅校验”功能做空载验证，
不要直接执行完整自动化。

## 4. 上传坐标到树莓派

上传前先把树莓派现有标定文件下载为备份：

```powershell
scp -O pi@192.168.50.2:/home/pi/autospin/config/coordinates.yaml config\coordinates.raspberrypi-backup.yaml
```

新的阵列生成规则位于 `src\coordinates.py`，首次部署本次优化时必须和坐标
文件一起上传：

```powershell
scp -O src\coordinates.py pi@192.168.50.2:/home/pi/autospin/src/coordinates.py
scp -O config\coordinates.yaml pi@192.168.50.2:/home/pi/autospin/config/coordinates.yaml
```

在树莓派校验：

```powershell
ssh pi@192.168.50.2 "cd /home/pi/autospin && .venv/bin/python -c 'from src.coordinates import load_coordinates; c=load_coordinates(); print(len(c.all_points()))'"
```

## 5. 何时必须重启

Web 服务在启动时通过 `CoordinateRegistry.from_yaml()` 把坐标读入内存。
因此，手工编辑、SCP 上传或调用坐标 PUT API 之后，都要停止旧的
Web/自动化进程并重新启动。单独刷新浏览器不会重载坐标。
语义实验流程会在运行时解析新坐标；但网页已录制 routine 中的手动
`move_to` 保存的是当时的数值 XYZ，不会随坐标库自动更新。坐标标定改变后，
这类固定坐标 routine 需要重新录制或重新生成。

如果当前是终端前台运行：

```bash
# 在旧服务终端按 Ctrl+C，然后：
cd /home/pi/autospin
source .venv/bin/activate
python3 tools/run_webserver.py --host 0.0.0.0 --port 8800
```

重启后会生成新 Bearer token，需在网页重新填写。

## 6. 上传本次旋涂减速修改

在 `autospin` 根目录逐个上传：

```powershell
scp -O src\config.py pi@192.168.50.2:/home/pi/autospin/src/config.py
scp -O src\hardware\spincoater_backend.py pi@192.168.50.2:/home/pi/autospin/src/hardware/spincoater_backend.py
scp -O src\hardware\autospinmotor_adapters\spin_motor_adapter.py pi@192.168.50.2:/home/pi/autospin/src/hardware/autospinmotor_adapters/spin_motor_adapter.py
scp -O src\webapp\registry.py pi@192.168.50.2:/home/pi/autospin/src/webapp/registry.py
scp -O src\webapp\mock_devices.py pi@192.168.50.2:/home/pi/autospin/src/webapp/mock_devices.py
scp -O src\webapp\routes_devices.py pi@192.168.50.2:/home/pi/autospin/src/webapp/routes_devices.py
scp -O src\webapp\static\index.html pi@192.168.50.2:/home/pi/autospin/src/webapp/static/index.html
scp -O src\webapp\static\app.js pi@192.168.50.2:/home/pi/autospin/src/webapp/static/app.js
scp -O src\system_estop.py pi@192.168.50.2:/home/pi/autospin/src/system_estop.py
scp -O config\hardware.yaml pi@192.168.50.2:/home/pi/autospin/config/hardware.yaml
```

上传后重启 Web 服务，再用低转速、无样品状态验证加速和减速斜坡。
