"""Live plot utility for the single AI-516 heating stage.

This script reuses HeatingStageController, so register addresses, Modbus
settings, mock behavior, and future controller fixes stay aligned with the
main project.
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

import matplotlib.pyplot as plt

PACKAGE_PARENT = Path(__file__).resolve().parents[2].parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.heating_stage import HeatingStageController


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor the configured AI-516 heating stage.")
    parser.add_argument("--port", default=CONFIG["communication"].get("heating_stage_port", "COM9"))
    parser.add_argument("--target-temp", type=float, default=60.0, help="Target SV in Celsius")
    parser.add_argument("--sample-interval", type=float, default=1.0, help="Sampling interval in seconds")
    parser.add_argument("--mock", action="store_true", help="Use mock controller without opening serial")
    parser.add_argument("--no-write-sv", action="store_true", help="Only read/plot PV and current SV")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    logger = logging.getLogger("HeatingStageLivePlot")
    heater = HeatingStageController(port=args.port, mock=args.mock, logger=logger)

    if not heater.connect():
        print("连接失败：请检查 COM口、485接线、Addr、bAud、AFC")
        return

    print("RS485连接成功")

    csv_name = f"heating_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    if args.no_write_sv:
        actual_sv = heater.read_sv()
        print(f"仅监控模式，当前 SV = {actual_sv:.1f} ℃")
    else:
        heater.write_sv(args.target_temp)
        actual_sv = heater.read_sv()
        print(f"已设置目标温度 SV = {actual_sv:.1f} ℃")

        if abs(actual_sv - args.target_temp) > 0.05:
            print(f"警告：SV 回读值 {actual_sv:.1f} ℃ 与目标 {args.target_temp:.1f} ℃ 不一致")

    times = []
    pvs = []
    svs = []

    start_time = time.time()

    plt.ion()
    fig, ax = plt.subplots()
    line_pv, = ax.plot([], [], label="PV current tem")
    line_sv, = ax.plot([], [], label="SV target tem")

    ax.set_xlabel("Time / s")
    ax.set_ylabel("Temperature / ℃")
    ax.set_title("Heating Stage Temperature Monitor")
    ax.legend()
    ax.grid(True)

    with open(csv_name, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)
        writer.writerow(["time_s", "pv_c", "sv_c"])

        try:
            while True:
                now = time.time() - start_time
                pv = heater.read_pv()
                sv = heater.read_sv()

                times.append(now)
                pvs.append(pv)
                svs.append(sv)

                writer.writerow([f"{now:.1f}", f"{pv:.2f}", f"{sv:.2f}"])
                f.flush()

                print(f"t={now:6.1f}s | PV={pv:6.2f} ℃ | SV={sv:6.2f} ℃")

                line_pv.set_data(times, pvs)
                line_sv.set_data(times, svs)

                ax.relim()
                ax.autoscale_view()

                plt.pause(0.01)
                time.sleep(args.sample_interval)

        except KeyboardInterrupt:
            print("\n用户停止监控")

        finally:
            heater.close()
            print(f"数据已保存到：{csv_name}")


if __name__ == "__main__":
    main()
