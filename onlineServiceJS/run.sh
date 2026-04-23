#!/usr/bin/env bash
# 本地启动 onlineServiceJS（Node）。请在任意目录执行：/path/to/onlineServiceJS/run.sh
#
# 常用环境变量（均可选，有默认值）：
#   ACCESS_TOKEN      默认 dev-local-token
#   PORT              默认 8765
#   REPO_ROOT         默认 onlineServiceJS 的上一级（trae-agent 仓库根）
#   TRAE_VENV         默认 $REPO_ROOT/.venv（供真实 trae-cli 任务）
#   TRAE_CLI          若设置，trae 任务直接调用该可执行文件
#   ONLINE_PROJECT_STATE_ROOT / ONLINE_PROJECT_LAYERS  见 skill.md
#   NODEJS_WATCH      默认开启：node --watch，改动 src/*.mjs 等依赖树会自动重启。
#                     设为 0 / false 则单次运行（与生产行为一致）。
#   若 PORT 已被监听，本脚本会先 kill -9 占用该端口的进程再启动（本地开发约定）。
#
# Docker（构建上下文须为 trae-agent 根目录）：
#   TRAE_ONLINE_JS_DOCKER=1 ./run.sh
#   IMAGE=trae-online-js:local TRAE_ONLINE_JS_DOCKER=1 ./run.sh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

REPO_ROOT="${REPO_ROOT:-$(cd "$SCRIPT_DIR/.." && pwd)}"
export REPO_ROOT
PORT="${PORT:-8765}"
export PORT
export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"

_local_no_proxy="localhost,127.0.0.1,::1"
export NO_PROXY="${_local_no_proxy}${NO_PROXY:+,${NO_PROXY}}"
export no_proxy="${_local_no_proxy}${no_proxy:+,${no_proxy}}"

export BusinessApiEndPoint="${BusinessApiEndPoint:-${BUSINESS_API_ENDPOINT:-http://127.0.0.1:${PORT}/api}}"

STATE_ROOT="${ONLINE_PROJECT_STATE_ROOT:-$REPO_ROOT/onlineProject_state}"
export ONLINE_PROJECT_STATE_ROOT="$STATE_ROOT"
mkdir -p "$STATE_ROOT/runtime" "$STATE_ROOT/logs" "$STATE_ROOT/reqLogs" "$STATE_ROOT/layers"

export TRAE_VENV="${TRAE_VENV:-$REPO_ROOT/.venv}"

if [[ "$(uname -s)" == Darwin ]]; then
  export GIT_HTTP_IPV4="${GIT_HTTP_IPV4:-1}"
fi

if [[ "${TRAE_ONLINE_JS_DOCKER:-0}" == "1" || "${TRAE_ONLINE_JS_DOCKER:-0}" == "true" ]]; then
  IMAGE="${TRAE_ONLINE_JS_IMAGE:-trae-online-js:local}"
  if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[run.sh] 镜像不存在，正在从仓库根构建: $IMAGE" >&2
    docker build -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" "$REPO_ROOT"
  fi
  exec docker run --rm -i \
    -p "${PORT}:${PORT}" \
    -e "ACCESS_TOKEN=${ACCESS_TOKEN}" \
    -e "PORT=${PORT}" \
    -e "REPO_ROOT=/app" \
    -e "BusinessApiEndPoint=${BusinessApiEndPoint}" \
    "${IMAGE}"
fi

if [[ ! -d node_modules ]]; then
  echo "[run.sh] 首次运行：npm install" >&2
  npm install --omit=dev
fi

echo "[run.sh] REPO_ROOT=$REPO_ROOT PORT=$PORT ACCESS_TOKEN=(set)" >&2
echo "[run.sh] 控制台: http://127.0.0.1:${PORT}/ui/${ACCESS_TOKEN}" >&2

_pids="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null || true)"
if [[ -n "$_pids" ]]; then
  _plist="${_pids//$'\n'/, }"
  echo "[run.sh] 端口 ${PORT} 已被占用（PID: ${_plist}），正在结束占用进程…" >&2
  # shellcheck disable=SC2086
  kill -9 $_pids 2>/dev/null || true
  sleep 0.5
  _pids="$(lsof -nP -iTCP:"${PORT}" -sTCP:LISTEN -t 2>/dev/null || true)"
  if [[ -n "$_pids" ]]; then
    echo "[run.sh] 错误: 结束占用后端口 ${PORT} 仍被监听（PID: ${_pids//$'\n'/, }）。请手动检查或换端口: PORT=8766 ./run.sh" >&2
    exit 1
  fi
fi

_watch_flag=()
_js_watch="${NODEJS_WATCH:-1}"
if [[ "${_js_watch}" != "0" && "${_js_watch}" != "false" ]]; then
  _watch_flag=(--watch)
  echo "[run.sh] 热加载已启用: node --watch（关闭请设 NODEJS_WATCH=0）" >&2
fi
exec node "${_watch_flag[@]}" src/server.mjs
