#!/usr/bin/env bash
# 绿基线一条命令：每个开发会话开始前和提交前都要跑，任何一步红 = 停下修复，禁止带病施工。
set -euo pipefail
cd "$(dirname "$0")/.."
# venv 位置：Pi SoR = .venv/，Mac 档案 = tools/spikes/.venv/
PY=""
for cand in .venv/bin/python tools/spikes/.venv/bin/python; do
  if [ -x "$cand" ]; then PY="$cand"; break; fi
done
if [ -z "$PY" ]; then echo "找不到 venv (.venv/ 或 tools/spikes/.venv/)" >&2; exit 1; fi

echo "── pytest ──"
$PY -m pytest tests -q
echo "── mypy --strict ──"
$PY -m mypy --strict src
echo "── schema 合同 diff ──"
$PY -m src.schema_export --out /tmp/api-v1.check.json
diff -u docs/api-v1.json /tmp/api-v1.check.json
echo "ALL GREEN"
