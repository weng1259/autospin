"""一次性恢复：把被 EMI 抖坏的 grbl 参数全部写回 + 清 alarm。

注意：此脚本只动 grbl 串口，不动 DSTUR 继电器——Z 悬空时继电器状态千万别碰。
"""
import serial
import time

PORT = "/dev/cu.wchusbserial110"

# 所有已知会被抖坏的关键参数（按历次诊断累积）
FIXES = [
    ("$0=10", "step pulse width us（130us 会让高速脉冲不可用）"),
    ("$1=255", "step idle delay，保持励磁（防 Z 下落）"),
    ("$3=6", "Y/Z 方向取反"),
    ("$4=0", "step enable 不反相"),
    ("$5=1", "NPN 反相"),
    ("$10=2", "status report mask"),
    ("$21=1", "hard-limits"),
    ("$22=1", "homing enable"),
    ("$23=0", "归零方向 = 全 +"),
    ("$24=25.000", "homing feed（慢速定位，保守值）"),
    ("$25=500.000", "homing seek（快速寻边，1500 会复现 ALARM:9）"),
    ("$26=250", "homing debounce ms"),
    ("$27=5.000", "pull-off 5mm"),
    ("$100=682.670", "X steps/mm（51200 脉冲/转 ÷ 75mm/转）"),
    ("$101=682.670", "Y steps/mm（51200 脉冲/转 ÷ 75mm/转）"),
    ("$102=682.670", "Z steps/mm（51200 脉冲/转 ÷ 75mm/转）"),
    ("$110=3000.000", "X max rate"),
    ("$111=3000.000", "Y max rate"),
    ("$112=3000.000", "Z max rate"),
    ("$120=200.000", "X acceleration"),
    ("$121=200.000", "Y acceleration"),
    ("$122=200.000", "Z acceleration"),
    ("$130=280.000", "X max travel"),
    ("$131=280.000", "Y max travel"),
    ("$132=95.000", "Z max travel"),
]


def read_until_ok(s, timeout=3.0):
    end = time.time() + timeout
    lines = []
    while time.time() < end:
        line = s.readline().decode("utf-8", errors="replace").strip()
        if not line:
            continue
        lines.append(line)
        if line == "ok" or line.startswith("error"):
            break
    return lines


s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(2.0)
while s.in_waiting:
    s.readline()

# 1. 清 alarm
print("--- $X ---")
s.write(b"$X\n")
s.flush()
for line in read_until_ok(s):
    print(f"  {line}")

# 2. 全部 fixes
for cmd, desc in FIXES:
    print(f"\n--- {cmd}  # {desc} ---")
    s.write(f"{cmd}\n".encode())
    s.flush()
    for line in read_until_ok(s):
        print(f"  {line}")

# 3. 验证关键参数
print("\n--- 验证 $$ ---")
s.write(b"$$\n")
s.flush()
end = time.time() + 5.0
seen = {}
while time.time() < end:
    line = s.readline().decode("utf-8", errors="replace").strip()
    if not line:
        continue
    if line == "ok":
        break
    if line.startswith("$") and "=" in line:
        k, v = line.split("=", 1)
        seen[k] = v

for cmd, _ in FIXES:
    k, expected = cmd.split("=")
    actual = seen.get(k, "?")
    mark = "✓" if actual == expected else f"✗ 实际 {actual}"
    print(f"  {k}={expected}  {mark}")

# 4. 状态快照
print("\n--- ? ---")
s.write(b"?")
s.flush()
time.sleep(0.3)
while s.in_waiting:
    line = s.readline().decode("utf-8", errors="replace").strip()
    if line:
        print(f"  {line}")

s.close()
print("\n完成。Z 当前靠刹车 + 励磁双保险。接下来可以重启 agent_smoke.py 尝试归零。")
