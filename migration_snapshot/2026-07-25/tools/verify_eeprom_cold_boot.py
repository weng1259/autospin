#!/usr/bin/env python3
"""冷启 $5 EEPROM 验证（Phase 3.2 前置硬件 smoke）。

目的：确认 grbl `$5=1`（限位反相，NPN 亮通型传感器逻辑）在断电后不会腐蚀为 0。
工作模式：**自动检测 USB 拔插**，PM 只做物理动作，脚本全程观察：

  1. 等待 /dev/cu.wchusbserial110 **消失**（PM 拔 USB 或断 24V 电源）
  2. 等待 /dev/cu.wchusbserial110 **重新出现**（PM 通电）
  3. 稍等 grbl 上电复位后打开串口，发 $$，抓 $5=?，记录 pass/fail
  4. 循环指定次数
  5. 打印汇总 + 退出码（全 pass → 0，任一 fail → 1）

用法：
  tools/spikes/.venv/bin/python tools/verify_eeprom_cold_boot.py --count 10

需要 Streamlit 等占串口的进程已关闭（否则 $$ 抓不到回应）。
"""
from __future__ import annotations

import argparse
import os
import pathlib
import re
import sys
import time
from datetime import datetime

import serial

GRBL_PORT = "/dev/cu.wchusbserial110"
GRBL_BAUD = 115200

LOG_DIR = pathlib.Path(__file__).parent / "_stability_logs"
LOG_DIR.mkdir(exist_ok=True)


def wait_until(condition_desc: str, check, timeout_s: float, say, poll_interval: float = 0.3) -> bool:
    """poll until check() returns truthy or timeout."""
    t0 = time.time()
    last_tick = 0.0
    while time.time() - t0 < timeout_s:
        if check():
            elapsed = time.time() - t0
            say(f"   ✓ {condition_desc}（用时 {elapsed:.1f}s）")
            return True
        if time.time() - last_tick > 5.0:
            remaining = timeout_s - (time.time() - t0)
            say(f"   … 等 {condition_desc}（剩 {remaining:.0f}s）")
            last_tick = time.time()
        time.sleep(poll_interval)
    say(f"   ✗ 超时：{condition_desc}（{timeout_s:.0f}s 未满足）")
    return False


def read_dollar_5(port: str, baud: int) -> tuple[bool, str]:
    """打开串口 → 等 grbl banner → 发 $$ → 抓 $5=N → 关闭。

    return: (pass, raw_$5_line)
    """
    ser = serial.Serial(port, baud, timeout=0.5)
    try:
        time.sleep(2.5)  # grbl 上电/复位 + banner
        ser.reset_input_buffer()
        ser.write(b"$$\n")
        buf = b""
        t0 = time.time()
        while time.time() - t0 < 3.0:
            chunk = ser.read(1024)
            if chunk:
                buf += chunk
            elif b"$132=" in buf:
                break
            else:
                time.sleep(0.05)

        text = buf.decode("utf-8", errors="replace")
        m = re.search(r"(\$5=\d+)", text)
        if m is None:
            snippet = text[:300].replace("\n", " | ")
            return False, f"[未找到 $5 行] raw={snippet}"

        line = m.group(1)
        passed = line == "$5=1"
        return passed, line
    finally:
        try:
            ser.close()
        except Exception:
            pass


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--count", type=int, default=10, help="冷启轮数")
    p.add_argument("--port", default=GRBL_PORT)
    p.add_argument("--baud", type=int, default=GRBL_BAUD)
    p.add_argument("--disconnect-timeout", type=float, default=60.0)
    p.add_argument("--reconnect-timeout", type=float, default=60.0)
    args = p.parse_args()

    logfile = LOG_DIR / f"{datetime.now():%Y%m%d_%H%M%S}_cold_boot.log"
    log_lines: list[str] = []

    def say(msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        log_lines.append(line)

    say(f"== 冷启 $5 验证 × {args.count} 次 ==")
    say(f"   端口: {args.port}, 波特率: {args.baud}")
    say(f"   日志: {logfile}")
    say("")

    # sanity: port must exist at start
    if not os.path.exists(args.port):
        say(f"✗ 开始前 {args.port} 不存在。请先把 Arduino Mega 通电 + 插 USB。")
        sys.exit(2)

    say("开始循环。**请 PM 按以下节奏物理操作**：")
    say("  对每一轮：")
    say("    1) 断电（拔 USB 或关 24V 电源）→ 脚本自动检测到断开")
    say("    2) 通电 → 脚本自动检测到出现并读 $5")
    say("    3) 看到 '✓ PASS' 或 '✗ FAIL' 后进入下一轮")
    say("")

    results: list[tuple[int, bool, str]] = []
    for i in range(1, args.count + 1):
        say("")
        say(f"── 第 {i}/{args.count} 次 ──")

        # Phase 1: wait for unplug
        say(f"1) 请断电（拔 USB / 关 24V）...")
        if not wait_until(
            "USB 断开", lambda: not os.path.exists(args.port),
            args.disconnect_timeout, say,
        ):
            results.append((i, False, "等断电超时"))
            continue

        # Phase 2: wait for replug
        say(f"2) 请通电（USB 插回 / 开 24V）...")
        if not wait_until(
            "USB 重新出现", lambda: os.path.exists(args.port),
            args.reconnect_timeout, say,
        ):
            results.append((i, False, "等通电超时"))
            continue

        # small grace period for udev/IOKit to finish setting up the device node
        time.sleep(1.5)

        # Phase 3: read $5
        try:
            passed, raw = read_dollar_5(args.port, args.baud)
        except Exception as e:
            say(f"   ✗ 读取异常: {e}")
            results.append((i, False, str(e)))
            continue

        status = "✓ PASS" if passed else "✗ FAIL"
        say(f"   {status}  {raw}")
        results.append((i, passed, raw))

    # 汇总
    say("")
    say("=" * 60)
    say("  汇总")
    say("=" * 60)
    passes = sum(1 for _, ok, _ in results if ok)
    fails = len(results) - passes
    say(f"  PASS: {passes}/{len(results)}")
    say(f"  FAIL: {fails}/{len(results)}")
    if fails:
        say("")
        say("  失败详情：")
        for i, ok, raw in results:
            if not ok:
                say(f"    #{i}: {raw}")

    logfile.write_text("\n".join(log_lines) + "\n", encoding="utf-8")

    sys.exit(0 if fails == 0 else 1)


if __name__ == "__main__":
    main()
