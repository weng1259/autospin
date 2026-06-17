import time
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from autospin_system.maestro import Maestro

__test__ = False

# 用户定义的实验方案
TEST_PROFILE = [
    {"type": "START", "direction": "forward", "wait": 1.0},
    {"type": "RAMP", "from": 0, "to": 1000, "duration": 3.0},
    {"type": "HOLD", "speed": 1000, "duration": 2.0},
    {"type": "RAMP", "from": 1000, "to": 3000, "duration": 2.0},
    {"type": "HOLD", "speed": 3000, "duration": 2.0},
    {"type": "RAMP", "from": 3000, "to": 0, "duration": 3.0},
    {"type": "STOP"}
]


def run_debug_profile():
    # ── 日志目录 ──────────────────────────────────────────────
    log_dir = os.path.join(os.path.dirname(__file__), "logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"spinmotor_{timestamp}.log")

    # ── 配置 logger ───────────────────────────────────────────
    logger = logging.getLogger("SpinMotorDebug")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    # 控制台输出
    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # 文件输出
    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    logger.info(f"日志文件: {log_path}")
    # ──────────────────────────────────────────────────────────

    maestro = Maestro(use_gantry=False, logger=logger,mock=True) # 模拟mock模式下开启，真实情况下mock=False
    motor = maestro.spincoater

    try:
        motor.unlock()

        for step in TEST_PROFILE:
            stype = step["type"]

            if stype == "START":
                logger.info(f">>> 启动电机: {step['direction']}")
                motor.start(direction=step['direction'])
                time.sleep(step['wait'])

            elif stype == "HOLD":
                logger.info(f">>> 保持转速: {step['speed']} RPM, 持续 {step['duration']}s")
                motor.set_speed(step['speed'])
                start_t = time.time()
                while time.time() - start_t < step['duration']:
                    status = motor.get_status()
                    logger.info(f"实际转速: {status['actual_speed']} RPM")
                    time.sleep(0.5)

            elif stype == "RAMP":
                low = step["from"]
                high = step["to"]
                dur = step["duration"]
                logger.info(f">>> 斜坡变化: {low} -> {high} RPM, 历时 {dur}s")

                start_t = time.time()
                while True:
                    elapsed = time.time() - start_t
                    if elapsed >= dur: break

                    # 线性计算当前瞬时转速
                    current_target = low + (high - low) * (elapsed / dur)
                    motor.set_speed(current_target)

                    actual = motor.get_actual_speed()
                    logger.info(f"斜坡中... 目标: {int(current_target)} | 实际: {actual}")
                    time.sleep(0.2)  # 0.2秒更新一次频率，保证平滑

            elif stype == "STOP":
                logger.info(">>> 停止电机")
                motor.stop()
                motor.lock()

    except Exception as e:
        logger.error(f"调试异常: {e}")
    finally:
        maestro.shutdown()


if __name__ == "__main__":
    run_debug_profile()
