#!/usr/bin/env python3
"""X 轴最大行程压测 —— 验证 5mm 软限位余量是否抗抖动。

背景（2026-04-23）：
- 原 constants.yaml soft_limits = [-280, 0]，= 物理极限，零余量
- Agent 命令 x=0 → 命到 + 限位 → ALARM
- 改成 [-275, -5] 后用这个脚本直接压 10 次最大行程来回，看 + 限位
  pull-off 边缘会不会被传感器抖动误触发

直接调 GantryBackend（不走 Agent SDK），排除 LLM 决策干扰。

用法：
    tools/spikes/.venv/bin/python tools/stress_test_x_axis.py
    tools/spikes/.venv/bin/python tools/stress_test_x_axis.py --iter 20

失败判定：
- 任一段 move_to 出错 / 进 alarm → 打印错误立即退出，保留现场让人看
- 成功：所有段 IDLE 结束，打印每段耗时
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.hardware.errors import L3Error
from src.hardware.gantry_backend import GantryBackend
from src.hardware.types import MachineState, Position


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--iter", type=int, default=10, help="来回段数（默认 10）")
    ap.add_argument("--feed", type=float, default=3000.0, help="feed mm/min（默认上限 3000）")
    ap.add_argument("--x-lo", type=float, default=-275.0, help="左端目标 mm")
    ap.add_argument("--x-hi", type=float, default=-5.0, help="右端目标 mm")
    ap.add_argument("--y", type=float, default=-5.0, help="Y 保持位（归零后 pull-off）")
    ap.add_argument("--z", type=float, default=-5.0, help="Z 保持位（归零后 pull-off）")
    args = ap.parse_args()

    print(f"连接 /dev/cu.wchusbserial110 ...")
    backend = GantryBackend()
    backend.connect()
    print("✓ 连接成功")

    try:
        st = backend.get_status()
        print(f"初始状态: {st.state.value} position={st.position} is_homed={st.is_homed}")

        if st.state == MachineState.ALARM:
            print("→ 当前 alarm 态，执行 recover_from_alarm（含重归）")
            rec = backend.recover_from_alarm(
                idempotency_key=f"stress-x-recover-{int(time.time())}"
            )
            print(f"  recover 完成: actions={rec.actions_taken} duration={rec.duration_ms/1000:.1f}s")
        elif not st.is_homed:
            print("→ 未归零，执行 home")
            hr = backend.home(idempotency_key=f"stress-x-home-{int(time.time())}")
            print(f"  home 完成: position={hr.position_after_pulloff} duration={hr.duration_ms/1000:.1f}s")
        else:
            print("→ 已归零，跳过 home")

        st = backend.get_status()
        print(f"压测前状态: {st.state.value} position={st.position}")

        targets = []
        for i in range(args.iter):
            # 偶数段去 x_lo，奇数段去 x_hi
            targets.append(Position(
                x_mm=args.x_lo if i % 2 == 0 else args.x_hi,
                y_mm=args.y,
                z_mm=args.z,
            ))

        print(f"\n=== 开始 {args.iter} 段压测 X ∈ [{args.x_lo}, {args.x_hi}] feed={args.feed} ===\n")

        durations = []
        t_start = time.time()
        for i, tgt in enumerate(targets, 1):
            t0 = time.time()
            try:
                res = backend.move_to(tgt, feed_mm_min=args.feed)
            except L3Error as e:
                print(f"\n❌ 段 {i}/{args.iter} 失败 → {type(e).__name__}")
                print(f"   human: {e.human_message}")
                print(f"   agent: {e.agent_message}")
                post = backend.get_status()
                print(f"   现场状态: {post.state.value} position={post.position}")
                print(f"\n已成功 {i-1} 段，累计 {time.time()-t_start:.1f}s")
                return 1
            d = res.duration_ms / 1000
            durations.append(d)
            post = backend.get_status()
            print(
                f"段 {i:2d}/{args.iter}  → x={tgt.x_mm:7.1f}  "
                f"final=(x={res.final_position.x_mm:8.3f}, y={res.final_position.y_mm:7.3f}, z={res.final_position.z_mm:7.3f})  "
                f"duration={d:5.2f}s  state={post.state.value}"
            )
            if post.state != MachineState.IDLE:
                print(f"\n❌ 段 {i} 结束后机器不在 IDLE（实际 {post.state.value}），中止压测")
                return 1

        total = time.time() - t_start
        avg = sum(durations) / len(durations)
        print(f"\n✅ {args.iter} 段全部完成，累计 {total:.1f}s，平均 {avg:.2f}s/段")
        print(f"   最快 {min(durations):.2f}s，最慢 {max(durations):.2f}s")
        return 0

    finally:
        backend.close()


if __name__ == "__main__":
    sys.exit(main())
