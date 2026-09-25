"""
步距测试：发 3200 个脉冲（= 1 转），量实际走了多少 mm。
- 走 ~5mm → 丝杠传动，640 步/mm
- 走 ~40mm → GT2 皮带传动，80 步/mm

使用方法：
1. 确保网页面板已关闭（避免串口冲突）
2. 在轴上用笔标记起始位置
3. 运行脚本
4. 用尺子量标记到当前位置的距离
"""

import serial
import time
import sys

ARDUINO_PORT = "/dev/cu.wchusbserial110"
BAUD = 115200
TARGET_STEPS = 3200  # 1 转

def main():
    axis = input("测试哪个轴？(X/Y/Z，默认 X): ").strip().upper() or "X"
    if axis not in ("X", "Y", "Z"):
        print("无效轴名")
        return

    print(f"\n连接 Arduino ({ARDUINO_PORT})...")
    try:
        ser = serial.Serial(ARDUINO_PORT, BAUD, timeout=1)
    except Exception as e:
        print(f"连接失败: {e}")
        print("请确认网页面板已关闭，串口未被占用")
        return

    time.sleep(2)  # 等待 Arduino 重启
    ser.reset_input_buffer()

    # 读取启动信息
    for _ in range(20):
        line = ser.readline().decode(errors='ignore').strip()
        if line:
            print(f"  ← {line}")
        if "READY" in line:
            break

    # 1. 先复位坐标
    print("\n[1] 复位坐标...")
    ser.write(b"HR\n")
    time.sleep(0.3)
    _drain(ser)

    # 2. 查询起始位置
    ser.write(b"P\n")
    time.sleep(0.3)
    start_pos = _read_position(ser, axis)
    print(f"[2] 起始位置: {axis} = {start_pos} 步")

    # 3. 设置步数为 100（减少发送次数）
    print(f"\n[3] 设置 {axis} 轴步数为 100...")
    # 先把步数调大，默认50，发一次 N 变成 100
    ser.write(f"{axis}N\n".encode())
    time.sleep(0.1)
    _drain(ser)

    # 4. 设置低速（安全起见）
    print(f"[4] 设置 {axis} 轴速度为 3 档（中低速）...")
    ser.write(f"{axis}3\n".encode())
    time.sleep(0.1)
    _drain(ser)

    # 5. 发送脉冲
    total_clicks = TARGET_STEPS // 100  # 3200 / 100 = 32 次
    print(f"\n[5] 准备发送 {TARGET_STEPS} 个脉冲（{total_clicks} 次 × 100 步）")
    print(f"    在 {axis} 轴上标记当前位置，然后按回车开始...")
    input()

    print("    发送中...")
    for i in range(total_clicks):
        ser.write(f"{axis}F\n".encode())
        time.sleep(0.15)  # 等每批完成
        # 简单进度
        if (i + 1) % 8 == 0:
            print(f"    进度: {(i+1)*100}/{TARGET_STEPS} 步 ({(i+1)*100*100//TARGET_STEPS}%)")

    # 等待运动完全结束
    time.sleep(1)
    _drain(ser)

    # 6. 查询终止位置
    ser.write(b"P\n")
    time.sleep(0.3)
    end_pos = _read_position(ser, axis)
    actual_steps = end_pos - start_pos
    print(f"\n[6] 终止位置: {axis} = {end_pos} 步")
    print(f"    实际走了: {actual_steps} 步")

    # 7. 结果
    print("\n" + "=" * 50)
    print(f"已发送 {TARGET_STEPS} 个脉冲，固件计数 {actual_steps} 步")
    print("=" * 50)
    print("\n请用尺子量从标记位置到当前位置的距离：")
    distance = input("实测距离 (mm): ").strip()
    if distance:
        try:
            d = float(distance)
            steps_per_mm = round(actual_steps / d, 1)
            print(f"\n结果: {steps_per_mm} 步/mm")
            if d > 20:
                print("→ GT2 皮带传动（~80 步/mm）")
            elif d > 3:
                print("→ 丝杠传动（~640 步/mm）")
            else:
                print("→ 数值异常，请检查")
        except ValueError:
            print("输入无效")

    # 恢复步数为默认 50
    ser.write(f"{axis}M\n".encode())
    time.sleep(0.1)

    ser.close()
    print("\n串口已关闭。")


def _drain(ser):
    """读掉缓冲区所有数据"""
    while ser.in_waiting:
        ser.readline()


def _read_position(ser, target_axis):
    """从串口读取 POS:x,y,z 并返回指定轴的值"""
    _drain(ser)  # 先清空
    ser.write(b"P\n")
    time.sleep(0.5)
    for _ in range(30):
        line = ser.readline().decode(errors='ignore').strip()
        if line.startswith("POS:"):
            # 格式: POS:1234,-567,890
            parts = line[4:].split(",")
            if len(parts) >= 3:
                idx = {"X": 0, "Y": 1, "Z": 2}[target_axis]
                return int(parts[idx])
    return 0


if __name__ == "__main__":
    main()
