#!/usr/bin/env python3
"""DBLS400 旋涂电机分级 bring-up（高速旋转设备，三设备里最危险）。

直接驱动 MotorController（绕过 Maestro 的 SharedRs485DeviceProxy open-use-close），
串口全程保持打开，行为可预测、可单步、可控。

子命令：
  voltage              只读母线电压（电机不转，最安全的第一步）
  spin [RPM] [HOLD_S]  低速真转（默认 100 RPM 保持 3s 后停）

安全门闩：
  - spin 默认只打印计划，不执行；真转需环境变量 SPIN_CONFIRM=1
    （= 确认卡盘装牢/清场/急停物理可达/PM 在场后才设）。
  - RPM > 300 硬拒绝（bring-up 首次只验低速真转+方向+平衡，升速另跑）。
  - SPIN_MOCK=1 走 mock（不开串口），用于逻辑 dry-run。

量纲（DBLS400 485 手册）：
  母线电压(V)   = raw / 4              （额定 24-48V → raw 96-192；欠压点10V/过压点60V）
  实际转速(RPM) = raw * 20 / 极数 = raw * 2.5  （pole_pairs=4 → 极数8；手册5对极例子用×2）
  speed_set(0x8005) 直接写 RPM
  ⚠️ 驱动器最低稳定转速 150 RPM（手册速度范围 150~20000）；设 100 可能转不稳，留意实际反馈。
"""
import os
import sys
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autospin_system.hardware.spin_motor.motor_controller import MotorController, Registers

PORT = os.environ.get("SPIN_PORT", "/dev/autospin_rs485")
MOCK = os.environ.get("SPIN_MOCK") == "1"
CONFIRM = os.environ.get("SPIN_CONFIRM") == "1"
MAX_BRINGUP_RPM = 300
FAULT_STATUS = 0x801B  # 第一字节(低)=故障位, 第二字节(高)=运行状态

# 0x801B 低字节故障位 (DBLS400 手册)
_FAULT_BITS = ["堵转", "过流", "霍尔异常", "母线欠压",
               "母线过压", "电流峰值报警", "保留6", "保留7"]


def decode_fault(value):
    if value is None:
        return "(读取失败)"
    fb = value & 0xFF
    active = [_FAULT_BITS[i] for i in range(8) if fb & (1 << i)]
    run = (value >> 8) & 0xFF
    return f"故障位={active or '无'} 运行状态=0x{run:02X}"


def make_logger():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s",
                        datefmt="%H:%M:%S")
    return logging.getLogger("spin-bringup")


def read_voltage(motor, log):
    data = motor.comm.read_register(Registers.BUS_VOLTAGE, 1)
    if not data:
        log.error("读母线电压失败: read_register 返回 %r (总线不通/设备未上电/RS485未接?)", data)
        return None
    raw = data[0]
    volts = raw / 4.0
    log.info("母线电压: raw=%d (0x%04X) -> %.2f V  [公式 raw/4]", raw, raw, volts)
    return volts


def read_speed(motor, log, rpm_set):
    raw = motor.comm.read_register(Registers.ACTUAL_SPEED, 1)
    fault = motor.comm.read_register(FAULT_STATUS, 1)
    actual = motor.get_actual_speed()
    rawval = raw[0] if raw else None
    fval = fault[0] if fault else None
    log.info("  设定 %d | 实际 %.1f RPM (raw=%s) | 0x801B=%s [%s]",
             rpm_set, actual, rawval,
             f"0x{fval:04X}" if fval is not None else None, decode_fault(fval))
    fault_byte = (fval & 0xFF) if fval is not None else 0
    return actual, fault_byte


def cmd_voltage(motor, log):
    log.info("=== Gate-1: 读母线电压 (电机不转, 最安全) ===")
    v = read_voltage(motor, log)
    if v is None:
        return 1
    if 20.0 <= v <= 52.0:
        log.info("PASS: 母线电压 %.2f V 在合理范围 (额定24-48V) -> 总线通+设备活", v)
    else:
        log.warning("CHECK: 母线电压 %.2f V 超出 24-48V, 核对电源/接线/量纲", v)
    return 0


def cmd_spin(motor, log, rpm, hold_s):
    log.info("=== Gate-2: 低速真转 set=%d RPM hold=%.1fs ===", rpm, hold_s)
    if rpm > MAX_BRINGUP_RPM:
        log.error("拒绝: bring-up 首次真转限 <=%d RPM (传入 %d). 平衡确认后再单独升速.",
                  MAX_BRINGUP_RPM, rpm)
        return 2
    if not CONFIRM and not MOCK:
        log.error("未确认: 真转需 SPIN_CONFIRM=1 "
                  "(确认卡盘装牢/清场/急停物理可达/PM在场). 当前仅打印计划, 未执行.")
        log.info("计划: unlock -> start(forward) -> set_speed(%d) -> 观察%.1fs -> stop -> lock",
                 rpm, hold_s)
        return 0
    v = read_voltage(motor, log)
    if v is None:
        log.error("总线不通, 放弃真转")
        return 1
    log.info("[1/5] unlock 进入就绪态...")
    if not motor.unlock():
        log.error("unlock 失败")
        return 1
    log.info("[2/5] start(forward)...")
    ok, msg = motor.start(direction="forward")
    log.info("      start -> ok=%s msg=%s", ok, msg)
    if not ok:
        return 1
    log.info("[3/5] set_speed(%d)...", rpm)
    ok, msg = motor.set_speed(rpm)
    log.info("      set_speed -> ok=%s msg=%s", ok, msg)
    if not ok:
        log.error("set_speed 失败, 立即停机")
        motor.stop()
        motor.lock()
        return 1
    log.info("[4/5] 观察实际转速 %.1fs (留意方向/振动/平衡)...", hold_s)
    t0 = time.time()
    samples = []
    while time.time() - t0 < hold_s:
        actual, _ = read_speed(motor, log, rpm)
        samples.append(actual)
        time.sleep(0.5)
    log.info("[5/5] 停机 stop()+lock()...")
    motor.stop()
    motor.lock()
    if samples:
        log.info("PASS: 已停机抱闸. 设定 %d, 实际 min=%.1f max=%.1f 末=%.1f",
                 rpm, min(samples), max(samples), samples[-1])
    log.info(">>> 请目视确认转盘完全静止 <<<")
    return 0


def cmd_ramp(motor, log, target, per_hold):
    log.info("=== 升速验证: 分级斜坡到 %d RPM, 每级 hold %.1fs (含0x801B故障自动急停) ===",
             target, per_hold)
    if target > 3000:
        log.error("拒绝: 升速上限 3000 RPM (传入 %d).", target)
        return 2
    levels = [lv for lv in (100, 300, 600, 1000, 1500, 2000, 2500) if lv < target]
    levels.append(target)
    if not CONFIRM and not MOCK:
        log.error("未确认: 升速需 SPIN_CONFIRM=1 (确认卡盘装牢/清场/急停可达/PM在场). 仅打印计划.")
        log.info("升速级: %s -> 顶部hold -> 平滑降速 -> stop+lock", levels)
        return 0
    v = read_voltage(motor, log)
    if v is None:
        log.error("总线不通, 放弃升速")
        return 1
    log.info("[1/4] unlock + start(forward)...")
    if not motor.unlock():
        log.error("unlock 失败")
        return 1
    ok, msg = motor.start(direction="forward")
    if not ok:
        log.error("start 失败: %s", msg)
        return 1
    aborted = False
    peak = 0.0
    log.info("[2/4] 分级升速 %s ...", levels)
    for lv in levels:
        ok, msg = motor.set_speed(lv)
        log.info(">>> 升到 %d RPM (set_speed ok=%s)", lv, ok)
        if not ok:
            log.error("set_speed 失败, 急停")
            aborted = True
            break
        t0 = time.time()
        while time.time() - t0 < per_hold:
            actual, fb = read_speed(motor, log, lv)
            peak = max(peak, actual)
            if fb != 0:
                log.error("!!! 故障位非零 0x%02X [%s], 立即急停 !!!", fb, decode_fault(fb))
                aborted = True
                break
            time.sleep(0.4)
        if aborted:
            break
    if not aborted:
        log.info("[3/4] 平滑降速 ...")
        for lv in (max(levels) // 2, 300, 150):
            if lv < 150 or lv >= max(levels):
                continue
            motor.set_speed(lv)
            log.info("<<< 降到 %d RPM", lv)
            t0 = time.time()
            while time.time() - t0 < 1.0:
                read_speed(motor, log, lv)
                time.sleep(0.4)
    log.info("[4/4] 停机 stop()+lock()...")
    motor.stop()
    motor.lock()
    log.info("%s: 峰值实际 %.1f RPM (目标 %d)", "ABORTED" if aborted else "PASS", peak, target)
    log.info(">>> 请目视确认转盘完全静止 <<<")
    return 1 if aborted else 0


def main():
    log = make_logger()
    args = sys.argv[1:]
    cmd = args[0] if args else "voltage"
    log.info("DBLS400 bring-up | port=%s mock=%s confirm=%s", PORT, MOCK, CONFIRM)
    motor = MotorController(port=PORT, mock=MOCK, logger=log)
    log.info("连接 (slave_id=%d baud=%d)...", motor.current_id, motor.comm.baudrate)
    if not motor.connect():
        log.error("连接失败: 串口打不开或驱动初始化失败")
        return 2
    try:
        if cmd == "voltage":
            return cmd_voltage(motor, log)
        if cmd == "spin":
            rpm = int(args[1]) if len(args) > 1 else 100
            hold_s = float(args[2]) if len(args) > 2 else 3.0
            return cmd_spin(motor, log, rpm, hold_s)
        if cmd == "ramp":
            target = int(args[1]) if len(args) > 1 else 1000
            per_hold = float(args[2]) if len(args) > 2 else 2.0
            return cmd_ramp(motor, log, target, per_hold)
        log.error("未知命令 %r (用: voltage | spin [RPM] [HOLD_S] | ramp [TARGET] [HOLD_S])", cmd)
        return 2
    finally:
        log.info("关闭串口")
        try:
            motor.comm.close()
        except Exception as e:  # noqa: BLE001
            log.warning("close 异常: %s", e)


if __name__ == "__main__":
    sys.exit(main())
