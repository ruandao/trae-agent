#!/usr/bin/env bash
# 从 trae-agent 仓库根目录构建 onlineServiceJS 镜像（与 Dockerfile 的 COPY 路径一致），并可推送至镜像仓库。
# 默认使用 docker buildx；arch_timestamp 下每架构推送两个标签：arm64-<时间戳>、arm64-latest（x86_64 同理）。
#
# 用法：
#   ./buildDocker.sh              # 本地 load：默认同名镜像 arm64-… / x86_64-…（仅本机架构）
#   DOCKER_REGISTRY_REPOSITORY=registry.example.com/ns/trae-online-js DOCKER_PUSH=1 ./buildDocker.sh
#
# 环境变量：
#   DOCKER_IMAGE        本地构建镜像名（不含 tag 或含 tag；arch_timestamp 下本地打 <cpu>-<TS> 与 <cpu>-latest）。默认 trae-online-js:local。
#   DOCKER_REGISTRY_REPOSITORY  镜像仓库地址 + 仓库名（同一变量），不含 tag。
#                               默认 registry.cn-qingdao.aliyuncs.com/ruandao/task2app-trae（可被环境变量覆盖）。
#   DOCKER_IMAGE_TAG_SCHEME  arch_timestamp（默认）| literal。
#                            arch_timestamp：每架构两条标签 <cpu>-<TS> 与 <cpu>-latest（TS 见下）。
#                            literal：单标签 DOCKER_IMAGE_TAG（默认 latest），可单次推送多架构清单。
#   DOCKER_IMAGE_TAG_TIMESTAMP  覆盖时间戳；未设置时用当前时间 $(date +%Y%m%d%H%M%S)，例如 20260405194908。
#   DOCKER_IMAGE_TAG    仅在 literal 下作为仓库 tag（默认 latest）。
#   DOCKER_PUSH_IMAGE   若设置：整条镜像引用（含 tag），单次推送，忽略 arch_timestamp 命名。
#   推送说明：literal 且 PUSH_REF 与 DOCKER_IMAGE 为短名时仍仅推 PUSH_REF；见前文。
#   DOCKER_PLATFORMS    逗号分隔，默认 linux/amd64,linux/arm64（arch_timestamp 下逐项构建推送）。
#   DOCKER_BUILDX_BUILDER  可选，传给 docker buildx build --builder。
#   DOCKER_PUSH         设为 1 / true 时推送（可与 --push 二选一）。
#   ENABLE_CODE_SERVER / NODE_VERSION / CODE_SERVER_VERSION  传给 Dockerfile。
#
# 推送前请在目标仓库执行 docker login（本脚本不代为交互登录）。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

DO_PUSH=0
for arg in "$@"; do
  case "$arg" in
    --push) DO_PUSH=1 ;;
    -h|--help)
      sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
  esac
done

if [[ "${DOCKER_PUSH:-0}" == "1" || "${DOCKER_PUSH:-false}" == "true" ]]; then
  DO_PUSH=1
fi

IMAGE="${DOCKER_IMAGE:-${TRAE_ONLINE_JS_IMAGE:-trae-online-js:local}}"
PLATFORMS="${DOCKER_PLATFORMS:-linux/amd64,linux/arm64}"
DOCKER_IMAGE_TAG_SCHEME="${DOCKER_IMAGE_TAG_SCHEME:-arch_timestamp}"
TAG_TS="${DOCKER_IMAGE_TAG_TIMESTAMP:-$(date +%Y%m%d%H%M%S)}"
DOCKER_REGISTRY_REPOSITORY="${DOCKER_REGISTRY_REPOSITORY:-registry.cn-qingdao.aliyuncs.com/ruandao/task2app-trae}"

native_platform() {
  case "$(uname -m)" in
    x86_64|amd64) printf '%s' linux/amd64 ;;
    aarch64|arm64) printf '%s' linux/arm64 ;;
    *) printf '%s' linux/amd64 ;;
  esac
}

trim_spaces() {
  local s="$1"
  s="${s#"${s%%[![:space:]]*}"}"
  s="${s%"${s##*[![:space:]]}"}"
  printf '%s' "$s"
}

platform_to_arch_slug() {
  case "$(trim_spaces "$1")" in
    linux/arm64) printf '%s' arm64 ;;
    linux/amd64) printf '%s' x86_64 ;;
    *)
      echo "[buildDocker.sh] 错误: 不支持的架构 \"$(trim_spaces "$1")\"（arch_timestamp 仅支持 linux/amd64、linux/arm64）" >&2
      return 1
      ;;
  esac
}

native_arch_slug() {
  platform_to_arch_slug "$(native_platform)"
}

resolve_push_ref() {
  if [[ -n "${DOCKER_PUSH_IMAGE:-}" ]]; then
    printf '%s' "$DOCKER_PUSH_IMAGE"
    return 0
  fi
  if [[ -n "${DOCKER_REGISTRY_REPOSITORY:-}" ]]; then
    local base="${DOCKER_REGISTRY_REPOSITORY%/}"
    local tag="${DOCKER_IMAGE_TAG:-latest}"
    printf '%s:%s' "$base" "$tag"
    return 0
  fi
  printf '%s' "$IMAGE"
}

if [[ ! -f "$REPO_ROOT/pyproject.toml" ]]; then
  echo "[buildDocker.sh] 错误: 未在预期仓库根找到 pyproject.toml: $REPO_ROOT" >&2
  exit 1
fi
if [[ ! -f "$SCRIPT_DIR/Dockerfile" ]]; then
  echo "[buildDocker.sh] 错误: 缺少 Dockerfile: $SCRIPT_DIR/Dockerfile" >&2
  exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
  echo "[buildDocker.sh] 错误: 未找到 docker 命令" >&2
  exit 1
fi

if ! docker buildx version >/dev/null 2>&1; then
  echo "[buildDocker.sh] 错误: 当前 Docker 不支持 buildx" >&2
  exit 1
fi

docker buildx inspect --bootstrap >/dev/null 2>&1 || true

BUILD_ARGS=( -f "$SCRIPT_DIR/Dockerfile" --build-arg "ENABLE_CODE_SERVER=${ENABLE_CODE_SERVER:-1}" )
if [[ -n "${NODE_VERSION:-}" ]]; then
  BUILD_ARGS+=( --build-arg "NODE_VERSION=${NODE_VERSION}" )
fi
if [[ -n "${CODE_SERVER_VERSION:-}" ]]; then
  BUILD_ARGS+=( --build-arg "CODE_SERVER_VERSION=${CODE_SERVER_VERSION}" )
fi

BX=( docker buildx build )
if [[ -n "${DOCKER_BUILDX_BUILDER:-}" ]]; then
  BX+=( --builder "${DOCKER_BUILDX_BUILDER}" )
fi

if [[ "$DO_PUSH" -eq 1 ]]; then
  if [[ -n "${DOCKER_PUSH_IMAGE:-}" ]]; then
    PUSH_REF="$(resolve_push_ref)"
    echo "[buildDocker.sh] 构建上下文: $REPO_ROOT" >&2
    echo "[buildDocker.sh] Dockerfile: $SCRIPT_DIR/Dockerfile" >&2
    echo "[buildDocker.sh] 推送引用（仅此标签）: $PUSH_REF" >&2
    echo "[buildDocker.sh] 平台（单条清单）: $PLATFORMS" >&2
    "${BX[@]}" -t "$PUSH_REF" "${BUILD_ARGS[@]}" --platform "$PLATFORMS" --push "$REPO_ROOT"
    echo "[buildDocker.sh] 已推送: $PUSH_REF" >&2
    exit 0
  fi

  if [[ "$DOCKER_IMAGE_TAG_SCHEME" == arch_timestamp ]]; then
    if [[ -z "${DOCKER_REGISTRY_REPOSITORY:-}" ]]; then
      echo "[buildDocker.sh] 错误: arch_timestamp 推送需要设置 DOCKER_REGISTRY_REPOSITORY（不含 tag），或改用 DOCKER_PUSH_IMAGE / DOCKER_IMAGE_TAG_SCHEME=literal" >&2
      exit 1
    fi
    base="${DOCKER_REGISTRY_REPOSITORY%/}"
    echo "[buildDocker.sh] 构建上下文: $REPO_ROOT" >&2
    echo "[buildDocker.sh] Dockerfile: $SCRIPT_DIR/Dockerfile" >&2
    printf '[buildDocker.sh] 标签方案: arch_timestamp，时间戳: %s\n' "${TAG_TS}" >&2
    echo "[buildDocker.sh] 仓库路径（不含 tag）: $base" >&2
    # 快照时间戳：避免 Bash 3.2 在含全角括号的 echo 等处误解析 $TAG_TS；循环内仅用快照。
    _tag_ts="${TAG_TS}"
    # macOS /bin/bash 为 3.2，无 read -a；用 IFS 拆分 PLATFORMS。
    _oifs=$IFS
    IFS=','
    for _plat_entry in $PLATFORMS; do
      IFS=$_oifs
      plat="$(trim_spaces "$_plat_entry")"
      [[ -z "${plat}" ]] && continue
      slug="$(platform_to_arch_slug "${plat}")" || exit 1
      ref_ts="${base}:${slug}-${_tag_ts}"
      ref_latest="${base}:${slug}-latest"
      printf '[buildDocker.sh] 构建并推送: %s 与 %s （平台 %s）\n' "${ref_ts}" "${ref_latest}" "${plat}" >&2
      "${BX[@]}" -t "$ref_ts" -t "$ref_latest" "${BUILD_ARGS[@]}" --platform "${plat}" --push "$REPO_ROOT"
    done
    IFS=$_oifs
    printf '[buildDocker.sh] 已完成按架构推送（时间戳 %s）。\n' "${_tag_ts}" >&2
    exit 0
  fi

  PUSH_REF="$(resolve_push_ref)"
  echo "[buildDocker.sh] 构建上下文: $REPO_ROOT" >&2
  echo "[buildDocker.sh] Dockerfile: $SCRIPT_DIR/Dockerfile" >&2
  echo "[buildDocker.sh] 推送引用（literal / 单清单）: $PUSH_REF" >&2
  [[ "$PUSH_REF" != "$IMAGE" ]] && echo "[buildDocker.sh] 说明: DOCKER_IMAGE=$IMAGE 未标记到本次构建，避免误推 docker.io/library/*" >&2
  echo "[buildDocker.sh] 平台: $PLATFORMS" >&2
  "${BX[@]}" -t "$PUSH_REF" "${BUILD_ARGS[@]}" --platform "$PLATFORMS" --push "$REPO_ROOT"
  echo "[buildDocker.sh] 已推送多架构清单: $PUSH_REF" >&2
  exit 0
fi

# ---- 本地 load ----
USE_PLATFORMS="$PLATFORMS"
OUTPUT=( --load )
if [[ "$DOCKER_IMAGE_TAG_SCHEME" == arch_timestamp ]]; then
  slug="$(native_arch_slug)"
  img_base="${IMAGE%:*}"
  [[ "$img_base" == "$IMAGE" ]] && img_base="$IMAGE"
  LOAD_REF_TS="${img_base}:${slug}-${TAG_TS}"
  LOAD_REF_LATEST="${img_base}:${slug}-latest"
  TAGS=( -t "$LOAD_REF_TS" -t "$LOAD_REF_LATEST" )
  if [[ "$PLATFORMS" == *","* ]]; then
    echo "[buildDocker.sh] 未使用 --push：无法载入多架构清单，改为仅构建本机架构 $(native_platform)" >&2
    USE_PLATFORMS="$(native_platform)"
  fi
else
  TAGS=( -t "$IMAGE" )
  if [[ "$PLATFORMS" == *","* ]]; then
    echo "[buildDocker.sh] 未使用 --push：无法将多架构清单载入本机 Docker，改为仅构建本机架构 $(native_platform)" >&2
    USE_PLATFORMS="$(native_platform)"
  fi
fi

echo "[buildDocker.sh] 构建上下文: $REPO_ROOT" >&2
echo "[buildDocker.sh] Dockerfile: $SCRIPT_DIR/Dockerfile" >&2
if [[ "$DOCKER_IMAGE_TAG_SCHEME" == arch_timestamp ]]; then
  printf '[buildDocker.sh] 标签: %s 与 %s\n' "${LOAD_REF_TS}" "${LOAD_REF_LATEST}" >&2
else
  echo "[buildDocker.sh] 标签: ${TAGS[1]}" >&2
fi
echo "[buildDocker.sh] 平台: $USE_PLATFORMS" >&2

"${BX[@]}" "${TAGS[@]}" "${BUILD_ARGS[@]}" --platform "$USE_PLATFORMS" "${OUTPUT[@]}" "$REPO_ROOT"

echo "[buildDocker.sh] 已完成本地构建。" >&2
