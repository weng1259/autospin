import logging
from maestro import Maestro

__test__ = False

logging.basicConfig(level=logging.INFO)

if __name__ == "__main__":
    # 传入 mock=True 开启全系统仿真！
    m = Maestro(use_gantry=True, mock=True)

    # 模拟启动实验
    m.start_experiment()

    # 模拟让机械臂归位
    m.idle_gantry()

    # 关闭系统
    m.shutdown()
