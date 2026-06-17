#!/usr/bin/env python3
"""Z轴大步距验证脚本

用明显可见的大步数测试 Z 轴是否正常运动。
顺便验证刹车锁定/释放的区别。
"""

import serial
import time
import sys

RELAY_PORT = "/dev/cu.usbmodem6670E00119391"
ARDUINO_PORT = "/dev/cu.wchusbserial110"
BRAKE_RELEASE = bytes([0xA0, 0x02, 0x01, 0xA3])  # CH2 ON
BRAKE_LOCK = bytes([0xA0, 0x02, 0x00, 0xA2])      # CH2 OFF


def read_all(arduino: serial.Serial, wait: float = 0.5):
    """读取 Arduino 所有待接收数据"""
    time.sleep(wait)
    lines = []
    while arduino.in_waiting:
        line = arduino.readline().decode("utf-8", errors="replace").strip()
        if line:
            lines.append(line)
            print(f"    ← {line}")
    return lines


def send_cmd(arduino: serial.Serial, cmd: str, wait: float = 0.5):
    """发送命令并打印响应"""
    print(f"  → {cmd}")
    arduino.reset_input_buffer()
    arduino.write(f"{cmd}\n".encode())
    return read_all(arduino, wait)


def main():
    print("=" * 50)
    print("  Z 轴大步距运动测试")
    print("=" * 50)

    # 连接设备
    print("\n连接设备...")
    try:
        relay = serial.Serial(RELAY_PORT, 9600, timeout=1)
        time.sleep(0.5)
        print(f"  继电器: {RELAY_PORT} ✓")
    except Exception as e:
        print(f"  !! 继电器连接失败: {e}")
        sys.exit(1)

    try:
        arduino = serial.Serial(ARDUINO_PORT, 115200, timeout=1)
        time.sleep(2)
        read_all(arduino, 1)
        print(f"  Arduino: {ARDUINO_PORT} ✓")
    except Exception as e:
        print(f"  !! Arduino 连接失败: {e}")
        relay.close()
        sys.exit(1)

    # 查看初始状态
    print("\n当前状态：")
    send_cmd(arduino, "STATE")
    send_cmd(arduino, "POS")
    send_cmd(arduino, "SENS")

    # 设中等速度（SPEED 3 = 500μs）
    send_cmd(arduino, "SPEED 3")

    # ========== 测试 1：刹车锁定时大步 JOG ==========
    print("\n" + "=" * 50)
    print("  测试 1：刹车【锁定】+ JOG Z- N5000")
    print("=" * 50)
    relay.write(BRAKE_LOCK)
    time.sleep(0.5)
    print("  刹车已锁定")
    print("  5000 步如果真动了，应该能看到几毫米的位移")
    input("  >>> 按回车开始...")
    send_cmd(arduino, "JOG Z- N5000", wait=5)

    # 如果进了 ERROR 先 RESET
    send_cmd(arduino, "RESET", wait=0.3)
    send_cmd(arduino, "POS")

    result_a = input("  >>> 现象？(1=嗡嗡震动 2=正常移动 3=完全不动): ").strip()

    # ========== 测试 2：刹车释放时大步 JOG ==========
    print("\n" + "=" * 50)
    print("  测试 2：刹车【释放】+ JOG Z- N5000")
    print("=" * 50)
    relay.write(BRAKE_RELEASE)
    time.sleep(0.5)
    print("  刹车已释放")
    input("  >>> 按回车开始...")
    send_cmd(arduino, "JOG Z- N5000", wait=5)

    # 如果进了 ERROR 先 RESET
    send_cmd(arduino, "RESET", wait=0.3)
    send_cmd(arduino, "POS")

    result_b = input("  >>> 现象？(1=嗡嗡震动 2=正常移动 3=完全不动): ").strip()

    # ========== 诊断 ==========
    print("\n" + "=" * 50)
    print("  诊断结论")
    print("=" * 50)

    if result_a in ("1", "3") and result_b == "2":
        print("  刹车工作正常 ✓")
        print("  锁定时不动/震动，释放后正常移动。")
        print("  → 之前 HOME 时震动可能是归零脉冲太快（30μs），需要降速")
    elif result_a == "2" and result_b == "2":
        print("  两次都动了 → 刹车可能没接上或锁不住")
        print("  检查 DSTUR-T80 CH2 → 刹车线接法")
    elif result_b in ("1", "3"):
        print("  释放刹车后还是不动/震动")
        if result_b == "1":
            print("  嗡嗡震动 → 电机在努力但动不了")
            print("  可能：驱动器闭环位置偏差报警（看灯闪几次）")
        else:
            print("  完全不动 → 脉冲可能没到驱动器")
        print("\n  看 Z 轴驱动器面板灯：")
        print("    绿灯常亮 = 正常")
        print("    红灯闪 3 次 = 过流")
        print("    红灯闪 5 次 = 位置偏差过大")
        print("    红灯闪 7 次 = 编码器故障")
    else:
        print(f"  测试 1={result_a}，测试 2={result_b}，把详细现象告诉我")

    # 清理
    print("\n锁定刹车，关闭串口...")
    relay.write(BRAKE_LOCK)
    time.sleep(0.3)
    arduino.close()
    relay.close()
    print("  完成 ✓")


if __name__ == "__main__":
    main()
