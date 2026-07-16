# 诊断：找出 28 系列移液枪正确的归位触发。
# 师兄 home() 写「运动控制寄存器(保持reg0)=0x00」无效（status_word/homed/pos 全不变）。
# 手册线索：5.4.3「复位：每次连接后需进行复位操作」；线圈 reg0=自动原点回归(0不启用/1启用)。
# 安全：只写 线圈reg0 / 运动控制reg0 / 运动电流reg1-2 / 速度reg3。绝不写 线圈reg2(恢复出厂)。
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.pipette import PipetteController


def _r(comm, a):
    try:
        return comm.read_input_registers(a)[0]
    except Exception:
        return None


def state(comm):
    sw, homed = _r(comm, 0), _r(comm, 1)
    ph, pl = _r(comm, 3), _r(comm, 4)
    pos = (ph << 16) | pl if (ph is not None and pl is not None) else None
    return sw, homed, pos


def watch(comm, label, secs):
    start = time.time()
    last = None
    while time.time() - start < secs:
        cur = state(comm)
        if cur != last:
            print(f"  [{label}] t={time.time()-start:4.1f}s sw={cur[0]} homed={cur[1]} pos={cur[2]}")
            last = cur
        if cur[1] == 1:
            print(f"  [{label}] ✅ homed=1  (t={time.time()-start:.1f}s)")
            return True
        time.sleep(0.2)
    print(f"  [{label}] 超时 {secs}s, homed 未变 1")
    return False


def main():
    port = CONFIG["communication"]["pipette_port"]
    c = PipetteController(port=port, mock=False)
    if not c.comm.connect():
        print("串口打不开")
        return 1
    comm = c.comm
    print("初始状态 (sw, homed, pos):", state(comm))

    print("\n[A] write_coil(reg0=自动原点回归, 1)  ← 最可能的归位/复位触发:")
    comm.write_coil(0, 1)
    if watch(comm, "A", 18):
        comm.close()
        return 0

    print("\n[B] 设运动电流(reg1=60, reg2=40)+速度(reg3=10) 后 write 运动控制(reg0)=0x00:")
    comm.write_register(1, 60)
    comm.write_register(2, 40)
    comm.write_register(3, 10)
    comm.write_register(0, 0x00)
    if watch(comm, "B", 18):
        comm.close()
        return 0

    print("\n[C] 先 write 运动控制(reg0)=0x06(缓慢停止) 再 =0x00, 制造变化边沿:")
    comm.write_register(0, 0x06)
    time.sleep(0.3)
    comm.write_register(0, 0x00)
    if watch(comm, "C", 18):
        comm.close()
        return 0

    print("\n[D] write_coil(reg0,1) + write 运动控制(reg0)=0x00 组合:")
    comm.write_coil(0, 1)
    time.sleep(0.3)
    comm.write_register(0, 0x00)
    if watch(comm, "D", 18):
        comm.close()
        return 0

    print("\n四种触发都没让 homed 变 1。需要看指示灯/查厂家调试软件的复位实现。")
    comm.close()
    return 1


if __name__ == "__main__":
    sys.exit(main())
