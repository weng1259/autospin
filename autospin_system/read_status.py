# 纯只读：查移液枪当前寄存器状态，不写任何寄存器、不触发任何动作。
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.pipette import PipetteController


def main():
    c = PipetteController(port=CONFIG["communication"]["pipette_port"], mock=False)
    if not c.comm.connect():
        print("串口打不开")
        return 1
    comm = c.comm

    def r(a):
        try:
            return comm.read_input_registers(a)[0]
        except Exception as e:  # noqa: BLE001
            return f"ERR:{e}"

    ph, pl = r(3), r(4)
    pos = (ph << 16) | pl if isinstance(ph, int) and isinstance(pl, int) else "?"
    print("status_word  =", r(0), "(0=IDLE 2=加速 3=减速 4=匀速)")
    print("homed        =", r(1), "(1=已归位/位置可靠)")
    print("driver_fault =", r(2), "(0=正常)")
    print(f"position     = {pos}  (raw H={ph} L={pl})")
    print("tip_present  =", r(13), "(1=tip在位 0=无tip)")
    comm.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
