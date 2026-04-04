#!/usr/bin/env bash
# 在运行本脚本前请先启动 onlineService（与代码版本一致），例如：
#   ./onlineService/run_local.sh
# 若本地 8765 上仍是旧进程导致 /api/requirements/task-gate 404，请重启后再跑。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$ROOT"
export TRAE_UI_BASE="${TRAE_UI_BASE:-http://127.0.0.1:8765}"
export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
export TRAE_E2E_REPO="${TRAE_E2E_REPO:-https://github.com/ruandao/somanyad.git}"
python3 -m pytest onlineService/e2e/test_trae_online_ui.py -v --tb=short "$@"
