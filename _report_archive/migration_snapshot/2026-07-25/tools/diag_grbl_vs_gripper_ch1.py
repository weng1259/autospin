"""Diagnose whether DSTUR relay channel switching disturbs grbl.

This script does not move XYZ. It opens the grbl serial port, polls realtime
status, then toggles one DSTUR-T80 channel repeatedly. It reports grbl reset
banners, alarm lines, Pn changes, and key settings after the test.
"""
from __future__ import annotations

import argparse
import threading
import time
from collections import Counter

import serial


GRBL_PORT = "/dev/cu.wchusbserial110"
RELAY_PORT = "/dev/cu.usbmodem6670E00119391"
GRBL_BAUD = 115200
RELAY_BAUD = 9600


KEY_SETTINGS = {
    "$1": "255",
    "$3": "6",
    "$5": "1",
    "$21": "1",
    "$22": "1",
    "$23": "0",
    "$24": "25.000",
    "$25": "500.000",
    "$26": "250",
    "$27": "5.000",
    "$130": "280.000",
    "$131": "280.000",
    "$132": "95.000",
}


def relay_frame(channel: int, on: bool) -> bytes:
    op = 0x01 if on else 0x00
    checksum = (0xA0 + channel + op) & 0xFF
    return bytes([0xA0, channel, op, checksum])


def drain_settings(grbl: serial.Serial, timeout_s: float = 5.0) -> dict[str, str]:
    grbl.write(b"$$\n")
    grbl.flush()
    end = time.time() + timeout_s
    seen: dict[str, str] = {}
    while time.time() < end:
        line = grbl.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        if line == "ok":
            break
        if line.startswith("$") and "=" in line:
            key, value = line.split("=", 1)
            seen[key] = value
    return seen


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cycles", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.35)
    parser.add_argument("--channel", type=int, default=1)
    args = parser.parse_args()
    if args.channel < 1 or args.channel > 8:
        raise SystemExit("--channel must be in [1, 8]")

    stop = threading.Event()
    stats: Counter[str] = Counter()
    pn_last = ""
    state_last = ""

    grbl = serial.Serial(GRBL_PORT, GRBL_BAUD, timeout=0.1)
    time.sleep(2.0)
    grbl.reset_input_buffer()

    def reader() -> None:
        nonlocal pn_last, state_last
        while not stop.is_set():
            try:
                line = grbl.readline().decode("utf-8", errors="replace").strip()
            except (serial.SerialException, OSError) as e:
                stats["serial_errors"] += 1
                print(f"!!! grbl serial error: {e}")
                stop.set()
                return
            if not line:
                continue
            if line.startswith("Grbl "):
                stats["grbl_banners"] += 1
                print(f"RST {line}")
                continue
            if line.startswith("ALARM:"):
                stats["alarms"] += 1
                print(f"ALM {line}")
                continue
            if line.startswith("<"):
                stats["status"] += 1
                body = line.strip("<>")
                parts = body.split("|")
                state = parts[0] if parts else "?"
                pn = ""
                for part in parts[1:]:
                    if part.startswith("Pn:"):
                        pn = part[3:]
                        break
                if state != state_last:
                    print(f"STA {state_last or '-'} -> {state}  {line}")
                    state_last = state
                if pn != pn_last:
                    print(f"PIN {pn_last or '-'} -> {pn or '-'}  {line}")
                    if pn:
                        stats["pn_hits"] += 1
                    pn_last = pn
                return
            if line.startswith("[MSG:"):
                print(f"MSG {line}")

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def poller() -> None:
        while not stop.is_set():
            try:
                grbl.write(b"?")
                grbl.flush()
            except (serial.SerialException, OSError) as e:
                stats["serial_errors"] += 1
                print(f"!!! grbl poll write error: {e}")
                stop.set()
                return
            time.sleep(0.2)

    poller_thread = threading.Thread(target=poller, daemon=True)
    poller_thread.start()

    relay = serial.Serial(RELAY_PORT, RELAY_BAUD, timeout=1.0)
    time.sleep(0.5)

    try:
        print(
            f"== Toggle DSTUR CH{args.channel} {args.cycles} cycles, "
            f"interval={args.interval}s; no XYZ motion =="
        )
        for i in range(args.cycles):
            relay.write(relay_frame(args.channel, True))
            relay.flush()
            print(f"CH{args.channel} ON  cycle {i + 1}/{args.cycles}")
            time.sleep(args.interval)
            relay.write(relay_frame(args.channel, False))
            relay.flush()
            print(f"CH{args.channel} OFF cycle {i + 1}/{args.cycles}")
            time.sleep(args.interval)
    finally:
        try:
            relay.write(relay_frame(args.channel, False))
            relay.flush()
        except Exception:
            pass
        relay.close()
        stop.set()
        reader_thread.join(timeout=1.0)
        poller_thread.join(timeout=1.0)

    print("\n== Summary ==")
    for key in ("status", "grbl_banners", "alarms", "pn_hits", "serial_errors"):
        print(f"{key}: {stats[key]}")

    print("\n== Key $$ check ==")
    grbl.reset_input_buffer()
    seen = drain_settings(grbl)
    for key, expected in KEY_SETTINGS.items():
        actual = seen.get(key, "?")
        mark = "OK" if actual == expected else f"BAD expected {expected}"
        print(f"{key}={actual}  {mark}")
    grbl.close()


if __name__ == "__main__":
    main()
