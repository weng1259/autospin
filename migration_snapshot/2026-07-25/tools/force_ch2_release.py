"""一次性恢复脚本：强制 DSTUR-T80 CH2 切到 ON（释放 Z 刹车）。

使用场景：RelayBackend state-memo 与物理状态失同步（Issue #025 自然触发后）。
协议：0xA0 [CH] [0x01=ON/0x00=OFF] [checksum=前三字节和]。
Bypass 所有 state-memo，直接写字节。
"""
import serial
import time

PORT = "/dev/cu.usbmodem6670E00119391"
CH = 2


def frame(ch: int, on: bool) -> bytes:
    op = 0x01 if on else 0x00
    s = 0xA0 + ch + op
    return bytes([0xA0, ch, op, s & 0xFF])


s = serial.Serial(PORT, 9600, timeout=1.0)
time.sleep(0.5)

# 双保险：先 OFF 再 ON，确保无论当前物理状态如何都会产生上升沿
s.write(frame(CH, False))
s.flush()
time.sleep(0.3)

s.write(frame(CH, True))
s.flush()
time.sleep(0.3)

s.close()
print(f"CH{CH} 已强制 ON。去看 DSTUR 板，CH{CH} LED 应该亮了（= Z 刹车释放）。")
