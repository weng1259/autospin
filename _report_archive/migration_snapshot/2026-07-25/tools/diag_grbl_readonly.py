"""只读诊断 + 软重启：\\x18 soft-reset 让 grbl 回到已知状态，然后读参数。

\\x18 只做软件 reset，不发 step 脉冲，不碰电机——在 Z 悬空时安全。
"""
import serial
import time

PORT = "/dev/cu.wchusbserial110"

EXPECTED = {
    "$0": "10", "$1": "255", "$3": "6", "$4": "0", "$5": "1",
    "$10": "2", "$20": "0", "$21": "1",
    "$22": "1", "$23": "0", "$24": "25.000", "$25": "500.000",
    "$26": "250", "$27": "5.000",
    "$100": "682.670", "$101": "682.670", "$102": "682.670",
    "$110": "3000.000", "$111": "3000.000", "$112": "3000.000",
    "$120": "200.000", "$121": "200.000", "$122": "200.000",
    "$130": "280.000", "$131": "280.000", "$132": "95.000",
}


def drain(s, timeout=0.5):
    end = time.time() + timeout
    out = []
    while time.time() < end:
        if s.in_waiting:
            line = s.readline().decode("utf-8", errors="replace").strip()
            if line:
                out.append(line)
        else:
            time.sleep(0.05)
    return out


s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(2.0)
drain(s)

# 1. Soft reset
print("=== \\x18 soft-reset ===")
s.write(b"\x18")
s.flush()
time.sleep(2.0)
for line in drain(s, timeout=2.0):
    print(f"  {line}")

# 2. 查状态
print("\n=== ? ===")
s.write(b"?")
s.flush()
time.sleep(0.5)
for line in drain(s, timeout=1.0):
    print(f"  {line}")

# 3. 全部参数
print("\n=== $$ ===")
s.write(b"$$\n")
s.flush()
time.sleep(0.5)
lines = drain(s, timeout=5.0)
for line in lines:
    if not line.startswith("$") or "=" not in line:
        continue
    key = line.split("=")[0]
    val = line.split("=", 1)[1]
    expected = EXPECTED.get(key)
    if expected is None:
        continue  # 不关心
    mark = "✓" if val == expected else f"✗ 期望 {expected}"
    print(f"  {line}  {mark}")

s.close()
