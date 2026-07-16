#!/usr/bin/env bash
# 绿基线一条命令：每个开发会话开始前和提交前都要跑，任何一步红 = 停下修复，禁止带病施工。
set -euo pipefail
cd "$(dirname "$0")/.."
PY=.venv/bin/python

echo "── pytest ──"
$PY -m pytest tests -q
echo "── mypy --strict ──"
$PY -m mypy --strict src
echo "── schema 合同 diff ──"
$PY -m src.schema_export --out /tmp/api-v1.check.json
diff -u docs/api-v1.json /tmp/api-v1.check.json
echo "ALL GREEN"
