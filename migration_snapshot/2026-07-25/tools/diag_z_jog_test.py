"""Z 机械装好后的验证：从底部朝上 30mm 回底部（Z+ 先，Z- 回）。

Z 在底部 → 朝 + 走 30mm（物理朝上）→ 朝 - 回 30mm（物理朝下）回底部
两档速度：500（普通）+ 1500（归零 seek 速度）
"""
import serial
import time

PORT = "/dev/cu.wchusbserial110"


def read_until_ok(s, timeout=10.0):
    end = time.time() + timeout
    while time.time() < end:
        line = s.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        print(f"  {line}")
        if line == "ok" or line.startswith("error") or line.startswith("ALARM"):
            return line


def query_status(s):
    s.write(b"?")
    s.flush()
    time.sleep(0.3)
    lines = []
    while s.in_waiting:
        line = s.readline().decode("utf-8", errors="replace").strip()
        if line:
            lines.append(line)
    return lines


def wait_idle(s, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        for line in query_status(s):
            if line.startswith("<Idle"):
                return line
            if line.startswith("<Alarm"):
                return line
        time.sleep(0.2)
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

print("\n--- 起点 ---")
for line in query_status(s):
    print(f"  {line}")

# 慢速先：上 30 → 下 30（回底部）
jog(s, +30, 500, "慢速上 500")
time.sleep(1.0)
jog(s, -30, 500, "慢速下 500（回底部）")
time.sleep(1.5)

# 快速：上 30 → 下 30
jog(s, +30, 1500, "快速上 1500")
time.sleep(1.0)
jog(s, -30, 1500, "快速下 1500（回底部）")

print("\n--- 终点 ---")
for line in query_status(s):
    print(f"  {line}")

s.close()
print("\n完成。告诉我：")
print("  1. 慢速上/下 声音如何？")
print("  2. 快速上/下 声音如何？（特别注意朝上）")
print("  3. Z 物理是否回到起点（底部）？")
