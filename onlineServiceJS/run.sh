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
#   TRAE_GIT_CLONE_ALLOW_IPV6=1  关闭默认的「git clone -4」与 GIT_HTTP_IPV4（仅当你必须走纯 IPv6 时）
#   TRAE_GIT_PATH       可设为绝对路径（如 /usr/bin/git），当 PATH 上 git 为不兼容包装脚本时避免克隆失败
#   NODEJS_WATCH      默认开启：node --watch，改动 src/*.mjs 等依赖树会自动重启。
#                     设为 0 / false 则单次运行（与生产行为一致）。
#   若 PORT 已被监听，本脚本会先 kill -9 占用该端口的进程再启动（本地开发约定）。
#
# Docker（默认开启；构建上下文须为 trae-agent 根目录）：
#   ./run.sh
#   IMAGE=trae-online-js:local ./run.sh
#   TRAE_ONLINE_JS_DOCKER_REBUILD=1  强制重新 docker build（忽略指纹）
#   TRAE_ONLINE_JS_DOCKER_STAMP_FILE=路径  覆盖默认指纹文件（见 onlineServiceJS/.last-docker-image-context.sha256）
# 仅当 trae-agent 下除 onlineProject/、onlineProject_state/、.git/ 外文件有变更时才重新 build；否则复用已有镜像。
# 本地直接跑 Node（不经过 Docker）：
#   TRAE_ONLINE_JS_DOCKER=0 ./run.sh
#
# Docker 模式下：
#   - 挂载宿主 ${REPO_ROOT}/onlineProject_state → 容器内状态、日志、reqLogs、overlay 元数据（upper/work）。
#   - 挂载宿主 ${REPO_ROOT}/onlineProject → 容器内「项目运行根」；可写层目录默认为其下 layers/。
#   - 使用 --privileged，以便在 Linux 容器内对每条新任务叠层执行 overlay 挂载（TRAE_USE_OVERLAY_STACK=1），
#     删除该任务层或回滚串行尾层时可整体丢弃 upper，实现指令级回滚语义。
#   - macOS 上 Docker 的 bind 卷上 overlay(5) 常报 “wrong fs type … on overlay”，故默认传 TRAE_USE_OVERLAY_STACK=0；
#     需强制 overlay 时可显式加 TRAE_USE_OVERLAY_STACK=1；即使为 1，代码层也会在挂载失败时回退为目录拷贝，避免留空层目录。
#   - 可选 ONLINE_PROJECT_HOST：宿主项目根目录，默认 ${REPO_ROOT}/onlineProject；容器内固定为 /app/onlineProject。
#     Docker 下请勿把 ONLINE_PROJECT_LAYERS 设成宿主路径，应使用容器内路径或留空（默认 /app/onlineProject/layers）。
#   - 使用 --cidfile + 后台 docker run + wait + trap：父 shell 能收到 HUP/INT/TERM 并 stop/kill 容器，避免仅 docker 子进程收信号而本脚本不清理。
#   - 因后台 run 时无法为容器分配 pty，故不传 --tty（保留 -i）；需真正伪终端时可 TRAE_ONLINE_JS_DOCKER=0 在宿主跑 node。
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

# 根据 REPO_ROOT（trae-agent）内除 .git、onlineProject、onlineProject_state 外文件内容生成指纹，用于是否触发 docker build。
# 用 sort 后的文件清单 + tar -cf - -T 一次流式归档再 SHA256，比逐文件 shasum 在大型仓库上快一个数量级。
trae_agent_docker_context_hash() {
  local root="$1"
  local list
  list="$(mktemp "${TMPDIR:-/tmp}/trae-docker-ctx.XXXXXX")"
  (
    cd "$root" || exit 1
    find . \( -path './.git' -o -path './onlineProject' -o -path './onlineProject_state' \) -prune -o -type f -print
  ) | LC_ALL=C sort > "$list" || true
  if [[ ! -s "$list" ]]; then
    rm -f "$list"
    printf '%s' 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
    return 0
  fi
  (
    cd "$root" || exit 1
    COPYFILE_DISABLE=1
    export COPYFILE_DISABLE
    tar -cf - -T "$list" 2>/dev/null
  ) | {
    if command -v sha256sum >/dev/null 2>&1; then
      sha256sum
    else
      shasum -a 256
    fi
  } | awk '{print $1}'
  rm -f "$list"
}

if [[ "${TRAE_ONLINE_JS_DOCKER:-1}" != "0" && "${TRAE_ONLINE_JS_DOCKER:-1}" != "false" ]]; then
  IMAGE="${TRAE_ONLINE_JS_IMAGE:-trae-online-js:local}"
  STAMP_FILE="${TRAE_ONLINE_JS_DOCKER_STAMP_FILE:-$SCRIPT_DIR/.last-docker-image-context.sha256}"
  _new_h=''
  _new_h=$(trae_agent_docker_context_hash "$REPO_ROOT") || _new_h=''
  _old_h=''
  [[ -f "$STAMP_FILE" ]] && _old_h="$(tr -d '\n\r' < "$STAMP_FILE" 2>/dev/null || true)"
  _force_rebuild=0
  if [[ "${TRAE_ONLINE_JS_DOCKER_REBUILD:-0}" == "1" || "${TRAE_ONLINE_JS_DOCKER_REBUILD:-false}" == "true" ]]; then
    _force_rebuild=1
  fi
  _need_docker_build=1
  if [[ "$_force_rebuild" -eq 0 ]] && docker image inspect "$IMAGE" >/dev/null 2>&1; then
    if [[ -n "$_new_h" && -n "$_old_h" && "$_old_h" == "$_new_h" ]]; then
      _need_docker_build=0
    fi
  fi
  if [[ "$_force_rebuild" -eq 1 ]]; then
    _need_docker_build=1
  fi
  if [[ "$_need_docker_build" -eq 1 ]]; then
    if [[ -z "$_new_h" ]]; then
      echo "[run.sh] 警告: 未得到仓库源指纹，仍将执行 docker build" >&2
    fi
    if [[ "$_force_rebuild" -eq 1 ]]; then
      echo "[run.sh] TRAE_ONLINE_JS_DOCKER_REBUILD=1：强制从仓库根构建: $IMAGE" >&2
    else
      echo "[run.sh] 镜像需更新或不存在，正在从仓库根构建: $IMAGE" >&2
    fi
    docker build -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" "$REPO_ROOT"
    if [[ -n "$_new_h" ]]; then
      printf '%s' "$_new_h" > "$STAMP_FILE"
    fi
  else
    echo "[run.sh] 源码未变（已排除 onlineProject/、onlineProject_state/、.git/），跳过 docker build" >&2
  fi
  ONLINE_PROJ_HOST="${ONLINE_PROJECT_HOST:-${REPO_ROOT}/onlineProject}"
  mkdir -p "${ONLINE_PROJ_HOST}/layers" "$STATE_ROOT/runtime" "$STATE_ROOT/logs" "$STATE_ROOT/reqLogs" "$STATE_ROOT/layers"
  _layers_in_container="${ONLINE_PROJECT_LAYERS:-/app/onlineProject/layers}"

  # 宿主为 macOS 时，Docker Desktop 的卷与 overlay(5) 常不兼容；未显式设置时默认关闭 overlay 叠层。
  _docker_overlay_default=1
  if [[ "$(uname -s)" == Darwin ]]; then
    _docker_overlay_default=0
  fi

  _docker_cid_file="${TMPDIR:-/tmp}/onlineServiceJS.docker.$$.$RANDOM.cid"
  rm -f "$_docker_cid_file"
  _docker_cleanup_done=0
  _docker_run_pid=''
  _docker_cleanup() {
    [[ "$_docker_cleanup_done" == 1 ]] && return
    _docker_cleanup_done=1
    local cid=""
    if [[ -f "$_docker_cid_file" ]]; then
      cid="$(tr -d '\n\r ' < "$_docker_cid_file" 2>/dev/null || true)"
    fi
    if [[ -n "$cid" ]]; then
      docker stop -t 10 "$cid" >/dev/null 2>&1 || docker kill "$cid" >/dev/null 2>&1 || true
    fi
    # 主进程是 docker 客户端时，stop 后通常已退出；若仍存活则再发信号，避免挂住占用端口/任务。
    if [[ -n "${_docker_run_pid}" ]] && kill -0 "$_docker_run_pid" 2>/dev/null; then
      kill -TERM "$_docker_run_pid" 2>/dev/null || true
    fi
    rm -f "$_docker_cid_file"
  }
  trap '_docker_cleanup' EXIT INT TERM HUP

  # 勿使用 exec。前台 `docker run` 时 SIGINT 常只交给 docker 子进程，bash 的 INT trap 不执行、EXIT 在 hung 时也不触发；
  # 子进程在后台、父 shell 前台 wait 时，同一会话上 Ctrl+C 会先到本 shell，trap 可可靠 stop 容器。后台 run 无法分配 pty，故不传 --tty。
  docker run --rm -i --privileged \
    --cidfile "$_docker_cid_file" \
    -p "${PORT}:${PORT}" \
    -v "${STATE_ROOT}:/app/onlineProject_state" \
    -v "${ONLINE_PROJ_HOST}:/app/onlineProject" \
    -e "ACCESS_TOKEN=${ACCESS_TOKEN}" \
    -e "PORT=${PORT}" \
    -e "REPO_ROOT=/app" \
    -e "ONLINE_PROJECT_STATE_ROOT=/app/onlineProject_state" \
    -e "ONLINE_PROJECT_LAYERS=${_layers_in_container}" \
    -e "TRAE_USE_OVERLAY_STACK=${TRAE_USE_OVERLAY_STACK:-$_docker_overlay_default}" \
    -e "BusinessApiEndPoint=${BusinessApiEndPoint}" \
    -e "GIT_HTTP_IPV4=${GIT_HTTP_IPV4:-1}" \
    "${IMAGE}" &
  _docker_run_pid=$!
  set +e
  wait "$_docker_run_pid"
  _docker_ex=$?
  set -e
  _docker_cleanup
  exit "$_docker_ex"
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

# 勿使用 exec：保留 shell 以注册 trap，在收到 INT/TERM/HUP 时对 node 发 SIGTERM，确保 Ctrl+C、kill 与 IDE「停止」能结束服务。
# 仅对 node 的 PID 发信号，避免用负号 PGID 误伤与 shell 同组的前台/后台布局。
_node_pid=''
__cleanup_node() {
  if [[ -n "${_node_pid}" ]] && kill -0 "$_node_pid" 2>/dev/null; then
    kill -TERM "$_node_pid" 2>/dev/null || true
  fi
  return 0
}
set +e
node "${_watch_flag[@]}" src/server.mjs &
_node_pid=$!
set -e
trap '__cleanup_node' INT TERM HUP
set +e
wait "$_node_pid"
_node_rc=$?
set -e
trap - INT TERM HUP
exit "$_node_rc"
