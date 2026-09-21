#!/usr/bin/env python3
"""步/mm 校准验证脚本

归零后沿各轴移动指定步数，用尺子量实际距离来验证 steps/mm。

理论值：51200 脉冲/转 ÷ 75mm/转（HTD3M×25齿）= 682.67 步/mm
测试方案：发 MOVE X6827 → 理论走 10.00mm → 量尺子对比
"""

import serial
import time
import sys

# === 配置 ===
ARDUINO_PORT = "/dev/cu.wchusbserial110"
RELAY_PORT = "/dev/cu.usbmodem6670E00119391"
BAUD_ARDUINO = 115200
BAUD_RELAY = 9600

# 测试步数（理论 10mm @ 682.67 步/mm）
TEST_STEPS = 6827
THEORETICAL_MM = TEST_STEPS / 682.67

# 继电器命令
BRAKE_RELEASE = bytes([0xA0, 0x02, 0x01, 0xA3])  # CH2 ON
BRAKE_LOCK = bytes([0xA0, 0x02, 0x00, 0xA2])      # CH2 OFF


def send_relay(relay: serial.Serial, cmd: bytes, desc: str):
    """发送继电器命令"""
    relay.write(cmd)
    time.sleep(0.3)
    print(f"  继电器: {desc}")


def send_cmd(arduino: serial.Serial, cmd: str, timeout: float = 5.0) -> list[str]:
    """发送命令并收集响应行"""
    arduino.reset_input_buffer()
    arduino.write(f"{cmd}\n".encode())

    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        if arduino.in_waiting:
            line = arduino.readline().decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
                # 收到同步响应就可以短暂继续收异步事件
                if line.startswith("OK") or line.startswith("ERR"):
                    # 再等一小会收异步事件
                    time.sleep(0.1)
                    while arduino.in_waiting:
                        extra = arduino.readline().decode("utf-8", errors="replace").strip()
                        if extra:
                            lines.append(extra)
                    break
        else:
            time.sleep(0.01)
    return lines


def wait_for_event(arduino: serial.Serial, target: str, timeout: float = 120.0) -> tuple[bool, list[str]]:
    """等待特定异步事件（如 @HOME_OK, @MOVE_OK）"""
    lines = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        if arduino.in_waiting:
            line = arduino.readline().decode("utf-8", errors="replace").strip()
            if line:
                lines.append(line)
                print(f"    ← {line}")
                if target in line:
                    return True, lines
                if "@HOME_FAIL" in line or "@FAULT" in line:
                    return False, lines
        else:
            time.sleep(0.01)
    return False, lines


def test_axis(arduino: serial.Serial, axis: str, steps: int):
    """测试单轴定位精度"""
    print(f"\n{'='*50}")
    print(f"  测试 {axis} 轴：MOVE {axis}{steps}（理论 {THEORETICAL_MM:.2f}mm）")
    print(f"{'='*50}")

    # 发送 MOVE
    print(f"\n  → MOVE {axis}{steps}")
    resp = send_cmd(arduino, f"MOVE {axis}{steps}", timeout=2)
    for line in resp:
        print(f"    ← {line}")

    if not any("OK" in l for l in resp):
        print(f"  !! MOVE 命令失败")
        return False

    # 等待 @MOVE_OK
    print(f"  等待运动完成...")
    ok, _ = wait_for_event(arduino, "@MOVE_OK", timeout=30)
    if not ok:
        print(f"  !! 运动未完成")
        return False

    # 查询位置
    time.sleep(0.2)
    resp = send_cmd(arduino, "POS")
    for line in resp:
        print(f"    ← {line}")

    input(f"\n  >>> 请用尺子量 {axis} 轴从原点到当前位置的距离，然后按回车继续...")

    # 回原点
    print(f"\n  → MOVE {axis}0（返回原点）")
    resp = send_cmd(arduino, f"MOVE {axis}0", timeout=2)
    for line in resp:
        print(f"    ← {line}")

    ok, _ = wait_for_event(arduino, "@MOVE_OK", timeout=30)
    if not ok:
        print(f"  !! 返回原点失败")
        return False

    print(f"  已返回原点")
    return True


def main():
    print("=" * 50)
    print("  步/mm 校准验证")
    print(f"  理论值：682.67 步/mm（51200÷75）")
    print(f"  测试：{TEST_STEPS} 步 = 理论 {THEORETICAL_MM:.2f}mm")
    print("=" * 50)

    # 连接设备
    print("\n[1/5] 连接设备...")
    try:
        relay = serial.Serial(RELAY_PORT, BAUD_RELAY, timeout=1)
        time.sleep(0.5)
        print(f"  继电器: {RELAY_PORT} ✓")
    except Exception as e:
        print(f"  继电器连接失败: {e}")
        sys.exit(1)

    try:
        arduino = serial.Serial(ARDUINO_PORT, BAUD_ARDUINO, timeout=1)
        time.sleep(2)  # 等 Arduino 重启
        # 清空启动信息
        while arduino.in_waiting:
            line = arduino.readline().decode("utf-8", errors="replace").strip()
            if line:
                print(f"    启动: {line}")
        print(f"  Arduino: {ARDUINO_PORT} ✓")
    except Exception as e:
        print(f"  Arduino 连接失败: {e}")
        relay.close()
        sys.exit(1)

    # 检查状态
    print("\n[2/5] 检查状态...")
    resp = send_cmd(arduino, "STATE")
    for line in resp:
        print(f"    ← {line}")

    resp_sens = send_cmd(arduino, "SENS")
    for line in resp_sens:
        print(f"    ← {line}")

    # 释放 Z 轴刹车
    print("\n[3/5] 释放 Z 轴刹车...")
    send_relay(relay, BRAKE_RELEASE, "CH2 ON = 刹车释放")
    time.sleep(0.5)

    # 归零
    print("\n[4/5] 归零（HOME）...")
    print("  → HOME")
    resp = send_cmd(arduino, "HOME", timeout=2)
    for line in resp:
        print(f"    ← {line}")

    if not any("OK" in l for l in resp):
        print("  !! HOME 命令失败，尝试 RESET 后重试...")
        send_cmd(arduino, "RESET")
        time.sleep(0.5)
        resp = send_cmd(arduino, "HOME", timeout=2)
        for line in resp:
            print(f"    ← {line}")

    print("  等待归零完成（最长 120s）...")
    ok, _ = wait_for_event(arduino, "@HOME_OK", timeout=120)
    if not ok:
        print("  !! 归零失败！")
        send_relay(relay, BRAKE_LOCK, "CH2 OFF = 刹车锁定")
        arduino.close()
        relay.close()
        sys.exit(1)

    # 验证归零后位置
    time.sleep(0.3)
    resp = send_cmd(arduino, "POS")
    for line in resp:
        print(f"    ← {line}")
    resp = send_cmd(arduino, "SENS")
    for line in resp:
        print(f"    ← {line}")

    print("\n  归零完成！三轴应在原点位置。")

    # 测试各轴
    print("\n[5/5] 开始步距校准测试...")
    print(f"  每轴移动 {TEST_STEPS} 步，理论距离 {THEORETICAL_MM:.2f}mm")
    print(f"  请准备尺子！\n")

    results = {}
    for axis in ["X", "Y", "Z"]:
        input(f"  >>> 按回车开始测试 {axis} 轴（放好尺子对准原点位置）...")

        if axis == "Z":
            print(f"  注意：Z 轴行程 100mm，测试 {TEST_STEPS} 步")

        ok = test_axis(arduino, axis, TEST_STEPS)
        if ok:
            measured = input(f"  >>> 实测距离是多少 mm？（输入数字，跳过按回车）: ").strip()
            if measured:
                try:
                    measured_mm = float(measured)
                    actual_spm = TEST_STEPS / measured_mm
                    results[axis] = {
                        "measured_mm": measured_mm,
                        "steps_per_mm": actual_spm,
                    }
                    print(f"  → {axis} 轴：{TEST_STEPS}步 = {measured_mm}mm → {actual_spm:.2f} 步/mm")
                except ValueError:
                    print(f"  → 输入无效，跳过")

    # 锁定刹车
    print("\n锁定 Z 轴刹车...")
    send_relay(relay, BRAKE_LOCK, "CH2 OFF = 刹车锁定")

    # 汇总
    print("\n" + "=" * 50)
    print("  校准结果汇总")
    print("=" * 50)
    print(f"  理论值：682.67 步/mm（51200÷75）")
    print(f"  旧值（已知有误）：68.3 步/mm")
    print()

    if results:
        for axis, data in results.items():
            ratio = data["steps_per_mm"] / 682.67
            print(f"  {axis} 轴：{data['measured_mm']:.1f}mm → {data['steps_per_mm']:.2f} 步/mm（理论值的 {ratio:.2f} 倍）")

        # 判断
        avg_spm = sum(d["steps_per_mm"] for d in results.values()) / len(results)
        print(f"\n  平均：{avg_spm:.2f} 步/mm")

        if abs(avg_spm - 682.67) / 682.67 < 0.05:
            print("  结论：682.67 步/mm 正确 ✓")
        elif abs(avg_spm - 68.3) / 68.3 < 0.05:
            print("  结论：68.3 步/mm 正确（÷10 规律成立）")
        else:
            print(f"  结论：都不匹配，实测值 {avg_spm:.2f}，需要进一步分析")
    else:
        print("  未输入实测数据，无法计算")

    arduino.close()
    relay.close()
    print("\n  测试完成，串口已关闭。")


if __name__ == "__main__":
    main()
