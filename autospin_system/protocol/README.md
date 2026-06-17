# 固定模板协议执行层说明

本文档说明 `AutoSpinmotorSystem/protocol/` 新增代码的作用、执行流程、模板格式和后续扩展方式。当前版本不接入 LLM，而是先用固定模板模拟“自然语言实验描述 -> 机器可执行流程”的完整链路。

## 目标

这层代码的目标是把实验意图先变成一个受控的结构化协议，再交给机器执行。它不是让自然语言或 LLM 直接控制硬件，而是采用更安全的中间层：

```text
固定实验模板
-> Pydantic 协议对象
-> 协议验证
-> dry-run 模拟预览
-> Maestro 任务编译
-> mock 或真实硬件执行
```

后续接入 LLM 时，只需要让 LLM 生成同样格式的 JSON，后面的验证、模拟、编译和执行逻辑都可以继续复用。

## 新增文件职责

| 文件 | 作用 |
|---|---|
| `schema.py` | 定义协议格式，例如 `MoveSample`、`DispenseLiquid`、`SpinCoat`、`Anneal`。 |
| `validator.py` | 检查协议是否安全、合法、硬件可执行。 |
| `compiler.py` | 把协议操作编译成 `Maestro.run_task()` 能执行的任务字典。 |
| `simulator.py` | 把协议渲染成 dry-run 文本，方便真实执行前人工检查。 |
| `templates.py` | 保存固定实验模板，当前包含 `perovskite_basic` 和 `spin_only_test`。 |
| `units.py` | 统一单位转换，例如 `min -> s`、`mL -> uL`。 |
| `inventory.py` | 读取试剂库存文件 `config/reagents.yaml`。 |
| `__init__.py` | 对外暴露常用函数。 |
| `../run_template_protocol.py` | 命令行入口，用来执行模板协议。 |

## 协议格式

一个完整协议由 `sample_id`、可选 `description` 和 `operations` 组成：

```json
{
  "sample_id": "sample_001",
  "description": "Spin coat a perovskite film.",
  "operations": [
    {
      "name": "MoveSample",
      "from": "storage_tray",
      "to": "spin_center"
    },
    {
      "name": "DispenseLiquid",
      "liquid": "perovskite_precursor",
      "volume": {"quantity": 80, "unit": "uL"},
      "target": "spin_center"
    },
    {
      "name": "SpinCoat",
      "steps": [
        {
          "speed": {"quantity": 1000, "unit": "rpm"},
          "duration": {"quantity": 10, "unit": "s"}
        },
        {
          "speed": {"quantity": 4000, "unit": "rpm"},
          "duration": {"quantity": 30, "unit": "s"}
        }
      ],
      "timed_events": [
        {
          "at": {"quantity": 22, "unit": "s"},
          "operation": {
            "name": "DispenseLiquid",
            "liquid": "antisolvent",
            "volume": {"quantity": 150, "unit": "uL"},
            "target": "spin_center"
          }
        }
      ]
    }
  ]
}
```

注意：当前建议模板中只使用 ASCII 单位，例如 `uL`、`mL`、`s`、`min`、`rpm`、`C`。不要使用特殊的微升符号或摄氏度符号，避免 Windows 终端编码问题。

## 当前支持的操作

| 操作 | 说明 | 编译后的 Maestro 任务 |
|---|---|---|
| `MoveSample` | 移动样品位置。目前调用 `Maestro.transfer()`，仍是占位日志逻辑。 | `move_sample` |
| `DispenseLiquid` | 移液枪滴加液体。 | `dispense_liquid` |
| `SpinCoat` | 多段旋涂曲线，可包含定时滴加事件。 | `spincoat` |
| `Anneal` | 热板退火。mock 下不等待真实退火时间。 | `anneal` |
| `Wait` | 等待。mock 下不等待。 | `wait` |
| `Measure` | 表征占位，用于记录人工测试或后续扩展。 | `measure` |

## 验证规则

`validator.py` 当前会检查：

1. 操作位置是否存在于 `system_config.yaml` 或逻辑别名中。
2. 液体名称是否存在于 `config/reagents.yaml`。
3. 移液体积是否超过移液枪最大量程。
4. 旋涂转速是否超过电机最大转速。
5. 旋涂时间是否为正。
6. 定时滴加事件是否发生在旋涂总时长内。
7. 退火温度单位和时间单位是否合法。
8. `Measure` 目前只是占位，会给出 warning。

## 试剂库存

试剂库存位于：

```text
AutoSpinmotorSystem/config/reagents.yaml
```

示例：

```yaml
liquids:
  perovskite_precursor:
    display_name: "Perovskite precursor solution"
    location: "precursor_vial"
    max_volume_ul: 2000

  antisolvent:
    display_name: "Antisolvent"
    location: "antisolvent_vial"
    max_volume_ul: 5000
```

新增模板液体前，应先把液体加入这里。否则验证器会拒绝执行。

## 如何运行

只做 dry-run，不执行 Maestro：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic
```

执行 mock 模式：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic --execute
```

运行短旋涂测试模板：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template spin_only_test --execute
```

真实硬件执行需要显式加 `--real`，并输入确认字符串：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic --execute --real
```

脚本会提示输入：

```text
EXECUTE
```

没有输入这个确认字符串就不会执行真实硬件。

## 如何新增模板

在 `templates.py` 的 `TEMPLATES` 字典中新增一个键，例如：

```python
"my_new_recipe": {
    "sample_id": "sample_002",
    "description": "My new spin-coating recipe.",
    "operations": [
        {"name": "MoveSample", "from": "storage_tray", "to": "spin_center"},
        {
            "name": "SpinCoat",
            "steps": [
                {
                    "speed": {"quantity": 2000, "unit": "rpm"},
                    "duration": {"quantity": 20, "unit": "s"},
                }
            ],
        },
    ],
}
```

然后运行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template my_new_recipe
```

建议新增模板后先运行 dry-run，再运行 mock，最后才考虑真实硬件。

## 和论文方法的对应关系

论文中的核心思想是：

```text
自然语言
-> JSON 格式单元操作
-> 语法/物理量/试剂/平台验证
-> 机器人执行
```

当前实现对应为：

```text
固定模板
-> ExperimentProtocol
-> validate_protocol()
-> simulate_protocol()
-> compile_protocol()
-> Maestro.run_task()
```

也就是说，我们现在先绕开 LLM，把后半段的“结构化协议到机器执行”打通。等这部分稳定后，再把固定模板替换为 LLM 生成的 JSON。

## 后续建议

1. 把 `MoveSample` 从日志占位补成真实夹爪/龙门动作。
2. 增加 `AspirateLiquid`，让吸液和吐液都进入协议层。
3. 把 `Measure` 扩展为膜厚、PL、吸收光谱或人工输入结果。
4. 给 `templates.py` 增加更多真实实验模板。
5. 增加协议 JSON 文件读取模式，而不只使用内置模板。
6. 接入 LLM 时，要求 LLM 只输出 `schema.py` 中定义的协议 JSON。

## 安全提醒

真实硬件执行前请确认：

1. `system_config.yaml` 中串口号正确。
2. 样品、移液枪、吸头、旋涂台、热板位置已经校准。
3. `reagents.yaml` 中液体名称和实际瓶位一致。
4. 先运行 dry-run 和 mock。
5. 确认电机、热板、龙门、移液枪都处于安全初始状态。

## 低层动作序列多轮生成器

除了上面的高级 `ExperimentProtocol`，当前还支持一种更贴近硬件调试的低层 JSON：

```json
{
  "operation": "MoveGantry",
  "params": {"x": -34.0, "y": -364.0, "z": -156.0}
}
```

这种格式由 `example_experiment.py` 直接执行。为了避免手写 18 轮重复 JSON，新增了：

```text
protocol/sequence_generator.py
generate_multi_round_sequence.py
```

它们的职责是：读取单轮模板 `examples/gantry_gripper_spin_hotplate_sequence.json`，根据轮次自动替换玻璃片坐标和取液/盖子坐标，生成完整多轮 recipe。

### 坐标规则

玻璃片/样品位置：

```python
sample_rows = [
    [0, 1, 2, 3, 4],
    [0, 1, 3, 4],
    [0, 1, 3, 4],
    [0, 1, 2, 3, 4],
]

sample_x = -34.0 - 37.25 * col
sample_y = -364.0 - 24.33 * row
z_pick = -156.0
z_lift = -50.0
```

取液/盖子位置：

```python
liquid_index = (round_no - 1) // 2
liquid_group = liquid_index // 6
liquid_inner = liquid_index % 6

liquid_x = -11.0 - 38.4 * liquid_group
liquid_y = -147.0 + 19.2 * liquid_inner
liquid_z = -145.0
```

每轮替换单轮模板中的 4 个位置：

```text
ops[3]  -> 取液/盖子位置
ops[7]  -> 取液/盖子位置
ops[9]  -> 玻璃片 z_pick 位置
ops[11] -> 玻璃片 z_lift 位置
```

其余操作保持单轮模板不变。

### 生成多轮 JSON

默认生成 18 轮：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py
```

如果希望运行脚本后根据提示一次性输入固定模板和坐标规则，可以使用交互模式：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --interactive
```

脚本会先一次性打印所有字段含义、默认值和一个 JSON 示例。随后你只需要粘贴一个 JSON 对象，里面只写需要更改的字段；缺少的字段自动使用默认值。如果直接按 Enter，则全部使用默认值。

例如只改轮数和输出文件：

```json
{
  "rounds": 12,
  "output": "AutoSpinmotorSystem/examples/my_12_rounds.json"
}
```

多行输入时，以单独一行 `END` 结束：

```text
> {
  "rounds": 12,
  "output": "AutoSpinmotorSystem/examples/my_12_rounds.json"
}
END
```

`sample_rows` 支持两种写法：

```json
{
  "sample_rows": [[0, 1, 2, 3, 4], [0, 1, 3, 4], [0, 1, 3, 4], [0, 1, 2, 3, 4]]
}
```

或紧凑字符串：

```json
{
  "sample_rows": "0,1,2,3,4;0,1,3,4;0,1,3,4;0,1,2,3,4"
}
```

字段含义：

```text
rounds: 生成轮数；当前样品布局最多 18。
input: 单轮低层 operation/params JSON 模板。
output: 自动生成的多轮机器可执行 JSON。
experiment_name: 输出 JSON 中的实验名；不填则沿用 input 中的 experiment_name。
sample_rows: 玻璃片布局，每一行有哪些列号。
sample_base_x / sample_base_y: 第 1 轮玻璃片位置。
sample_dx / sample_dy: 玻璃片 X/Y 间距；生成时沿负方向偏移。
sample_z_pick: 夹取玻璃片时的 Z。
sample_z_lift: 抬起玻璃片后的 Z。
liquid_base_x / liquid_base_y: 第 1 个取液/盖子位置。
liquid_dx_group: 取液位置组间 X 间距；生成时沿负方向偏移。
liquid_dy_inner: 取液位置组内 Y 间距；生成时沿正方向偏移。
liquid_z: 取液/盖子位置的 Z。
liquid_pick_index: 单轮模板中第几个 operation 替换为取液/盖子位置。
liquid_return_index: 单轮模板中第几个 operation 替换为返回取液/盖子位置。
sample_pick_index: 单轮模板中第几个 operation 替换为玻璃片夹取位置。
sample_lift_index: 单轮模板中第几个 operation 替换为玻璃片抬起位置。
```

如果只是想确认交互模式但全部使用默认值，可以运行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --interactive --accept-defaults
```

输出文件：

```text
AutoSpinmotorSystem/examples/gantry_gripper_spin_hotplate_sequence_18_rounds.json
```

自定义轮数、输入和输出：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --rounds 12 --input AutoSpinmotorSystem\examples\gantry_gripper_spin_hotplate_sequence.json --output AutoSpinmotorSystem\examples\my_12_rounds.json
```

注意：当前样品位置表最多支持 18 轮，超过 18 会报错。

### mock 执行多轮 JSON

快速软件路径验证：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\example_experiment.py AutoSpinmotorSystem\examples\gantry_gripper_spin_hotplate_sequence_18_rounds.json --mock --time-scale 0
```

接近真实时序的 mock 验证：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\example_experiment.py AutoSpinmotorSystem\examples\gantry_gripper_spin_hotplate_sequence_18_rounds.json --mock
```

真实硬件执行前，需要人工检查生成 JSON 中所有 `MoveGantry` 坐标，确认不会越界、碰撞或夹持错误。
## 一键生成多轮 JSON 与可选 mock 检查

`generate_multi_round_sequence.py` 现在可以作为一键入口使用。直接运行时会先询问是否修改轮数、输出文件、样品排布、取液坐标、替换索引等参数：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py
```

如果回答 `n` 或直接回车，会使用默认参数生成 18 轮 JSON。

如果回答 `y`，会进入参数修改界面。你只需要粘贴要修改的字段，例如：

```json
{
  "rounds": 12,
  "output": "AutoSpinmotorSystem/examples/my_12_rounds.json"
}
```

如果不想出现启动询问，可以使用：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --no-start-prompt
```

生成后立即进行 mock 检查：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --run-after-generate
```

默认 post-generation run 使用 mock 模式，并且 `time_scale=0`，所以会快速检查任务顺序和软件路径，不等待真实实验时间。

如果你是从 IDE 里直接点运行，不想输入命令行参数，可以修改 `generate_multi_round_sequence.py` 顶部开关：

```python
PROMPT_FOR_SETTINGS_ON_START = True
AUTO_RUN_AFTER_GENERATE = True
AUTO_RUN_MOCK = True
AUTO_RUN_TIME_SCALE = 0.0
```

含义：

- `PROMPT_FOR_SETTINGS_ON_START=True`：启动时询问是否修改生成参数。
- `AUTO_RUN_AFTER_GENERATE=True`：生成 JSON 后自动运行检查。
- `AUTO_RUN_MOCK=True`：使用 mock 模式，不连接真实硬件。
- `AUTO_RUN_TIME_SCALE=0.0`：跳过等待时间，快速完成软件路径检查。

如果把 `AUTO_RUN_MOCK` 改成 `False`，脚本会视为真实硬件执行，并要求输入 `EXECUTE` 才会继续。

临时关闭生成后的自动运行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\generate_multi_round_sequence.py --no-run-after-generate
```

## LLM-friendly protocol JSON

For natural-language parsing, prefer the public `operation`/`params` shape.
The user or LLM should describe experimental intent only. Do not ask for
gantry coordinates, sample layout rows, liquid slot spacing, or template
operation indexes in this layer.

Example:

```json
{
  "experiment_name": "FA-Cs PVSK film preparation",
  "operations": [
    {
      "operation": "DispenseLiquid",
      "params": {
        "source": "FA-Cs precursor",
        "target": "substrate_center",
        "volume_uL": 80
      }
    },
    {
      "operation": "SpinCoat",
      "params": {
        "steps": [
          {"rpm": 1000, "time_s": 10},
          {"rpm": 4000, "time_s": 35}
        ],
        "antisolvent": {
          "enabled": true,
          "source": "chlorobenzene",
          "volume_uL": 250,
          "drop_at_remaining_time_s": 10
        }
      }
    },
    {
      "operation": "Anneal",
      "params": {
        "temperature_C": 100,
        "time_min": 30
      }
    }
  ]
}
```

Use `protocol_from_llm_json(raw)` to convert this JSON into
`ExperimentProtocol`, then run `validate_protocol`, `simulate_protocol`, and
`compile_protocol` as usual. Use `runner_recipe_from_llm_json(raw)` when the
same LLM JSON should be exported to `example_experiment.py` style
machine-executable JSON.

`antisolvent.enabled=false` means no antisolvent dispense is generated. When it
is true or omitted, `source` and `volume_uL` are required. The field
`drop_at_remaining_time_s` means "drop this many seconds before the whole spin
profile ends"; for a 45 s spin with value 10, the timed event is compiled at
35 s from spin start.

When exporting to `example_experiment.py` runner JSON, a disabled antisolvent is
written as JSON boolean `false` so the runner will not enter its antisolvent
dispense branch.

## 详细使用方法：从实验语义 JSON 到 mock/真实执行

本模块推荐使用两条入口：

1. 固定模板入口：适合快速验证系统链路，模板写在 `templates.py`。
2. LLM 语义 JSON 入口：适合自然语言实验描述转换，输入使用 `operation` + `params`，不暴露夹爪坐标、取液位置、样品排布和步骤索引。

### 1. 运行已有固定模板

直接运行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py
```

这等价于运行默认模板 `perovskite_basic`：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic
```

只做 dry-run，不执行硬件动作。脚本会依次输出：

```text
Protocol JSON
Validation
Simulation
Compiled Tasks
```

如果要 mock 执行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic --execute
```

如果要真实硬件执行：

```powershell
AutoSpinmotorSystem\.venv\Scripts\python AutoSpinmotorSystem\run_template_protocol.py --template perovskite_basic --execute --real
```

真实执行会要求手动输入确认字符串 `EXECUTE`。

### 2. 修改固定模板中的实验参数

如果只是修改取液量、转速、旋涂时间、退火温度或退火时间，通常只需要改 `templates.py`。

例如修改前驱体滴加量：

```python
{
    "name": "DispenseLiquid",
    "liquid": "perovskite_precursor",
    "volume": {"quantity": 100, "unit": "uL"},
    "target": "spin_center",
}
```

例如修改旋涂程序：

```python
{
    "name": "SpinCoat",
    "steps": [
        {
            "speed": {"quantity": 1000, "unit": "rpm"},
            "duration": {"quantity": 10, "unit": "s"},
        },
        {
            "speed": {"quantity": 4000, "unit": "rpm"},
            "duration": {"quantity": 35, "unit": "s"},
        },
    ],
}
```

例如修改退火：

```python
{
    "name": "Anneal",
    "temperature": {"quantity": 100, "unit": "C"},
    "duration": {"quantity": 30, "unit": "min"},
    "target": "Hotplate1",
}
```

### 3. 使用 LLM 语义 JSON

自然语言描述应先由 LLM 转成下面这种 JSON：

```json
{
  "experiment_name": "FA-Cs PVSK film preparation",
  "operations": [
    {
      "operation": "DispenseLiquid",
      "params": {
        "source": "FA-Cs precursor",
        "target": "substrate_center",
        "volume_uL": 80
      }
    },
    {
      "operation": "SpinCoat",
      "params": {
        "steps": [
          {"rpm": 1000, "time_s": 10},
          {"rpm": 4000, "time_s": 35}
        ],
        "antisolvent": {
          "enabled": true,
          "source": "chlorobenzene",
          "volume_uL": 250,
          "drop_at_remaining_time_s": 10
        }
      }
    },
    {
      "operation": "Anneal",
      "params": {
        "temperature_C": 100,
        "time_min": 30
      }
    }
  ]
}
```

在 Python 中使用：

```python
from AutoSpinmotorSystem.protocol import (
    compile_protocol,
    protocol_from_llm_json,
    runner_recipe_from_llm_json,
    simulate_protocol,
    validate_protocol,
)

protocol = protocol_from_llm_json(raw)
validation = validate_protocol(protocol)

if not validation.valid:
    raise ValueError(validation.errors)

for line in simulate_protocol(protocol):
    print(line)

tasks = compile_protocol(protocol)
runner_recipe = runner_recipe_from_llm_json(raw)
```

其中：

- `protocol_from_llm_json(raw)`：把 LLM JSON 转成 `ExperimentProtocol`。
- `validate_protocol(protocol)`：检查液体、单位、体积、转速、时间和位置是否合法。
- `simulate_protocol(protocol)`：生成给人工确认的 dry-run 文本。
- `compile_protocol(protocol)`：生成 `Maestro.run_task()` 能执行的任务字典。
- `runner_recipe_from_llm_json(raw)`：生成 `example_experiment.py` 能读取的机器可执行 JSON。

### 4. 反溶剂字段写法

需要滴加反溶剂：

```json
"antisolvent": {
  "enabled": true,
  "source": "chlorobenzene",
  "volume_uL": 250,
  "drop_at_remaining_time_s": 10
}
```

不需要滴加反溶剂：

```json
"antisolvent": {
  "enabled": false
}
```

`drop_at_remaining_time_s` 表示距离整个旋涂结束还剩多少秒时滴加。例如两段旋涂总时间是 `10 + 35 = 45 s`，`drop_at_remaining_time_s = 10`，则系统会编译成旋涂开始后 `35 s` 滴加。

### 5. 各模块应该如何修改

| 目标 | 修改位置 |
|---|---|
| 改取液量、转速、时间、温度 | `templates.py` 或 LLM 输入 JSON |
| 新增固定实验模板 | `templates.py` |
| 新增 LLM 友好的液体别名，例如 `DMF:DMSO precursor` | `llm_adapter.py` 的 `LIQUID_ALIASES` |
| 新增 LLM 友好的目标位置别名 | `llm_adapter.py` 的 `TARGET_ALIASES` |
| 新增试剂库存 | `config/reagents.yaml` |
| 新增一种协议操作，例如 `PlasmaClean` | `schema.py`、`validator.py`、`compiler.py`、`simulator.py` |
| 修改安全限制，例如最大体积、最大转速、退火温度范围 | `validator.py` 或 `config/system_config.yaml` |
| 修改任务如何变成 Maestro 可执行字典 | `compiler.py` |
| 修改 dry-run 文本显示方式 | `simulator.py` |
| 新增单位换算，例如 `hour -> s` | `units.py` |

### 6. 新增一种操作的实现顺序

假设要新增 `PlasmaClean`：

1. 在 `schema.py` 中新增 `PlasmaCleanOperation`，定义它有哪些字段。
2. 把 `PlasmaCleanOperation` 加入 `Operation` 联合类型。
3. 在 `validator.py` 中检查功率、时间、目标位置是否合法。
4. 在 `compiler.py` 中把它编译成 Maestro 任务字典。
5. 在 `simulator.py` 中增加 dry-run 显示文本。
6. 在 `templates.py` 或 LLM JSON 中使用这个新操作。

这样做的好处是：LLM 只负责生成实验语义 JSON，真正能不能执行由 `validator.py` 和后续编译层决定，避免自然语言直接控制硬件。
