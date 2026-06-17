#!/usr/bin/env python3
"""
电动夹爪 RS485 测试工具
混合伺服力控电夹爪 Y1 — 协议探测与控制测试

接线：
  USB转RS485 适配器 A+  → 控制器 T
  USB转RS485 适配器 B-  → 控制器 R
  USB转RS485 适配器 GND → 控制器 G
  控制器 24V / COM-     → 24V 电源

使用方法：
  python3 tools/gripper_test.py
"""

import sys
import time
import glob

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    print("缺少 pyserial 库，请先安装：")
    print("  pip3 install pyserial")
    sys.exit(1)

# 常见波特率
BAUD_RATES = [9600, 19200, 38400, 57600, 115200]


def list_serial_ports():
    """列出所有可用串口"""
    ports = list_ports.comports()
    available = []
    for p in ports:
        available.append({
            "device": p.device,
            "description": p.description,
            "hwid": p.hwid,
        })
    return available


def select_port():
    """让用户选择串口"""
    ports = list_serial_ports()

    if not ports:
        print("未找到任何串口设备！")
        print("请检查：")
        print("  1. USB转RS485 适配器是否插好")
        print("  2. CH340 驱动是否已安装")
        return None

    print("\n可用串口：")
    for i, p in enumerate(ports):
        print(f"  [{i}] {p['device']}  —  {p['description']}")

    if len(ports) == 1:
        print(f"\n只有一个串口，自动选择: {ports[0]['device']}")
        return ports[0]["device"]

    while True:
        try:
            choice = input(f"\n选择串口编号 [0-{len(ports)-1}]: ").strip()
            idx = int(choice)
            if 0 <= idx < len(ports):
                return ports[idx]["device"]
        except (ValueError, KeyboardInterrupt):
            pass
        print("无效输入，请重新选择")


def scan_baud_rates(port):
    """自动扫描波特率，监听夹爪是否发数据"""
    print("\n" + "=" * 50)
    print("自动扫描波特率")
    print("每个波特率监听 3 秒，同时发探测指令...")
    print("=" * 50)

    results = []

    for baud in BAUD_RATES:
        print(f"\n尝试 {baud} baud ... ", end="", flush=True)

        try:
            ser = serial.Serial(port, baud, timeout=0.5)
            time.sleep(0.1)

            # 清空缓冲区
            ser.reset_input_buffer()

            # 发一些常见探测指令
            for probe in [b"\r\n", b"?\r\n", b"ID\r\n"]:
                ser.write(probe)
                time.sleep(0.3)

            # 监听 3 秒
            start = time.time()
            received = b""
            while time.time() - start < 3.0:
                if ser.in_waiting:
                    received += ser.read(ser.in_waiting)
                time.sleep(0.05)

            ser.close()

            if received:
                print(f"收到 {len(received)} 字节！")

                # 尝试显示为文本
                try:
                    text = received.decode("ascii", errors="replace")
                    printable = "".join(
                        c if c.isprintable() or c in "\r\n\t" else f"[0x{ord(c):02X}]"
                        for c in text
                    )
                    print(f"  文本: {printable[:200]}")
                except Exception:
                    pass

                # 显示十六进制
                hex_str = " ".join(f"{b:02X}" for b in received[:32])
                print(f"  HEX:  {hex_str}")

                results.append((baud, received))
            else:
                print("无数据")

        except serial.SerialException as e:
            print(f"错误: {e}")

    # 总结
    print("\n" + "=" * 50)
    print("扫描结果")
    print("=" * 50)

    if results:
        for baud, data in results:
            # 判断数据可读性
            try:
                text = data.decode("ascii", errors="strict")
                readable = True
            except Exception:
                readable = False

            status = "可读文本" if readable else "二进制数据（可能是 Modbus RTU）"
            print(f"  {baud} baud: 收到 {len(data)} 字节 — {status}")

        best = results[0][0]
        print(f"\n建议先尝试 {best} baud")
    else:
        print("  所有波特率都没有收到数据")
        print("\n可能的原因：")
        print("  1. T 和 R 接反了 → 对调试试")
        print("  2. GND 没接 → 检查 G 线")
        print("  3. 控制器没通电 → 检查 24V 电源")
        print("  4. 夹爪不主动发数据 → 需要先发正确指令才回复，试试交互模式")

    return results


def format_bytes(data):
    """格式化显示收到的字节"""
    parts = []
    for b in data:
        if 0x20 <= b <= 0x7E:
            parts.append(chr(b))
        else:
            parts.append(f"[0x{b:02X}]")
    return "".join(parts)


def interactive_mode(port, baud):
    """交互模式：手动发送指令，实时显示回复"""
    print(f"\n{'=' * 50}")
    print(f"交互模式  |  端口: {port}  |  波特率: {baud}")
    print("=" * 50)
    print("可用命令：")
    print("  直接输入文本 → 发送文本 + 回车换行")
    print("  hex XX XX XX → 发送十六进制字节")
    print("  baud N       → 切换波特率")
    print("  scan         → 重新扫描波特率")
    print("  listen       → 持续监听 10 秒")
    print("  modbus       → 发送 Modbus RTU 常见探测帧")
    print("  quit         → 退出")
    print("-" * 50)

    try:
        ser = serial.Serial(port, baud, timeout=0.1)
    except serial.SerialException as e:
        print(f"无法打开串口: {e}")
        return

    try:
        while True:
            # 检查是否有数据进来
            if ser.in_waiting:
                data = ser.read(ser.in_waiting)
                print(f"[夹爪→] {format_bytes(data)}")
                hex_str = " ".join(f"{b:02X}" for b in data)
                print(f"        HEX: {hex_str}")

            # 读取用户输入（非阻塞）
            try:
                cmd = input("> ").strip()
            except EOFError:
                break

            if not cmd:
                continue

            if cmd.lower() == "quit":
                break

            elif cmd.lower() == "listen":
                print("持续监听 10 秒（按 Ctrl+C 提前停止）...")
                try:
                    start = time.time()
                    while time.time() - start < 10:
                        if ser.in_waiting:
                            data = ser.read(ser.in_waiting)
                            print(f"[夹爪→] {format_bytes(data)}")
                            hex_str = " ".join(f"{b:02X}" for b in data)
                            print(f"        HEX: {hex_str}")
                        time.sleep(0.05)
                except KeyboardInterrupt:
                    pass
                print("监听结束")

            elif cmd.lower() == "scan":
                ser.close()
                scan_baud_rates(port)
                ser = serial.Serial(port, baud, timeout=0.1)

            elif cmd.lower().startswith("baud"):
                parts = cmd.split()
                if len(parts) == 2:
                    try:
                        new_baud = int(parts[1])
                        ser.close()
                        baud = new_baud
                        ser = serial.Serial(port, baud, timeout=0.1)
                        print(f"波特率已切换为: {baud}")
                    except (ValueError, serial.SerialException) as e:
                        print(f"错误: {e}")
                else:
                    print("用法: baud 9600")

            elif cmd.lower().startswith("hex"):
                hex_str = cmd[3:].strip()
                try:
                    # 支持 "01 06 01 00" 或 "01060100" 格式
                    hex_str = hex_str.replace(" ", "")
                    data = bytes.fromhex(hex_str)
                    ser.write(data)
                    display = " ".join(f"{b:02X}" for b in data)
                    print(f"[→夹爪] HEX: {display} ({len(data)} 字节)")
                    # 等待回复
                    time.sleep(0.5)
                    if ser.in_waiting:
                        resp = ser.read(ser.in_waiting)
                        print(f"[夹爪→] {format_bytes(resp)}")
                        hex_resp = " ".join(f"{b:02X}" for b in resp)
                        print(f"        HEX: {hex_resp}")
                    else:
                        print("[无回复]")
                except ValueError:
                    print("十六进制格式错误，示例: hex 01 06 01 00 00 01")

            elif cmd.lower() == "modbus":
                print("发送 Modbus RTU 常见探测帧...")
                print("（假设设备地址=0x01，尝试读取保持寄存器）")
                # 常见 Modbus RTU 帧
                modbus_frames = [
                    # 读保持寄存器: 地址01, 功能码03, 起始0000, 数量0001
                    (b"\x01\x03\x00\x00\x00\x01\x84\x0A", "读寄存器 0x0000 (地址=1)"),
                    # 读保持寄存器: 地址01, 功能码03, 起始0100, 数量0001
                    (b"\x01\x03\x01\x00\x00\x01\x85\xF6", "读寄存器 0x0100 (地址=1)"),
                    # 写单个寄存器: 地址01, 功能码06, 寄存器0100, 值0001
                    (b"\x01\x06\x01\x00\x00\x01\x49\xF6", "写寄存器 0x0100=1 (地址=1)"),
                    # 广播地址读: 地址00, 功能码03
                    (b"\x00\x03\x00\x00\x00\x01\x85\xDB", "读寄存器 0x0000 (广播地址=0)"),
                ]
                for frame, desc in modbus_frames:
                    hex_display = " ".join(f"{b:02X}" for b in frame)
                    print(f"\n  发送: {hex_display}  ({desc})")
                    ser.write(frame)
                    time.sleep(0.5)
                    if ser.in_waiting:
                        resp = ser.read(ser.in_waiting)
                        print(f"  回复: {' '.join(f'{b:02X}' for b in resp)}")
                        print(f"  解读: {format_bytes(resp)}")
                    else:
                        print("  [无回复]")
                print("\n如果某条指令有回复，说明该夹爪使用 Modbus RTU 协议")

            else:
                # 当作文本发送
                data = (cmd + "\r\n").encode()
                ser.write(data)
                print(f"[→夹爪] 文本: \"{cmd}\"")
                # 等待回复
                time.sleep(0.5)
                if ser.in_waiting:
                    resp = ser.read(ser.in_waiting)
                    print(f"[夹爪→] {format_bytes(resp)}")
                    hex_str = " ".join(f"{b:02X}" for b in resp)
                    print(f"        HEX: {hex_str}")
                else:
                    print("[无回复]")

    except KeyboardInterrupt:
        print("\n退出")
    finally:
        ser.close()


def main():
    print("=" * 50)
    print("  电动夹爪 RS485 测试工具")
    print("  混合伺服力控电夹爪 Y1")
    print("=" * 50)
    print()
    print("接线确认：")
    print("  USB转RS485 A+  → 控制器 T")
    print("  USB转RS485 B-  → 控制器 R")
    print("  USB转RS485 GND → 控制器 G")
    print("  控制器 24V/COM- → 24V 电源")
    print()

    # 选择串口
    port = select_port()
    if not port:
        return

    print(f"\n已选择: {port}")

    # 选择操作
    print("\n请选择操作：")
    print("  [1] 自动扫描波特率（推荐先做这个）")
    print("  [2] 直接进入交互模式（已知波特率时用）")

    choice = input("\n选择 [1/2]: ").strip()

    if choice == "1":
        results = scan_baud_rates(port)
        if results:
            baud = results[0][0]
        else:
            baud = 9600

        go = input(f"\n进入交互模式？波特率={baud} [Y/n]: ").strip().lower()
        if go != "n":
            interactive_mode(port, baud)
    elif choice == "2":
        baud_input = input("输入波特率 [默认 9600]: ").strip()
        baud = int(baud_input) if baud_input else 9600
        interactive_mode(port, baud)
    else:
        print("无效选择")


if __name__ == "__main__":
    main()
