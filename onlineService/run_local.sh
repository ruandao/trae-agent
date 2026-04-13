#!/usr/bin/env bash
# 在开发机从仓库根目录的 .venv 启动服务（默认开启代码热重载）
# Docker 模式（将整个仓库挂载进容器，便于 onlineProject ↔ layers 与宿主 IDE 同步）：
#   TRAE_ONLINE_DOCKER=1 ./onlineService/run_local.sh
set -euo pipefail
# 固定项目根目录，避免在某些环境下被误判为上一级目录
ROOT="/Users/task2app/gitClone/ramDisk/ram-mount/trae-agent"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
# 浏览器 EventSource（/api/events/stream）等长连接存在时，uvicorn 默认 timeout_graceful_shutdown=None
# 会无限等待连接关闭；reload 父进程又在 join 子进程，表现为多次 Ctrl+C 仍卡住。
# 设为 0 可恢复 uvicorn 默认（不推荐本地使用）。
_GRACEFUL_SHUTDOWN="${UVICORN_TIMEOUT_GRACEFUL_SHUTDOWN:-10}"
_GRACEFUL_ARGS=()
if [[ "${_GRACEFUL_SHUTDOWN}" != "0" ]]; then
  _GRACEFUL_ARGS=(--timeout-graceful-shutdown "${_GRACEFUL_SHUTDOWN}")
fi

if [[ "${TRAE_ONLINE_DOCKER:-0}" == "1" || "${TRAE_ONLINE_DOCKER:-0}" == "true" ]]; then
  IMAGE="${TRAU_ONLINE_IMAGE:-trae-online-service:latest}"
  PORT="${PORT:-8765}"
  export ACCESS_TOKEN="${ACCESS_TOKEN:-dev-local-token}"
  export REPO_ROOT="${REPO_ROOT:-$ROOT}"
  export TRAE_VENV="${TRAE_VENV:-$ROOT/.venv}"
  export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"
  export BusinessApiEndPoint="${BusinessApiEndPoint:-${BUSINESS_API_ENDPOINT:-http://127.0.0.1:${PORT}/api}}"
  _local_no_proxy="localhost,127.0.0.1,::1"
  # 容器内访问宿主机请用 IP；由 Docker host-gateway 解析出数值地址供 DOCKER_HOST_GATEWAY_IP / 配置替换
  _trae_gw_ip="${DOCKER_HOST_GATEWAY_IP:-}"
  if [[ -z "${_trae_gw_ip}" ]] && command -v docker >/dev/null 2>&1; then
    _trae_gw_ip="$(docker run --rm --add-host=_trae_gw:host-gateway alpine:3.19 getent hosts _trae_gw 2>/dev/null | awk '{print $1}' || true)"
  fi
  if [[ -n "${_trae_gw_ip}" ]]; then
    _local_no_proxy="${_local_no_proxy},${_trae_gw_ip}"
  fi
  export NO_PROXY="${_local_no_proxy}${NO_PROXY:+,${NO_PROXY}}"
  export no_proxy="${_local_no_proxy}${no_proxy:+,${no_proxy}}"
  _docker_extra_env=()
  if [[ -n "${_trae_gw_ip}" ]]; then
    _docker_extra_env+=(-e "DOCKER_HOST_GATEWAY_IP=${_trae_gw_ip}")
  fi
  exec docker run --rm -i \
    -p "${PORT}:${PORT}" \
    -v "${ROOT}:${ROOT}" \
    -e "REPO_ROOT=${ROOT}" \
    -e "TRAE_VENV=${ROOT}/.venv" \
    -e "PYTHONPATH=${PYTHONPATH}" \
    -e "ACCESS_TOKEN=${ACCESS_TOKEN}" \
    -e "BusinessApiEndPoint=${BusinessApiEndPoint}" \
    -e "NO_PROXY=${NO_PROXY:-}" \
    -e "no_proxy=${no_proxy:-}" \
    "${_docker_extra_env[@]}" \
    -w "${ROOT}/onlineService" \
    "${IMAGE}" \
    "${ROOT}/.venv/bin/python" -m uvicorn app.main:app --host 0.0.0.0 --port "${PORT}" "${_GRACEFUL_ARGS[@]}"
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
if [[ "${#_GRACEFUL_ARGS[@]}" -gt 0 ]]; then
  CMD+=("${_GRACEFUL_ARGS[@]}")
fi
if [[ "${#RELOAD_ARGS[@]}" -gt 0 ]]; then
  CMD+=("${RELOAD_ARGS[@]}")
fi

# 前台 exec 在 uvicorn --reload / 长连接场景下易被“粘住”；改为子进程后台跑，本 shell 专职收信号并整树清理。
_run_local_kill_tree() {
  local pid="$1"
  local sig="${2:-TERM}"
  local c
  for c in $(pgrep -P "${pid}" 2>/dev/null || true); do
    _run_local_kill_tree "${c}" "${sig}"
  done
  if kill -0 "${pid}" 2>/dev/null; then
    case "${sig}" in
      KILL) kill -KILL "${pid}" 2>/dev/null || true ;;
      TERM) kill -TERM "${pid}" 2>/dev/null || true ;;
      *) kill -s "${sig}" "${pid}" 2>/dev/null || true ;;
    esac
  fi
}

_run_local_on_signal() {
  local _exit="${1:-130}"
  trap - INT TERM HUP
  if [[ -n "${_RUN_LOCAL_BG_PID:-}" ]] && kill -0 "${_RUN_LOCAL_BG_PID}" 2>/dev/null; then
    _run_local_kill_tree "${_RUN_LOCAL_BG_PID}" TERM
    local _i=0
    while kill -0 "${_RUN_LOCAL_BG_PID}" 2>/dev/null && [[ "${_i}" -lt 50 ]]; do
      sleep 0.1
      _i=$((_i + 1))
    done
    if kill -0 "${_RUN_LOCAL_BG_PID}" 2>/dev/null; then
      _run_local_kill_tree "${_RUN_LOCAL_BG_PID}" KILL
    fi
  fi
  wait "${_RUN_LOCAL_BG_PID}" 2>/dev/null || true
  exit "${_exit}"
}

trap '_run_local_on_signal 130' INT
trap '_run_local_on_signal 143' TERM
trap '_run_local_on_signal 129' HUP

"${CMD[@]}" &
_RUN_LOCAL_BG_PID=$!

set +e
wait "${_RUN_LOCAL_BG_PID}"
_ec=$?
set -e
trap - INT TERM HUP
exit "${_ec}"
