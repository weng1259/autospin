# AutoSpin 网页控制台使用说明

## 1. 用途与架构

`autospin` 提供当前维护中的网页控制台，用于设备状态监视、手动操作、程序录制与重放、实验 recipe 生成及工艺坐标维护。它取代实验性的
`AutoSpinmotorSystem/web_control/server.py` 页面。

网页层通过 `DeviceRegistry`、后端 API、适配器和已验证的控制器操作设备，不在浏览器中直接打开串口，也不允许网页请求临时改变硬件连接拓扑。串口、软限位和设备能力统一由配置文件管理。

完整实验仍应使用低层硬件动作 recipe 路径：

```text
examples/*.json + example_experiment.py：真实完整实验入口
templates.py + run_template_protocol.py：高级 protocol 检查路径，不作为真实完整实验入口
```

## 2. 部署、安装与启动

### 2.1 部署前检查

树莓派上的项目目录应为 `/home/pi/autospin`。先检查关键目录和文件，避免把新旧版本混在一起运行：

```bash
ssh pi@192.168.50.2
cd ~/autospin
test -f constants.yaml && echo constants-OK
test -f config/hardware.yaml && echo hardware-OK
test -f config/devices.yaml && echo devices-OK
test -f tools/run_webserver.py && echo webserver-OK
test -f src/webapp/registry.py && echo registry-OK
test -f src/hardware/autospinmotor_adapters/__init__.py && echo adapters-OK
```

也可以列出树莓派已有的硬件代码：

```bash
find /home/pi/autospin/src/hardware -maxdepth 3 -type f | sort
```

生产启动至少依赖以下内容：

- `src/`：配置模型、硬件后端、已验证驱动/适配器、Web API 和静态页面。
- `tools/run_webserver.py`：Web 服务入口。
- `requirements.txt`：Python 依赖。
- 根目录 `constants.yaml`：通用运动与安全配置。
- `config/hardware.yaml`：端口、地址、寄存器、软限位和设备能力。
- `config/devices.yaml`：逻辑设备注册和后端选择。
- `config/coordinates.yaml`：唯一的坐标、工具偏移与校准真值文件。

注意：`constants.yaml` 位于项目根目录，不是 `config/constants.yaml`。

### 2.2 从 Windows 上传代码

在 Windows 项目根目录
`D:\study\pythonlearning\study\history_version\AutoSpinMerge` 执行：

```powershell
scp -r autospin/src pi@192.168.50.2:/home/pi/autospin/
scp autospin/tools/run_webserver.py pi@192.168.50.2:/home/pi/autospin/tools/run_webserver.py
scp autospin/requirements.txt pi@192.168.50.2:/home/pi/autospin/requirements.txt
scp autospin/constants.yaml pi@192.168.50.2:/home/pi/autospin/constants.yaml
scp autospin/config/devices.yaml pi@192.168.50.2:/home/pi/autospin/config/devices.yaml
scp autospin/config/hardware.yaml pi@192.168.50.2:/home/pi/autospin/config/hardware.yaml
```

如果树莓派缺少 `config` 目录，先创建：

```powershell
ssh pi@192.168.50.2 "mkdir -p /home/pi/autospin/config"
```

不要在未备份和核对的情况下覆盖树莓派上已经标定的
`config/coordinates.yaml`。

### 2.3 SCP 递归上传中断

如果 `scp -r` 反复在同一个文件处出现
`client_loop: send disconnect: Connection reset`，先检查树莓派空间：

```bash
df -h /home/pi
df -i /home/pi
```

再从 Windows 将 `src` 打成一个压缩包传输，减少大量小文件的 SCP 会话操作：

```powershell
tar -czf autospin-src.tar.gz -C autospin src
scp autospin-src.tar.gz pi@192.168.50.2:/home/pi/autospin/autospin-src.tar.gz
ssh pi@192.168.50.2 "tar -xzf /home/pi/autospin/autospin-src.tar.gz -C /home/pi/autospin"
```

解压后验证适配器文件和 Python 导入：

```bash
test -f src/hardware/autospinmotor_adapters/linearstage_adapter.py && echo adapter-file-OK
python3 -c "from src.hardware.autospinmotor_adapters import HeaterControllerAdapter, LinearStageControllerAdapter, PipetteControllerAdapter, SpinMotorControllerAdapter; print('adapters OK')"
```

若单文件上传也会断开，检查 SSH、欠压和内核日志：

```bash
sudo journalctl -u ssh -n 50 --no-pager
vcgencmd get_throttled
dmesg --level=err,warn | tail -n 50
```

### 2.4 安装依赖与 mock 检查

在树莓派上执行：

```bash
ssh pi@192.168.50.2
cd ~/autospin
source .venv/bin/activate
python3 -m pip install -r requirements.txt
python3 tools/run_webserver.py --mock --host 127.0.0.1 --port 8800
```

mock 模式不会打开串口或操作真实硬件。首次部署、修改代码/配置或检修后必须先用 mock 检查页面、鉴权和 API。

### 2.5 验证真实硬件构造

停止 mock 服务后执行：

```bash
python3 -c "from src.webapp.registry import DeviceRegistry; r=DeviceRegistry.from_config(); print(r.mock, bool(r.gantry), bool(r.relay), bool(r.gripper), bool(r.heater), bool(r.spincoater), bool(r.pipette), bool(r.linear_stage))"
```

预期输出：

```text
False True True True True True True True
```

这一步只装配七个硬件后端，不会主动连接串口。若出现
`DeviceRegistry.from_config() will be wired in a later W3 task`，说明树莓派上的
`src/webapp/registry.py` 仍是旧占位版本。若出现
`No module named 'src.hardware.autospinmotor_adapters'`，说明 `src` 没有完整上传。

### 2.6 启动真实 Web 服务

在树莓派保持以下命令运行：

```bash
ssh pi@192.168.50.2
cd ~/autospin
source .venv/bin/activate
python3 tools/run_webserver.py --host 127.0.0.1 --port 8800
```

正常启动会显示：

```text
Bearer token: <本次随机生成的 token>
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8800
```

服务启动只创建后端对象；实际串口连接在网页中点击各设备“连接”后发生。

### 2.7 从 Windows 打开网页

服务绑定树莓派 `127.0.0.1` 时，Windows 不能直接访问树莓派的该回环地址。保持服务器终端运行，再打开一个 Windows PowerShell 建立 SSH 隧道：

```powershell
ssh -N -L 8800:127.0.0.1:8800 pi@192.168.50.2
```

输入密码后窗口没有输出属于正常现象，使用期间不要关闭。随后在 Windows 浏览器打开：

```text
http://127.0.0.1:8800
```

如果 Windows 本地 8800 端口已占用，可改用本地 8801：

```powershell
ssh -N -L 8801:127.0.0.1:8800 pi@192.168.50.2
```

此时浏览器打开 `http://127.0.0.1:8801`。

也可以让服务监听整个局域网：

```bash
python3 tools/run_webserver.py --host 0.0.0.0 --port 8800
```

然后访问 `http://192.168.50.2:8800`。该方式会把控制 API 暴露到局域网，安全性低于 SSH 隧道，仅可在可信隔离网络内使用。

### 2.8 Bearer token

服务每次启动都会使用安全随机数生成一个新的 Bearer token。它是本次 Web 服务的临时访问密码，不是 OpenAI token 或 API Key，不会调用 OpenAI，也不会消耗模型额度、账户余额或其他付费资源。

将服务器终端显示的 token 填入网页顶部“访问 token”输入框。网页之后会在控制请求中发送：

```http
Authorization: Bearer <本次 token>
```

Token 只保存在当前浏览器的本地存储中。服务重启后旧 token 失效，必须输入新 token。任何能访问服务器并持有 token 的人都可能调用硬件控制 API，因此不要公开、写入代码仓库或长期复用 token；发生泄露时重启 Web 服务即可作废旧 token。

## 3. 页面全局状态

页面顶部状态栏提供：

- 服务连接状态和状态流连接状态。
- 状态快照序号 `SEQ`。
- 当前占用操作、设备/动作名称及已运行时间。
- 全局“紧急停止”按钮。

设备状态和操作进度通过服务器发送事件（SSE）实时更新。连接中断后网页会自动尝试重连。页面底部“事件日志”记录本次浏览器会话中的连接、操作完成、失败、急停等事件；刷新页面后不会作为永久运行记录保留。

普通硬件动作进入统一操作队列，同一时间只执行一个受控操作。龙门架和滑台的“立即停止”以及全局急停属于安全动作，可绕过普通队列直接下发。

## 4. 全局紧急停止

点击右上角“紧急停止”后，网页调用全局急停接口，并在“最近急停结果”中逐项显示各设备的停止结果。

夹爪的急停策略是释放，即继电器 CH1 关闭、夹爪张开。系统没有夹持力、位置或样品在位反馈，操作员必须现场确认样品状态。

急停后按以下顺序恢复：

1. 排除物理危险，检查样品、枪头、各轴和设备。
2. 确认龙门架位置、状态和限位开关。
3. 按需重新连接设备。
4. 执行 Alarm 恢复并重新归零。
5. 使用 mock、仅校验或低风险动作复查后，再继续实验。

## 5. 设备面板

### 5.1 龙门架

网页可显示 X/Y/Z 位置、机器状态、归零状态和 Pn 限位状态，并提供：

- 连接、断开和三轴归零。
- X/Y/Z 六向点动，可选择步长和进给速度。
- 输入 XYZ 进行绝对移动。
- “仅校验”目标坐标，不执行运动。
- 归零诊断和 Alarm 恢复。
- 释放或闭合 Z 制动。
- 绕过普通操作队列的立即停止。

绝对移动、点动和 recipe 中的龙门架目标均由后端再次校验软限位。当前实际上下限以
`config/hardware.yaml` 中的 `gantry.soft_limits` 为准，不能只依赖网页输入框。

服务端另保留 `/api/gantry/grbl-settings` 用于读取并检查 GRBL 设置，但当前页面没有对应按钮，需要通过受鉴权的 API 客户端调用。

### 5.2 加热台

网页可连接/断开加热台、显示 PV 和 SV、设置目标温度 SV，并可主动读取 PV。SV 上限由运行配置提供，后端会再次进行范围校验。

### 5.3 旋涂台

网页可连接/断开旋涂台、设置目标 RPM 并启动、分别设置启动加速度和
正常停止减速度、选择停止时是否制动，以及读取故障位。页面显示目标转速、
加减速设置、运行状态和故障位。普通停止先用软件斜坡降到零转速，全局急停则
绕过斜坡立即制动。

旋涂台没有角度反馈，因此不能保证在指定角度定向停转。

### 5.4 移液器

网页可连接/断开移液器、归零、按 µL 输入体积执行吸液或排液，以及退枪头。页面显示柱塞位置、归零状态和枪头状态。最大体积来自运行配置并由后端校验。

### 5.5 直线滑台

网页可连接/断开滑台、归零、输入绝对位置移动和立即停止。页面显示当前位置、归零状态和设备标志位。移动范围来自硬件配置。

### 5.6 继电器

网页开放继电器 CH3 至 CH8 的 ON/OFF 控制，并显示各通道状态。每个通道可填写用途备注，备注仅保存在当前浏览器中。

CH1 和 CH2 不开放为通用网页开关：CH1 由夹爪占用，CH2 由 Z 制动占用，防止通用继电器操作绕过专用设备逻辑。

### 5.7 夹爪

夹爪通过继电器 CH1 控制，网页提供“夹紧”和“张开”，并显示当前指令状态。夹爪没有位置、夹持力或样品检测反馈，动作后必须人工确认。

## 6. 程序录制与重放

“程序”面板用于记录成功完成的网页手动动作，并保存为可重放程序：

1. 输入程序名称并点击“开始录制”。
2. 在设备面板依次执行本轮动作。
3. 点击“停止录制并保存”。
4. 在程序列表中检查名称、步数、预计时长和运动摘要。
5. 确认设备与样品状态后点击“重放”。

列表支持查看、重放和删除单个程序。重放时页面显示当前步骤和完成进度；“中止重放”可请求停止，等待步骤也可以被中止打断。程序文件保存在
`runtime/routines/`。

录制程序适合复用网页手动动作，但不等同于 `recipes/*.json` 的完整实验 recipe。网页当前不支持从中断点恢复重放，也没有持久化运行历史；事件日志只保留当前页面会话。

## 7. 实验构建器

“实验构建器”生成由当前 `autospin` 服务直接执行的多轮 JSON recipe。当前页面支持：

- 选择已注册的单轮模板。
- 设置总轮数，范围为 1 至 100。
- 设置每个工艺组包含的轮数。
- 覆盖热台停留时间；留空时使用模板默认值。
- 设置实验名称。
- 指定单轮输入 recipe 路径。
- 设置输出 recipe 文件名。
- Dry-run 展开并预览完整步骤摘要。
- 校验龙门架目标、模板参数和 recipe 结构。
- 人工确认后保存单个 recipe 文件。

点击“Dry-run 预览”不会写文件。预览区会显示总步骤数、龙门架目标数、移液/旋涂动作数、体积与热台停留摘要、部分展开步骤，以及 mock 和真实运行命令。

确认预览和现场条件后，点击“确认并保存 recipe”。输出文件名会经过安全化处理，并写入
`recipes/`。可用环境变量 `AUTOSPIN_RECIPES_DIR` 覆盖该目录；当前实际路径可在
`GET /api/config/runtime` 的 `storage.recipes_dir` 查看。

推荐执行顺序：

```bash
cd ~/autospin
source .venv/bin/activate
python3 tools/run_webserver.py --host 127.0.0.1 --port 8800
```

在网页中先执行“运行已保存 recipe（Dry-run）”，检查完成后再执行“正式运行已保存
recipe”。两种运行均由 `/api/experiments/multi-round/execute` 接口完成，不再调用同级
旧项目脚本。

mock 必须完成坐标、枪头、样品/液体、真空、夹爪、热台、旋涂台和急停检查后，才能真实运行。

当前网页构建器尚未提供样品起始编号/样品位映射、逐步骤编辑 Spin RPM/时间、逐步骤编辑移液体积/延迟、恢复执行和持久化运行记录。这些属于后续扩展，不应视为当前已实现功能。

服务端仍保留 `/api/routines/generate-multi-round` 兼容接口，可按轮复制一个已录制程序，并对其中龙门架 `move_to` 步骤应用每轮 XYZ 偏移。当前页面已使用 recipe 实验构建器，不再提供该旧生成器的表单。

## 8. 工艺坐标

当前静态页面没有坐标编辑器；服务端保留了受验证的
`GET/PUT /api/config/coordinates`。日常标定应编辑唯一真值文件
`config/coordinates.yaml`，并使用 `src.coordinates.load_coordinates()` 校验。

每个可执行坐标点都必须位于龙门架软限位内。工艺坐标描述实验工位位置，
不会替代机器限位或软件安全限位。服务在启动时加载坐标，修改或上传后
必须重启 Web/自动化进程，仅刷新浏览器无效。

如果在其他电脑修改了坐标文件，可上传到树莓派：

```powershell
cd D:\study\pythonlearning\study\AutoSpinmotorSystem
scp -O config\coordinates.yaml pi@192.168.50.2:/home/pi/autospin/config/coordinates.yaml
```

完整的修改、校验、备份、上传和重启流程见
`docs/guides/coordinates-and-spin-deceleration.md`。

## 9. 配置驱动的输入范围

网页启动后读取 `/api/config/runtime`，根据后端配置设置输入范围，不在 HTML 中维护另一套固定值：

- 龙门架 XYZ：`config/hardware.yaml` 中的 `gantry.soft_limits`。
- 旋涂转速：`spincoater.max_rpm`。
- 加热台 SV：`heater.sv_max_c`。
- 移液体积：`pipette.max_volume_ul`。
- 滑台位置：配置的最小值、最大值或行程。

浏览器限制只用于减少误输入，所有关键参数仍会在服务器和设备控制层再次校验。

## 10. 鉴权与 API 行为

除健康检查和网页静态资源外，控制 API 使用 Bearer token 鉴权。网页会自动在请求和 SSE 状态流中附带当前 token。Token 错误或过期时，页面会显示未授权状态并停止正常控制。

多数设备动作返回操作 ID，由网页持续显示执行中、成功或失败状态。设备正忙时，新普通操作会被拒绝，并显示当前占用操作。这样可以避免多个浏览器动作同时争用硬件。

主要网页接口包括：

| 功能 | 接口 |
| --- | --- |
| 健康检查、实时状态 | `/api/health`、`/api/status`、`/api/status/stream` |
| 当前操作与操作结果 | `/api/operations/current`、`/api/operations/{operation_id}` |
| 全局急停 | `/api/estop` |
| 龙门架 | `/api/gantry/*` |
| 加热台、旋涂台、移液器 | `/api/heater/*`、`/api/spincoater/*`、`/api/pipette/*` |
| 滑台、继电器、夹爪 | `/api/linearstage/*`、`/api/relay/*`、`/api/gripper/*` |
| 程序录制、列表、重放和删除 | `/api/routines/*` |
| 实验构建器 | `/api/experiment-builder/defaults`、`/api/experiment-builder/recipe` |
| 运行配置和工艺坐标 | `/api/config/runtime`、`/api/config/coordinates` |

其中 `/api/gantry/grbl-settings` 和 `/api/routines/generate-multi-round` 是当前服务端保留的 API-only 功能，网页界面没有直接操作控件。

## 11. 与之前网页端的差异

这里的“之前网页端”包括早期 `web_control/index.html` 的 Web Serial 页面，以及
`AutoSpinmotorSystem/web_control/server.py` 提供的实验性单体服务。

### 11.1 架构差异

| 项目 | 之前网页端 | 当前网页端 |
| --- | --- | --- |
| 硬件连接 | 浏览器 Web Serial，或单体 server 直接持有控制器 | 树莓派服务通过 `DeviceRegistry`、语义后端和已验证适配器统一管理 |
| 硬件配置 | 浏览器可提交串口名称，参数分散在页面/脚本 | `constants.yaml`、`hardware.yaml`、`devices.yaml` 集中管理 |
| 支持设备 | 主要围绕三轴、继电器、移液和 A/Z2 | 龙门架、加热台、旋涂台、移液器、滑台、继电器、夹爪七设备 |
| 状态更新 | 手动刷新、轮询或浏览器解析串口文本 | SSE 实时设备快照、序号、当前操作和自动重连 |
| 并发控制 | 页面级 busy，多个客户端缺少统一仲裁 | 服务端统一操作门控，同一时间只允许一个普通硬件操作 |
| 安全校验 | 页面参数和旧控制器各自处理 | 浏览器范围提示、API 模型校验、后端软限位三层检查 |
| 鉴权 | 无统一访问 token | 每次启动生成临时 Bearer token |
| 急停 | 发送单条停机命令，结果反馈有限 | 全局急停直达多设备，并逐项返回 `EstopReport` |
| 自动化 | 以手动控制和位置记录为主 | 程序录制/重放、中止、实验 recipe 构建和 Dry-run |
| 可维护性 | HTML 中混合界面、协议和状态逻辑 | 前端、API、设备后端、驱动和配置分层 |

### 11.2 主要改进

1. **不再依赖浏览器直接访问串口。** Web Serial 页面受浏览器兼容性、USB 授权和客户端机器限制；当前模式由树莓派统一持有硬件，Windows 只负责显示和下发语义操作。
2. **设备能力显著扩展。** 当前页面统一显示七设备状态，并提供加热、旋涂、夹爪、滑台、继电器保护通道等专用控制。
3. **安全边界更明确。** CH1/CH2 不再作为普通继电器开放；龙门架目标受配置软限位保护；急停、龙门架停止和滑台停止可绕过普通队列。
4. **避免并发争用硬件。** 服务端操作门控会拒绝与当前操作冲突的新请求，并把操作 ID、执行时间和结果反馈给页面。
5. **配置成为唯一参数来源。** 网页输入上限由运行配置读取，后端使用同一配置再次验证，减少页面数值与真实设备能力不一致。
6. **状态与错误更可观察。** SSE 状态流、设备快照、当前操作、重放进度、事件日志和急停分项报告替代了旧页面较粗粒度的文本日志。
7. **支持可复用流程。** 成功的手动动作可录制、保存、查看、重放和中止；实验构建器可以展开、校验并保存多轮完整 recipe。
8. **部署访问更安全。** 推荐只监听树莓派回环地址，通过 SSH 隧道访问；Bearer token 防止同一网络中的未授权请求直接控制设备。
9. **测试与演练路径更完整。** Web 服务有纯 mock 组合根，完整实验 recipe 还支持 `--mock --time-scale 0`，可以在真实动作前分别验证界面和实验步骤。

### 11.3 功能对应关系

| 之前网页功能 | 当前网页对应功能 |
| --- | --- |
| `status` / 刷新状态 | SSE 设备状态和操作状态 |
| `connect/disconnect` | 各设备面板分别连接/断开 |
| `home` | 龙门架归零 |
| `move_abs` | 龙门架绝对 XYZ 移动 |
| `move_rel/manual_jog` | 六向定步长点动 |
| `dry_run` | 龙门架“仅校验” |
| `halt` | 龙门架“立即停止”和全局急停 |
| `recover` | Alarm 恢复 |
| `homing_diagnostics` | 归零诊断 |
| A/Z2 直线运动 | 独立滑台面板 |
| `z_brake` | Z 制动专用控制 |
| 移液动作 | 移液器面板 |
| 旋涂动作 | 旋涂台面板 |
| `gripper` | 夹爪面板 |
| `record_position` | 工艺坐标 JSON 编辑器 |

### 11.4 尚未原样迁移或仍有限制的能力

- 旧页面的 XY 平面画布、轨迹可视化和 Z 高度条未保留；当前页面更侧重真实状态、操作结果和多设备总览。
- 旧页面的 A/Z2 `0/25/50/75/100/125` 快捷预设及“将当前位置声明为预设”没有原样保留；当前滑台使用归零和绝对位置输入。
- 旧页面移液液体检测掩码没有作为当前网页控件开放。
- 当前程序重放支持中止，但尚不支持断点恢复。
- 当前事件日志是浏览器会话日志，不是持久化审计记录。
- 实验构建器尚未提供样品位映射和每一步 RPM、移液参数的自由编辑。

因此，当前网页在设备覆盖、安全、并发、配置一致性和自动化方面有明显提升，但不是旧页面每个快捷操作的逐按钮复制。涉及旧 A/Z2 预设或液体检测的工作流，在真实实验切换前需要确认新的等价操作方式。

## 12. 相关文件

- 服务入口：`tools/run_webserver.py`
- 应用创建：`src/webapp/app.py`
- 网页前端：`src/webapp/static/`
- 设备接口：`src/webapp/routes_devices.py`
- 龙门架接口：`src/webapp/routes_gantry.py`
- 程序接口：`src/webapp/routes_routines.py`
- 配置与实验构建器接口：`src/webapp/routes_configuration.py`
- 硬件配置：`config/hardware.yaml`
- 工艺坐标：`config/coordinates.yaml`
- 已录制程序：`runtime/routines/`
- 完整实验运行说明：`EXPERIMENT_RUNBOOK.md`
