#!/usr/bin/env bash
# 在开发机从仓库根目录的 .venv 启动服务（默认开启代码热重载）
# Docker 模式（将整个仓库挂载进容器，便于 onlineProject ↔ layers 与宿主 IDE 同步）：
#   TRAE_ONLINE_DOCKER=1 ./onlineService/run_local.sh
set -euo pipefail
# 固定项目根目录，避免在某些环境下被误判为上一级目录
ROOT="/Users/task2app/gitClone/ramDisk/ram-mount/trae-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if [[ "${TRAE_ONLINE_DOCKER:-0}" == "1" || "${TRAE_ONLINE_DOCKER:-0}" == "true" ]]; then
  IMAGE="${TRAU_ONLINE_IMAGE:-trae-online-service:latest}"
  PORT="${PORT:-8765}"
  export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
  export REPO_ROOT="${REPO_ROOT:-$ROOT}"
  export TRAE_VENV="${TRAE_VENV:-$ROOT/.venv}"
  export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
  _local_no_proxy="localhost,127.0.0.1,::1"
  export NO_PROXY="${_local_no_proxy}${NO_PROXY:+,${NO_PROXY}}"
  export no_proxy="${_local_no_proxy}${no_proxy:+,${no_proxy}}"
  exec docker run --rm -i \
    -p "${PORT}:${PORT}" \
    -v "${ROOT}:${ROOT}" \
    -e "REPO_ROOT=${ROOT}" \
    -e "TRAE_VENV=${ROOT}/.venv" \
    -e "PYTHONPATH=${PYTHONPATH}" \
    -e "ACCESS_TOKEN=${ACCESS_TOKEN}" \
    -e "NO_PROXY=${NO_PROXY:-}" \
    -e "no_proxy=${no_proxy:-}" \
    -w "${ROOT}/onlineService" \
    "${IMAGE}" \
    "${ROOT}/.venv/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}"
fi
# 本机 Task API（localhost）若继承全局 HTTP(S)_PROXY，urllib 会走代理导致 exchange-refresh 等超时
_local_no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="${_local_no_proxy}${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="${_local_no_proxy}${no_proxy:+,${no_proxy}}"
export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
export BusinessApiEndPoint="${BusinessApiEndPoint:-${BUSINESS_API_ENDPOINT:-http://127.0.0.1:${PORT:-8765}/api}}"
export REPO_ROOT="${REPO_ROOT:-$ROOT}"
export TRAE_VENV="${TRAE_VENV:-$ROOT/.venv}"
# 允许从 onlineService 子目录启动时导入仓库根下的 `trae_agent` 包
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
# macOS 自带 git 使用 LibreSSL，克隆 GitHub 时易出现 SSL_ERROR_SYSCALL；默认强制 IPv4 可缓解（需关闭时: GIT_HTTP_IPV4=0 ./run_local.sh）
if [[ "$(uname -s)" == Darwin ]]; then
  export GIT_HTTP_IPV4="${GIT_HTTP_IPV4:-1}"
fi
# 若克隆 GitHub 仍报 SSL/代理相关错误，可尝试：GIT_CLONE_UNSET_PROXY=1 ./run_local.sh（仅去掉 git 子进程的代理变量）
if command -v uv >/dev/null 2>&1; then
  # requirements.txt 中使用了 `-e ..[online]`，需在 onlineService 目录执行
  (cd "$SCRIPT_DIR" && uv pip install -q -r requirements.txt)
else
  "$ROOT/.venv/bin/python" -m pip install -q -r "$SCRIPT_DIR/requirements.txt"
fi
# 状态与日志在 REPO_ROOT/onlineProject_state（见 app.paths.state_root），不在 onlineService 下，避免热重载监视写入
STATE_ROOT="${ONLINE_PROJECT_STATE_ROOT:-$ROOT/onlineProject_state}"
export ONLINE_PROJECT_STATE_ROOT="$STATE_ROOT"
mkdir -p "$STATE_ROOT/runtime" "$STATE_ROOT/logs" "$STATE_ROOT/reqLogs" "$STATE_ROOT/layers"
# 关闭热重载：UVICORN_RELOAD=0 ./run_local.sh
RELOAD_ARGS=()
if [[ "${UVICORN_RELOAD:-1}" != "0" ]]; then
  RELOAD_ARGS=(
    --reload
    --reload-dir "$SCRIPT_DIR"
    --reload-include "*.html"
    --reload-include "*.md"
  )
fi
CMD=(
  "$ROOT/.venv/bin/python"
  -m uvicorn
  app.main:app
  --host "${HOST:-0.0.0.0}"
  --port "${PORT:-8765}"
)
if [[ "${#RELOAD_ARGS[@]}" -gt 0 ]]; then
  CMD+=("${RELOAD_ARGS[@]}")
fi
exec "${CMD[@]}"
