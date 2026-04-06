#!/usr/bin/env bash
# 在开发机从仓库根目录的 .venv 启动服务（默认开启代码热重载）
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
# 本机 Task API（localhost）若继承全局 HTTP(S)_PROXY，urllib 会走代理导致 exchange-refresh 等超时
_local_no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="${_local_no_proxy}${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="${_local_no_proxy}${no_proxy:+,${no_proxy}}"
export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
export REPO_ROOT="${REPO_ROOT:-$ROOT}"
export TRAE_VENV="${TRAE_VENV:-$ROOT/.venv}"
# macOS 自带 git 使用 LibreSSL，克隆 GitHub 时易出现 SSL_ERROR_SYSCALL；默认强制 IPv4 可缓解（需关闭时: GIT_HTTP_IPV4=0 ./run_local.sh）
if [[ "$(uname -s)" == Darwin ]]; then
  export GIT_HTTP_IPV4="${GIT_HTTP_IPV4:-1}"
fi
# 若克隆 GitHub 仍报 SSL/代理相关错误，可尝试：GIT_CLONE_UNSET_PROXY=1 ./run_local.sh（仅去掉 git 子进程的代理变量）
if command -v uv >/dev/null 2>&1; then
  (cd "$ROOT" && uv pip install -q -r onlineService/requirements.txt)
else
  "$ROOT/.venv/bin/python" -m pip install -q -r requirements.txt
fi
# 确保 runtime 目录存在，便于 --reload-exclude 将其识别为排除目录（避免 jobs_state 等写入触发重启）
mkdir -p "$SCRIPT_DIR/runtime"
# 关闭热重载：UVICORN_RELOAD=0 ./run_local.sh
RELOAD_ARGS=()
if [[ "${UVICORN_RELOAD:-1}" != "0" ]]; then
  RELOAD_ARGS=(
    --reload
    --reload-dir "$SCRIPT_DIR"
    --reload-exclude "$SCRIPT_DIR/runtime"
    --reload-include "*.html"
    --reload-include "*.md"
  )
fi
exec "$ROOT/.venv/bin/python" -m uvicorn app.main:app \
  --host "${HOST:-0.0.0.0}" \
  --port "${PORT:-8765}" \
  "${RELOAD_ARGS[@]}"
