"""Z 电机空载诊断：电机从龙门架拆下，只剩电机 + 相线 + 编码器线。

发送 jog 命令让电机空载旋转约 2 圈，用户手握电机观察：
- 旋转是否平稳匀速
- 是否有涩/卡/哒哒哒
- 扭矩感觉（用手轻阻挡，看电机是否能推过手的阻力）

安全注意：
- 电机在手里，相线 24V 不要短路到金属
- 电机轴可能有惯性，拿稳
- 空载电机通电发热，测试完尽快断电

距离 150mm = 2 圈（导程 75mm/转）
"""
import serial
import time

PORT = "/dev/cu.wchusbserial110"
DIST = 150  # 2 圈


def read_until_ok(s, timeout=30.0):
    end = time.time() + timeout
    while time.time() < end:
        line = s.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        print(f"  {line}")
        if line == "ok" or line.startswith("error") or line.startswith("ALARM"):
            return line


def wait_idle(s, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        s.write(b"?")
        s.flush()
        time.sleep(0.3)
        while s.in_waiting:
            line = s.readline().decode("utf-8", errors="replace").strip()
            if line.startswith("<Idle"):
                return line
            if line.startswith("<Alarm"):
                return line
        time.sleep(0.1)
    return None


def jog(s, delta_mm: float, feed: float, label: str):
    print(f"\n--- {label}: Z{delta_mm:+g}mm @ {feed:g}mm/min ---")
    s.write(f"$J=G91 Z{delta_mm:+g} F{feed:g}\n".encode())
    s.flush()
    read_until_ok(s)
    print(f"  结束: {wait_idle(s)}")


s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(2.0)
while s.in_waiting:
    s.readline()

print("--- $X ---")
s.write(b"$X\n")
s.flush()
read_until_ok(s)

# 慢速正反各 2 圈
jog(s, +DIST, 500, "慢速正转 2 圈")
time.sleep(1.5)
jog(s, -DIST, 500, "慢速反转 2 圈")
time.sleep(2.0)  # 让用户换手感受 / 喘口气

# 快速正反各 2 圈
jog(s, +DIST, 1500, "快速正转 2 圈（归零 seek 速度）")
time.sleep(1.5)
jog(s, -DIST, 1500, "快速反转 2 圈")

s.close()
print("\n完成。告诉我：")
print("  1. 空载旋转是否平稳匀速？")
print("  2. 有没有涩/卡/哒哒哒？")
print("  3. 用手轻挡电机轴，能感觉到扭矩吗？（应该能明显推过手指轻阻）")
