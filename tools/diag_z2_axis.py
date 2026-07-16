"""Z2 (grbl A 轴) 手动诊断 — 阶段 1：纯只读，不发任何运动指令。

\x18 软重启只复位固件状态机，不发 step 脉冲。然后读 ?, $$, $I, $G。
重点看 A 轴：$103/$113/$123/$133，以及 $3/$1/$22。
"""
import serial, time, sys

PORT = "/dev/autospin_xyz"

def drain(s, t=0.6):
    end = time.time() + t; out = []
    while time.time() < end:
        if s.in_waiting:
            ln = s.readline().decode("utf-8", "replace").strip()
            if ln: out.append(ln)
        else:
            time.sleep(0.04)
    return out

s = serial.Serial(PORT, 115200, timeout=0.3)
time.sleep(2.0); drain(s)

print("=== \\x18 soft-reset banner ===")
s.write(b"\x18"); s.flush(); time.sleep(2.0)
for l in drain(s, 2.0): print(" ", l)

print("\n=== ? status ===")
s.write(b"?"); s.flush(); time.sleep(0.4)
for l in drain(s, 1.0): print(" ", l)

print("\n=== $I build ===")
s.write(b"$I\n"); s.flush(); time.sleep(0.4)
for l in drain(s, 1.0): print(" ", l)

print("\n=== $G parser state ===")
s.write(b"$G\n"); s.flush(); time.sleep(0.4)
for l in drain(s, 1.0): print(" ", l)

print("\n=== $$ (A-axis + key safety params) ===")
s.write(b"$$\n"); s.flush(); time.sleep(0.5)
WATCH = {"$1","$3","$4","$20","$21","$22","$23",
         "$103","$113","$123","$133",
         "$100","$110","$120","$130"}
for l in drain(s, 5.0):
    if l.startswith("$") and "=" in l:
        k = l.split("=")[0]
        if k in WATCH:
            print(" ", l)
s.close()
print("\nDONE")
