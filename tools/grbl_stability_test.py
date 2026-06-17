#!/usr/bin/env python3
"""grbl 稳定性诊断工具

三个模式：
  health   只读体检：dump $$、轮询 ? 30s，不动电机，看 Pn/Lim/ALARM 抖动
  jog      jog 压测：指定轴和距离，反复来回 jog，统计 ok/error/ALARM/断连
  duplex   双串口干扰：jog 同时反复 toggle DSTUR-T80 CH2，复现 MEMORY 里记过的 EMI

用法:
  python3 tools/grbl_stability_test.py health
  python3 tools/grbl_stability_test.py jog --axis X --range 20 --count 200 --feed 2000
  python3 tools/grbl_stability_test.py duplex --axis X --range 20 --count 100

日志写到 tools/_stability_logs/<timestamp>_<mode>.log，同时打印到屏幕。
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import pathlib
import re
import signal
import sys
import threading
import time
from collections import Counter
from dataclasses import dataclass, field

import serial

GRBL_PORT = "/dev/cu.wchusbserial110"
RELAY_PORT = "/dev/cu.usbmodem6670E00119391"
GRBL_BAUD = 115200
RELAY_BAUD = 9600

BRAKE_RELEASE = bytes([0xA0, 0x02, 0x01, 0xA3])
BRAKE_LOCK    = bytes([0xA0, 0x02, 0x00, 0xA2])

LOG_DIR = pathlib.Path(__file__).parent / "_stability_logs"

STATUS_RE = re.compile(r"<([^|>]+)\|([^>]*)>")


@dataclass
class Stats:
    sent: int = 0
    ok: int = 0
    errors: Counter = field(default_factory=Counter)
    alarms: Counter = field(default_factory=Counter)
    pn_hits: Counter = field(default_factory=Counter)
    status_samples: int = 0
    disconnects: int = 0
    started: float = field(default_factory=time.time)

    def summary(self) -> str:
        elapsed = time.time() - self.started
        lines = [
            "",
            "=" * 60,
            f"  压测汇总（{elapsed:.1f}s）",
            "=" * 60,
            f"  发出命令数      : {self.sent}",
            f"  ok 响应数       : {self.ok}",
            f"  error 总数      : {sum(self.errors.values())}  详情: {dict(self.errors)}",
            f"  ALARM 总数      : {sum(self.alarms.values())}  详情: {dict(self.alarms)}",
            f"  Pn 触发次数     : {sum(self.pn_hits.values())} 详情: {dict(self.pn_hits)}",
            f"  ?-status 采样   : {self.status_samples}",
            f"  USB/serial 断联 : {self.disconnects}",
            "=" * 60,
        ]
        return "\n".join(lines)


class GrblLink:
    """grbl 串口封装 + 读线程 + 日志 + 事件分类"""

    def __init__(self, port: str, baud: int, logfile: pathlib.Path):
        self.port = port
        self.baud = baud
        self.ser: serial.Serial | None = None
        self.logfile = logfile
        self.log_fp = logfile.open("w", encoding="utf-8", buffering=1)
        self.stats = Stats()
        self.stop_flag = threading.Event()
        self.reader_thread: threading.Thread | None = None
        self.last_status: str = ""
        self.last_pn: str = ""
        self.last_state: str = ""

    def log(self, tag: str, msg: str):
        ts = dt.datetime.now().strftime("%H:%M:%S.%f")[:-3]
        line = f"[{ts}] {tag} {msg}"
        print(line)
        self.log_fp.write(line + "\n")

    def open(self):
        self.ser = serial.Serial(self.port, self.baud, timeout=0.1)
        time.sleep(2.0)  # grbl 上电/reset 等待
        self.ser.reset_input_buffer()
        self.reader_thread = threading.Thread(target=self._reader, daemon=True)
        self.reader_thread.start()
        self.log("SYS", f"opened {self.port}@{self.baud}, log -> {self.logfile}")

    def close(self):
        self.stop_flag.set()
        if self.reader_thread:
            self.reader_thread.join(timeout=2.0)
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.log_fp.close()

    def _reader(self):
        buf = b""
        while not self.stop_flag.is_set():
            try:
                chunk = self.ser.read(256)
                if not chunk:
                    continue
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    text = line.decode("utf-8", errors="replace").strip("\r\n ")
                    if not text:
                        continue
                    self._classify(text)
            except (serial.SerialException, OSError) as e:
                self.stats.disconnects += 1
                self.log("!!!", f"serial disconnect: {e}")
                self.stop_flag.set()
                return

    def _classify(self, text: str):
        # 状态回应 <Idle|MPos:...|Pn:XYZ>  — 注意 grbl 可能返回 <State|...> 也可能 <State|WPos:...>
        if text.startswith("<") and text.endswith(">"):
            self.stats.status_samples += 1
            self.last_status = text
            fields = dict()
            parts = text.strip("<>").split("|")
            state = parts[0] if parts else "?"
            for part in parts[1:]:
                if ":" in part:
                    k, v = part.split(":", 1)
                    fields[k] = v
            pn = fields.get("Pn", "")
            if pn and pn != self.last_pn:
                self.log("PIN", f"Pn change: '{self.last_pn}' -> '{pn}'  full={text}")
                for c in pn:
                    self.stats.pn_hits[c] += 1
                self.last_pn = pn
            elif not pn and self.last_pn:
                self.log("PIN", f"Pn cleared (was '{self.last_pn}')  full={text}")
                self.last_pn = ""
            # 每 20 次打一行避免刷屏，状态切换立刻打
            if state != self.last_state:
                self.log("STA", f"[{self.stats.status_samples}] state {self.last_state}->{state}  {text}")
                self.last_state = state
            elif self.stats.status_samples == 1 or self.stats.status_samples % 20 == 0:
                self.log("STA", f"[{self.stats.status_samples}] {text}")
            return

        if text == "ok":
            self.stats.ok += 1
            self.log("ACK", "ok")
            return

        if text.startswith("error:"):
            code = text.split(":", 1)[1]
            self.stats.errors[code] += 1
            self.log("ERR", text)
            return

        if text.startswith("ALARM:"):
            code = text.split(":", 1)[1]
            self.stats.alarms[code] += 1
            self.log("ALM", text)
            return

        # grbl banner / $$ 输出 / feedback
        self.log("RX ", text)

    def send(self, cmd: str):
        if self.stop_flag.is_set():
            return
        self.stats.sent += 1
        self.log("TX ", cmd)
        self.ser.write((cmd + "\n").encode())

    def send_and_wait_ack(self, cmd: str, timeout: float = 5.0) -> str:
        """发一条命令，等直到 ok / error:N / ALARM:N 回来（grbl 简单流控）"""
        before_ok = self.stats.ok
        before_err = sum(self.stats.errors.values())
        before_alm = sum(self.stats.alarms.values())
        self.send(cmd)
        t0 = time.time()
        while time.time() - t0 < timeout and not self.stop_flag.is_set():
            if self.stats.ok > before_ok:
                return "ok"
            if sum(self.stats.errors.values()) > before_err:
                return "error"
            if sum(self.stats.alarms.values()) > before_alm:
                return "alarm"
            time.sleep(0.02)
        return "timeout"

    def send_realtime(self, byte: int, label: str):
        self.log("TX*", f"realtime 0x{byte:02X} ({label})")
        self.ser.write(bytes([byte]))

    def wait_idle(self, timeout: float = 30.0) -> bool:
        """轮询 ? 直到状态变 Idle"""
        t0 = time.time()
        while time.time() - t0 < timeout:
            if self.stop_flag.is_set():
                return False
            self.send_realtime(0x3F, "?")  # '?'
            time.sleep(0.2)
            if self.last_status.startswith("<Idle"):
                return True
        return False


def open_relay() -> serial.Serial | None:
    try:
        r = serial.Serial(RELAY_PORT, RELAY_BAUD, timeout=0.3)
        time.sleep(0.5)
        return r
    except serial.SerialException as e:
        print(f"!!! 继电器打不开: {e}")
        return None


def mode_health(args):
    """只读体检：dump $$、循环 ? 30s"""
    logfile = LOG_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}_health.log"
    g = GrblLink(GRBL_PORT, GRBL_BAUD, logfile)
    try:
        g.open()
        time.sleep(0.5)
        g.send("$$")         # 设置 dump
        time.sleep(1.5)
        g.send("$G")         # gcode parser state
        time.sleep(0.5)
        g.send("$I")         # build info
        time.sleep(0.5)
        g.send("$#")         # 坐标系
        time.sleep(0.5)

        duration = args.duration
        g.log("SYS", f"== 进入 {duration}s 的 ? 轮询（不发任何运动命令） ==")
        t0 = time.time()
        while time.time() - t0 < duration and not g.stop_flag.is_set():
            g.send_realtime(0x3F, "?")
            time.sleep(0.5)
    finally:
        print(g.stats.summary())
        g.close()


def mode_jog(args):
    logfile = LOG_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}_jog_{args.axis}.log"
    g = GrblLink(GRBL_PORT, GRBL_BAUD, logfile)
    relay = None
    try:
        g.open()
        time.sleep(0.5)

        # Z 轴压测必须先释放刹车
        if args.axis.upper() == "Z":
            relay = open_relay()
            if not relay:
                g.log("!!!", "Z 轴压测但继电器打不开，退出")
                return
            relay.write(BRAKE_RELEASE)
            time.sleep(0.3)
            g.log("SYS", "Z 刹车已释放 (DSTUR CH2 ON)")

        # 确认当前 Idle
        g.send("$X")          # 清掉任何旧 alarm
        time.sleep(0.3)
        if not g.wait_idle(5):
            g.log("!!!", "grbl 不是 Idle，放弃。先手动处理。")
            return

        mode_desc = "流控+等 ok" if args.flow_control else "无流控（喂满 buffer）"
        g.log("SYS", f"== 开始 {args.count} 次 {args.axis}±{args.range}mm jog，feed={args.feed}，流控={mode_desc}，首步方向 - ==")
        for i in range(args.count):
            if g.stop_flag.is_set():
                break
            # 首步往 -（因为 $23=0 归零后坐标在 0，范围 [-max, 0]）
            direction = -args.range if i % 2 == 0 else +args.range
            cmd = f"$J=G91 {args.axis.upper()}{direction:+g} F{args.feed}"
            if args.flow_control:
                # 1. 等 ok（grbl 已接受进 planner）
                r = g.send_and_wait_ack(cmd, timeout=3.0)
                if r == "alarm":
                    g.log("!!!", "出现 ALARM，终止压测以便分析。")
                    break
                if r in ("error", "timeout"):
                    g.log("!!!", f"jog 响应异常 ({r})，继续观察")
                # 2. 等 jog 实际执行完（轮询 ? 直到 Idle）
                t0 = time.time()
                while time.time() - t0 < 5.0 and not g.stop_flag.is_set():
                    g.send_realtime(0x3F, "?")
                    time.sleep(0.05)
                    if g.last_status.startswith("<Idle"):
                        break
            else:
                g.send(cmd)
                time.sleep(max(0.15, abs(direction) / (args.feed / 60.0) + 0.05))
                g.send_realtime(0x3F, "?")
            if g.stats.alarms:
                g.log("!!!", "出现 ALARM，终止压测以便分析。")
                break
        # 收尾等 idle
        g.wait_idle(timeout=10)
    finally:
        # 关闭顺序：先锁刹车 → 长等（1.5s）让感性尖峰消散 → 再关 grbl（DTR 会抖）
        # 避免两个 EMI 事件重叠引发 macOS USB 子系统重置
        if relay:
            try:
                relay.write(BRAKE_LOCK)
                time.sleep(1.5)
                relay.close()
                g.log("SYS", "Z 刹车已锁回（等 1.5s 后继续）")
            except Exception:
                pass
        time.sleep(0.3)
        print(g.stats.summary())
        g.close()


def mode_home(args):
    """自动归零：释放 Z 刹车 → $H → 锁回刹车"""
    logfile = LOG_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}_home.log"
    g = GrblLink(GRBL_PORT, GRBL_BAUD, logfile)
    relay = None
    try:
        g.open()
        time.sleep(0.5)
        relay = open_relay()
        if not relay:
            g.log("!!!", "继电器打不开，归零会失败（Z 刹车锁着）。退出。")
            return
        # 清 alarm + 确认
        g.send("$X")
        time.sleep(0.5)
        # 释放 Z 刹车
        relay.write(BRAKE_RELEASE)
        time.sleep(0.5)
        g.log("SYS", "Z 刹车已释放，现在发 $H")
        # $H 归零（Z 先归顶部 → X+Y 并行），超时 90s
        g.send("$H")
        t0 = time.time()
        homed = False
        while time.time() - t0 < 90.0:
            g.send_realtime(0x3F, "?")
            time.sleep(0.3)
            if g.last_status.startswith("<Idle") and g.stats.ok > 0:
                homed = True
                break
            if g.stats.alarms:
                g.log("!!!", "归零过程中出现 ALARM")
                break
        if homed:
            g.log("SYS", f"== 归零成功，耗时 {time.time()-t0:.1f}s，WPos={g.last_status} ==")
        else:
            g.log("!!!", "归零未完成（超时或 alarm）")
    finally:
        # 关闭顺序：先锁刹车 → 长等（1.5s）让感性尖峰消散 → 再关 grbl（DTR 会抖）
        # 避免两个 EMI 事件重叠引发 macOS USB 子系统重置
        if relay:
            try:
                relay.write(BRAKE_LOCK)
                time.sleep(1.5)
                relay.close()
                g.log("SYS", "Z 刹车已锁回（等 1.5s 后继续）")
            except Exception:
                pass
        time.sleep(0.3)
        print(g.stats.summary())
        g.close()


def mode_duplex(args):
    """一边 jog，一边 toggle 继电器 CH3（备用路，不动刹车），验证双串口 EMI"""
    logfile = LOG_DIR / f"{dt.datetime.now():%Y%m%d_%H%M%S}_duplex_{args.axis}.log"
    g = GrblLink(GRBL_PORT, GRBL_BAUD, logfile)
    relay = open_relay()
    if not relay:
        return
    stop_relay = threading.Event()

    def relay_worker():
        # 用 CH3（备用路），不动 CH2/CH1 避免影响实验
        ON  = bytes([0xA0, 0x03, 0x01, 0xA4])
        OFF = bytes([0xA0, 0x03, 0x00, 0xA3])
        state = False
        while not stop_relay.is_set():
            try:
                relay.write(ON if not state else OFF)
                state = not state
                time.sleep(0.3)
            except Exception as e:
                g.log("!!!", f"继电器线程异常: {e}")
                return

    try:
        g.open()
        g.send("$X")
        time.sleep(0.3)
        g.wait_idle(5)
        t = threading.Thread(target=relay_worker, daemon=True)
        t.start()
        g.log("SYS", f"== duplex: jog + CH3 toggle@0.3s，{args.count} 次 ==")
        for i in range(args.count):
            if g.stop_flag.is_set():
                break
            direction = args.range if i % 2 == 0 else -args.range
            g.send(f"$J=G91 {args.axis.upper()}{direction:+g} F{args.feed}")
            time.sleep(max(0.2, abs(direction) / (args.feed / 60.0) + 0.1))
            if g.stats.alarms:
                break
        g.wait_idle(10)
    finally:
        stop_relay.set()
        time.sleep(0.5)
        try:
            relay.write(bytes([0xA0, 0x03, 0x00, 0xA3]))  # CH3 OFF 收尾
            relay.close()
        except Exception:
            pass
        print(g.stats.summary())
        g.close()


def main():
    LOG_DIR.mkdir(exist_ok=True)
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="mode", required=True)

    h = sub.add_parser("health")
    h.add_argument("--duration", type=int, default=30)
    h.set_defaults(func=mode_health)

    j = sub.add_parser("jog")
    j.add_argument("--axis", choices=["X", "Y", "Z", "x", "y", "z"], required=True)
    j.add_argument("--range", type=float, default=10.0, help="单次 jog 距离 mm")
    j.add_argument("--count", type=int, default=100)
    j.add_argument("--feed", type=int, default=2000)
    j.add_argument("--flow-control", action="store_true", help="等 ok + 等 Idle 再发下一条（模拟正确使用）")
    j.set_defaults(func=mode_jog)

    hm = sub.add_parser("home", help="安全归零：Z 自动释放/锁刹车")
    hm.set_defaults(func=mode_home)

    d = sub.add_parser("duplex")
    d.add_argument("--axis", choices=["X", "Y", "Z", "x", "y", "z"], required=True)
    d.add_argument("--range", type=float, default=10.0)
    d.add_argument("--count", type=int, default=100)
    d.add_argument("--feed", type=int, default=2000)
    d.set_defaults(func=mode_duplex)

    args = p.parse_args()

    def _sigint(*_):
        print("\n^C 收到，发 Ctrl-X soft reset")
        try:
            with serial.Serial(GRBL_PORT, GRBL_BAUD, timeout=0.5) as s:
                s.write(b"\x18")
        except Exception:
            pass
        sys.exit(130)

    signal.signal(signal.SIGINT, _sigint)
    args.func(args)


if __name__ == "__main__":
    main()
