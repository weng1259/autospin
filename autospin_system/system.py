# AutoSpinmotorSystem/system.py
# 基于 PASCAL 架构的核心系统结构

import itertools as itt
from autospin_system.config.hardware_config import CONFIG

from .workers import (
    Worker_GantryGripper,
    Worker_Characterization,
    Worker_Hotplate,
    Worker_SpincoaterLiquidHandler,
    Worker_Storage,
    Worker_HumanOperator
)


# define workers
def generate_workers(maestro=None):
    """实例化所有硬件 Worker(如 3 块加热板、2 个存储盘)并将它们注册到 Maestro 中心 。"""

    if maestro is None:
        kws = dict(planning=True)
    else:
        kws = dict(maestro=maestro, planning=False)

    # 【从 YAML 配置中动态读取默认容量，消除硬编码】
    cap_cfg = CONFIG.get('system', {}).get('default_worker_capacity', {})
    hp_cap = cap_cfg.get('hotplate', 30)
    st_cap = cap_cfg.get('storage', 45)

    gg = Worker_GantryGripper(**kws)
    sclh = Worker_SpincoaterLiquidHandler(**kws)

    hp1 = Worker_Hotplate(capacity=hp_cap, **kws)
    hp1.name = "Hotplate1"
    hp2 = Worker_Hotplate(capacity=hp_cap, **kws)
    hp2.name = "Hotplate2"
    hp3 = Worker_Hotplate(capacity=hp_cap, **kws)
    hp3.name = "Hotplate3"

    st1 = Worker_Storage(capacity=st_cap, initial_fill=st_cap, **kws)
    st1.name = "Tray1"
    st2 = Worker_Storage(capacity=st_cap, initial_fill=st_cap, **kws)
    st2.name = "Tray2"

    cl = Worker_Characterization(**kws)
    ho = Worker_HumanOperator(**kws)

    return {w.name: w for w in [gg, sclh, hp1, hp2, hp3, st1, st2, cl, ho]}


ALL_WORKERS = generate_workers()

ALL_TASKS = {}
"""定义所有任务的元数据，包括 required Worker 类型、预估耗时等协作 Worker 列表。"""

for worker in ALL_WORKERS.values():
    for task, details in worker.functions.items():
        if task not in ALL_TASKS:
            ALL_TASKS[task] = {
                "workers": [type(worker)]
                           + details.other_workers,  # list of workers required to perform task
                "estimated_duration": details.estimated_duration,  # time (s) to complete task
            }

# define transitions
"""定义所有任务之间的过渡任务，包括机械臂、存储盘、加热板、退火器等。"""
TRANSITION_TASKS = {
    Worker_SpincoaterLiquidHandler: {
        Worker_Hotplate: "spincoater_to_hotplate",
        Worker_Storage: "spincoater_to_storage",
        Worker_Characterization: "spincoater_to_characterization",
    },
    Worker_Hotplate: {
        Worker_SpincoaterLiquidHandler: "hotplate_to_spincoater",
        Worker_Storage: "hotplate_to_storage",
        Worker_Characterization: "hotplate_to_characterization",
    },
    Worker_Storage: {
        Worker_SpincoaterLiquidHandler: "storage_to_spincoater",
        Worker_Hotplate: "storage_to_hotplate",
        Worker_Characterization: "storage_to_characterization",
    },
    Worker_Characterization: {
        Worker_SpincoaterLiquidHandler: "characterization_to_spincoater",
        Worker_Hotplate: "characterization_to_hotplate",
        Worker_Storage: "characterization_to_storage",
    },
}

transitions = {
    True: [],  # use GantryGripper
    False: [],  # use HumanOperator for transitions
}

for w1, w2 in itt.permutations(ALL_WORKERS.values(), 2):
    t1, t2 = type(w1), type(w2)
    if Worker_GantryGripper in [t1, t2]:
        continue  # no transition tasks for this worker
    if Worker_HumanOperator in [t1, t2]:
        continue  # no transition tasks for this worker
    if t1 == t2:
        continue  # no transtion between same type (hotplate->hotplate, etc)
    immediate = False
    if Worker_Hotplate in (t1, t2):
        immediate = True  # always get on/off the hotplate at exact time
    if t1 == Worker_SpincoaterLiquidHandler:
        immediate = True  # move off of spincoater ASAP

    transition_name = TRANSITION_TASKS[t1][t2]
    this_transition_ho = None
    this_transition_gg = None

    # For now, we'll use a simplified transition system
    # In a real implementation, we would use roboflo or similar


# default system
def build(use_gantry):
    """系统构建函数。根据布尔值决定是启用机械臂自动化还是人工引导模式 。"""
    return {
        "workers": list(ALL_WORKERS.values()),
        "transitions": transitions[use_gantry],
        "starting_worker": ALL_WORKERS["Tray1"],
        "ending_worker": ALL_WORKERS["Tray1"],
    }
