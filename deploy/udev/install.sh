#!/usr/bin/env bash
# 安装/刷新 AutoSpin 串口稳定命名 udev 规则。幂等、自愈。需 sudo。
# 用法: deploy/udev/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="$HERE/99-autospin.rules"
DST="/etc/udev/rules.d/99-autospin.rules"

echo "[autospin-udev] 安装规则 -> $DST"
sudo install -o root -g root -m 0644 "$SRC" "$DST"

echo "[autospin-udev] 清掉残留旧软链（避免插拔后不刷新的陈旧链接）"
sudo rm -f /dev/autospin_xyz /dev/autospin_rs485 /dev/autospin_heater /dev/autospin_relay

echo "[autospin-udev] 重载规则 + 触发 add 事件重建"
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add --subsystem-match=tty
sleep 1.5

echo "[autospin-udev] 生成结果:"
if ! ls -l /dev/autospin_* 2>/dev/null; then
  echo "!! 没有 autospin_* 软链 —— 检查 grbl 是否插 1-1 口、RS485 模块是否插 1-2 口" >&2
  exit 1
fi
echo "[autospin-udev] 完成。"
