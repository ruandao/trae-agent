#!/usr/bin/env bash
# 在开发机从仓库根目录的 .venv 启动服务
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$(dirname "$0")"
export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
export REPO_ROOT="${REPO_ROOT:-$ROOT}"
export TRAE_VENV="${TRAE_VENV:-$ROOT/.venv}"
if command -v uv >/dev/null 2>&1; then
  (cd "$ROOT" && uv pip install -q -r onlineService/requirements.txt)
else
  "$ROOT/.venv/bin/python" -m pip install -q -r requirements.txt
fi
exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app --host "${HOST:-0.0.0.0}" --port "${PORT:-8765}"
