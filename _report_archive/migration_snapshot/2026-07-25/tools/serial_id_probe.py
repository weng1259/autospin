#!/usr/bin/env python3
"""安全识别串口身份：grbl(115200) vs DBLS400 Modbus(9600)。

全程只读：grbl 发 `?` 实时状态查询（无运动），Modbus 发 0x03 只读电压寄存器
（不写控制字，电机绝不转）。交叉发也无害——波特率/帧不符的一端只会丢弃乱码。

用法: python serial_id_probe.py [/dev/ttyUSB0 /dev/ttyUSB1 ...]
"""
import sys
import time
import struct
import serial


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def probe_grbl(port: str):
    try:
        with serial.Serial(port, 115200, timeout=1) as s:
            time.sleep(0.2)
            s.reset_input_buffer()
            s.write(b"?")        # 实时状态查询, 立即返回 <...>, 无运动
            time.sleep(0.3)
            s.write(b"\r\n")     # 触发一个 ok
            time.sleep(0.4)
            resp = s.read(200)
            txt = resp.decode("ascii", "replace")
            hit = any(k in txt for k in ("Grbl", "<Idle", "<Run", "<Alarm",
                                         "<Hold", "<Jog", "ok", "error"))
            clean = txt.strip().replace("\r", " ").replace("\n", " ")[:120]
            return hit, clean or "(无响应)"
    except Exception as e:  # noqa: BLE001
        return False, f"ERR {e}"


def probe_modbus(port: str, slave: int, reg: int, label: str):
    """读一个只读寄存器, 看是否有合法 Modbus 响应。"""
    try:
        with serial.Serial(port, 9600, timeout=0.6) as s:
            time.sleep(0.2)
            s.reset_input_buffer()
            frame = struct.pack(">BBHH", slave, 0x03, reg, 1)
            frame += struct.pack("<H", crc16(frame))
            s.write(frame)
            time.sleep(0.1)
            resp = s.read(7)
            if len(resp) >= 7 and resp[0] == slave and resp[1] == 0x03:
                rc = struct.unpack("<H", resp[-2:])[0]
                if rc == crc16(resp[:-2]):
                    lo, hi = resp[3], resp[4]
                    raw = (hi << 8) | lo
                    return True, f"{label} slave={slave} reg=0x{reg:04X} raw={raw}"
            return False, f"resp={resp.hex() or '(空)'}"
    except Exception as e:  # noqa: BLE001
        return False, f"ERR {e}"


def main():
    ports = sys.argv[1:] or ["/dev/ttyUSB0", "/dev/ttyUSB1"]
    for p in ports:
        print(f"\n=== {p} ===")
        g_hit, g_msg = probe_grbl(p)
        print(f"  grbl(115200)?        {'YES' if g_hit else 'no '}  {g_msg}")
        # DBLS400 电机 slave=2 读电压 0x8019
        m_hit, m_msg = probe_modbus(p, 2, 0x8019, "DBLS400电压")
        print(f"  DBLS400(9600,s2)?    {'YES' if m_hit else 'no '}  {m_msg}")
        # 加热台 slave=3 读 PV 40075(reg74→0x004A) 作 RS485 总线旁证
        h_hit, h_msg = probe_modbus(p, 3, 0x004A, "加热台PV")
        print(f"  加热台(9600,s3)?     {'YES' if h_hit else 'no '}  {h_msg}")
        verdict = "grbl 运动控制器" if g_hit else (
            "RS485 Modbus 总线" if (m_hit or h_hit) else "未识别")
        print(f"  >>> 判定: {verdict}")
    print()


if __name__ == "__main__":
    main()
