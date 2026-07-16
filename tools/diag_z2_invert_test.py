"""Z2 (A 轴) 方向反相 + 1mm 向下测试。
$3: 6 -> 14 (加 A 轴 bit3=8), $X 解锁, G91, G1 A1 F30 慢速下移 1mm。
"""
import serial, time
PORT = "/dev/autospin_xyz"

def drain(s, t=0.6):
    end = time.time() + t; out = []
    while time.time() < end:
        if s.in_waiting:
            ln = s.readline().decode("utf-8","replace").strip()
            if ln: out.append(ln)
        else: time.sleep(0.04)
    return out

def cmd(s, c, t=1.0, show=True):
    s.write((c + "\n").encode() if c not in ("?",) else c.encode())
    s.flush(); time.sleep(0.15)
    lines = drain(s, t)
    if show:
        for l in lines: print(f"  [{c}] {l}")
    return lines

s = serial.Serial(PORT, 115200, timeout=0.3); time.sleep(2.0); drain(s)
print("=== soft-reset ==="); s.write(b"\x18"); s.flush(); time.sleep(2.0)
for l in drain(s,2.0): print("  ",l)

print("\n=== 写 $3=14 (反相 A 轴) ==="); cmd(s,"$3=14",1.0)
print("\n=== 复读 $$ 找 $3 / $103 ===")
s.write(b"$$\n"); s.flush(); time.sleep(0.4)
for l in drain(s,4.0):
    if l.startswith(("$3=","$103=","$113=","$123=","$133=")): print("  ",l)

print("\n=== $X 解锁 ==="); cmd(s,"$X",1.0)
print("=== ? 状态 ==="); 
for l in cmd(s,"?",1.0,show=False): print("  ",l)

print("\n=== G91 相对模式 ==="); cmd(s,"G91",0.6)
print("\n>>> 现在发 G1 A1 F30 —— 盯住滑台! (1mm 向下, 约 2 秒) <<<")
cmd(s,"G1 A1 F30",1.0)
# 轮询直到 Idle
print("--- 等运动完成 ---")
for i in range(20):
    st = cmd(s,"?",0.5,show=False)
    line = st[0] if st else ""
    print("  ", line)
    if "Idle" in line or "Alarm" in line: break
    time.sleep(0.3)
s.close(); print("\nDONE")
