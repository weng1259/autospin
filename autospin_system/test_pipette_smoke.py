# 28-series pipette bring-up smoke test.
#
# Uses the brother's PipetteController (same class as Maestro), but the bring-up
# verification deliberately does NOT trust controller.home()/wait_for_idle():
# that helper judges "done" by status_word==0 (IDLE), which reads IDLE in the
# few ms before the plunger even starts, so it returns instantly and homed never
# becomes 1. (Fixing the driver = Chunk 5 ADR-004 shell, not this card.)
#
# Instead we send the documented motion commands at the comm layer and POLL the
# real evidence registers: homed (input reg 1) for homing, position (input regs
# 3/4) deltas for aspirate/dispense.
#
# Shared RS485 bus, slave_id 1, 115200. Manual: 运动控制(保持reg0) bit3-0:
#   0x01 原点回归 / 0x0A 吸液 / 0x0B 吐液 ; 运动量 = 保持reg6(H)/7(L).
#
# Usage (on Pi, from ~/autospin):
#   .venv/bin/python -m autospin_system.test_pipette_smoke            # connect + home + status
#   .venv/bin/python -m autospin_system.test_pipette_smoke --cycles 3 # + aspirate/dispense 50uL x3
import argparse
import logging
import sys
import time
from pathlib import Path

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))

from autospin_system.config.hardware_config import CONFIG
from autospin_system.hardware.pipette import PipetteController
from autospin_system.hardware.pipette.pipette_controller import Registers, ActionCode

__test__ = False


def _r(comm, addr):
    try:
        return comm.read_input_registers(addr)[0]
    except Exception:
        return None


def snap(comm):
    sw = _r(comm, Registers.STATUS)
    homed = _r(comm, Registers.HOMED)
    fault = _r(comm, Registers.DRIVER_FAULT)
    ph, pl = _r(comm, Registers.POS_H), _r(comm, Registers.POS_L)
    pos = (ph << 16) | pl if (ph is not None and pl is not None) else None
    tip = _r(comm, Registers.TIP_PRESENT)
    return {"sw": sw, "homed": homed, "fault": fault, "pos": pos, "tip": tip}


def do_home(comm, timeout=30):
    print("  归位: write 运动控制(reg0)=原点回归(0x01), 轮询 homed 寄存器 ...")
    comm.write_register(Registers.CTRL, ActionCode.HOME)  # reg0 <- 0x01 (动作码已修正)
    start = time.time()
    last = None
    while time.time() - start < timeout:
        s = snap(comm)
        key = (s["sw"], s["homed"], s["pos"])
        if key != last and s["sw"] is not None:
            print(f"    t={time.time()-start:5.1f}s sw={s['sw']} homed={s['homed']} "
                  f"pos={s['pos']} fault={s['fault']}")
            last = key
        if s["homed"] == 1:
            print(f"    -> homed=1 归位完成 (t={time.time()-start:.1f}s)")
            return True
        time.sleep(0.2)
    print(f"    -> 超时 {timeout}s, homed 仍未变 1")
    return False


def do_motion(comm, action, vol, label, timeout=15):
    high, low = (vol >> 16) & 0xFFFF, vol & 0xFFFF
    comm.write_registers(Registers.VOL_H, [high, low])  # reg6/7 <- 运动量
    comm.write_register(Registers.CTRL, action)         # reg0 <- 0x0A/0x0B
    start = time.time()
    p0 = snap(comm)["pos"]
    last = None
    seen_motion = False
    while time.time() - start < timeout:
        s = snap(comm)
        key = (s["sw"], s["pos"])
        if key != last and s["sw"] is not None:
            print(f"    [{label}] t={time.time()-start:5.1f}s sw={s['sw']} pos={s['pos']}")
            last = key
        if s["sw"] not in (0, None):
            seen_motion = True
        if seen_motion and s["sw"] == 0:
            dp = (s["pos"] - p0) if (s["pos"] is not None and p0 is not None) else "?"
            print(f"    [{label}] 完成 (t={time.time()-start:.1f}s, Δpos={dp})")
            return True
        time.sleep(0.1)
    print(f"    [{label}] 超时 {timeout}s")
    return False


def print_status(controller, label=""):
    try:
        st = controller.get_status()
    except Exception as exc:  # noqa: BLE001
        print(f"  get_status() FAILED: {type(exc).__name__}: {exc}")
        return None
    if label:
        print(f"  [{label}]")
    for k in ("is_initialized", "homed", "tip_present", "driver_fault",
              "status_word", "position", "aspirate_state", "dispense_state",
              "liquid_detect_state", "max_volume"):
        print(f"    {k:20s}= {st[k]}")
    return st


def main():
    ap = argparse.ArgumentParser(description="28-series pipette bring-up smoke")
    ap.add_argument("--cycles", type=int, default=0,
                    help="aspirate->dispense 循环次数 (需 tip+液源)。0=只 home+读状态")
    ap.add_argument("--volume", type=int, default=50, help="每次吸吐体积 uL (默认 50)")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(message)s")
    logger = logging.getLogger("PipetteSmoke")

    port = CONFIG["communication"].get("pipette_port", "COM3")
    cfg = CONFIG["devices"]["pipette"]
    print("=" * 64)
    print("28系列移液枪 bring-up smoke")
    print(f"Config: PORT={port}, SLAVE_ID={cfg['slave_id']}, "
          f"BAUDRATE={cfg['baudrate']}, timeout={cfg['timeout']}s, "
          f"max_volume={cfg['max_volume_ul']}uL")
    print("⚠️  归位会让柱塞移动。确认行程无卡阻、tip 不撞东西。")
    print("=" * 64)

    controller = PipetteController(port=port, mock=False, logger=logger)
    comm = controller.comm

    # 阶段1: 师兄 controller.connect()（含 _initialize_pipette 的归位）——记录其行为
    print("\n[1] controller.connect()（师兄路径，含自动归位 + wait_for_idle 判定）:")
    ok = controller.connect()
    print(f"    connect() -> {ok}")
    st1 = print_status(controller)
    if st1 is None:
        print("\n❌ 状态读不出 —— 总线没通 / slave_id / baud。")
        controller.close()
        return 1
    if not st1["homed"]:
        print("    ⚠️ connect 后 homed=False —— 复现 wait_for_idle 竞态（驱动 bug，Chunk 5 修）。")

    # 阶段2: bring-up 正确归位 —— 轮询 homed 寄存器
    print("\n[2] bring-up 归位（轮询 homed 寄存器，绕过竞态判定）:")
    homed_ok = do_home(comm, timeout=30)
    st2 = print_status(controller, "归位后")
    if not homed_ok or st2 is None or st2["homed"] != 1:
        print("\n❌ 归位未确认（homed 未变 1）。柱塞动了吗？查行程/限位/驱动故障。")
        controller.close()
        return 1
    if st2["driver_fault"]:
        print("\n❌ driver_fault=True。")
        controller.close()
        return 1
    print("\n✅ 归位确认 (homed=1) + 状态可读 + 无 driver_fault —— 半个 gate 过。")

    if args.cycles <= 0:
        print("\n(--cycles=0：跳过吸吐。装好 tip+液源后加 --cycles 3 跑完整 gate。)")
        controller.close()
        return 0

    if not st2["tip_present"]:
        print("\n❌ tip_present=False —— 无 tip，吸液无意义。装 tip 后重跑。")
        controller.close()
        return 1

    print(f"\n[3] aspirate→dispense ×{args.cycles}  vol={args.volume}uL（盯柱塞/液体）:")
    failures = 0
    for i in range(1, args.cycles + 1):
        print(f"  --- cycle {i}/{args.cycles} ---")
        a = do_motion(comm, ActionCode.ASPIRATE, args.volume, f"吸液{i}")
        time.sleep(0.5)
        d = do_motion(comm, ActionCode.DISPENSE, args.volume, f"吐液{i}")
        if not (a and d):
            failures += 1
        time.sleep(0.5)

    controller.close()
    if failures:
        print(f"\n⚠️  吸吐阶段 {failures}/{args.cycles} 循环有动作未确认，见上方明细。")
        return 1
    print(f"\n✅ 吸吐阶段：{args.cycles}/{args.cycles} 循环全部确认完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
