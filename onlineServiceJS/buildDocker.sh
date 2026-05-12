#!/usr/bin/env bash
# 从 trae-agent 仓库根目录构建 onlineServiceJS 镜像（与 Dockerfile 的 COPY 路径一致），并可推送至镜像仓库。
# 默认使用 docker buildx；arch_timestamp 下每架构推送两个标签：<git_tag> 与 <cpu>-latest
# （git_tag 即当前 commit 已有的首个 git tag，否则按 "${Arch}_%Y-%m-%d_%H-%M" 在 HEAD 上新建；
#  显式设置 DOCKER_IMAGE_TAG_VERSION 时回退到旧命名 <cpu>-<VER>）。
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
#                            arch_timestamp：每架构两条标签 <cpu>-<VER> 与 <cpu>-latest（VER 见下）。
#                            literal：单标签 DOCKER_IMAGE_TAG（默认 latest），可单次推送多架构清单。
#   DOCKER_IMAGE_TAG_VERSION    覆盖版本片段；未设置时启用"git tag → docker tag"流程：
#                                  1) 若当前 HEAD 已有任意 git tag，复用首个；
#                                  2) 否则按 "${Arch}_%Y-%m-%d_%H-%M" 在 HEAD 上新建 git tag
#                                     （%Y-%m-%d %H:%M 中的空格、冒号在 git/docker tag 中均非法，
#                                      已分别替换为 "_" 与 "-"）；
#                                  3) 该 git tag 经 docker 字符集净化后直接作为 docker tag（${base}:<git_tag>）。
#                                显式设置时则按旧行为：作为 ${slug}-${VER} 中的版本片段，不打 git tag。
#                                未在 git 仓库中时，回退到 ${Arch}_$(date +%Y-%m-%d_%H-%M)（仅生成名字、不打 tag）。
#   DOCKER_IMAGE_TAG_TIMESTAMP  兼容旧名，等价于 DOCKER_IMAGE_TAG_VERSION（前者优先级更高）。
#   DOCKER_IMAGE_TAG    仅在 literal 下作为仓库 tag（默认 latest）。
#   DOCKER_PUSH_IMAGE   若设置：整条镜像引用（含 tag），单次推送，忽略 arch_timestamp 命名。
#   推送说明：literal 且 PUSH_REF 与 DOCKER_IMAGE 为短名时仍仅推 PUSH_REF；见前文。
#   DOCKER_PLATFORMS    逗号分隔，默认 linux/amd64,linux/arm64（arch_timestamp 下逐项构建推送）。
#   DOCKER_BUILDX_BUILDER  可选，传给 docker buildx build --builder。
#   DOCKER_PUSH         设为 1 / true 时推送（可与 --push 二选一）。
#   ENABLE_CODE_SERVER / NODE_VERSION / CODE_SERVER_VERSION  传给 Dockerfile。
#   NPM_REGISTRY  可选，传给 Dockerfile（例：https://registry.npmmirror.com），减轻 npm ci 时 ECONNRESET。
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
      sed -n '2,35p' "$0" | sed 's/^# \{0,1\}//'
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
DOCKER_REGISTRY_REPOSITORY="${DOCKER_REGISTRY_REPOSITORY:-registry.cn-qingdao.aliyuncs.com/ruandao/task2app-trae}"
# 推送时默认强制双架构齐全，避免只推单架构导致线上拉取失败。
REQUIRED_PUSH_PLATFORMS="${REQUIRED_PUSH_PLATFORMS:-linux/amd64,linux/arm64}"

# Docker tag 允许的字符集为 [A-Za-z0-9_.-]，首字符须为 [A-Za-z0-9_]，长度 <=128。
sanitize_docker_tag() {
  local s="$1"
  s="$(printf '%s' "$s" | LC_ALL=C tr -c 'A-Za-z0-9_.-' '-')"
  case "$s" in
    [A-Za-z0-9_]*) ;;
    *) s="_${s}" ;;
  esac
  printf '%s' "${s:0:128}"
}

# 优先用 git describe（含 tag/commit/dirty 信息），不在 git 仓库时落回时间戳。
compute_image_version() {
  local desc=""
  if command -v git >/dev/null 2>&1 \
     && git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    desc="$(git -C "$REPO_ROOT" describe --tags --always --dirty 2>/dev/null || true)"
  fi
  if [[ -z "$desc" ]]; then
    desc="$(date +%Y%m%d%H%M%S)"
  fi
  sanitize_docker_tag "$desc"
}

# 查询或创建当前 commit 的「按架构」git tag。
# - 当前 HEAD 已有匹配该架构前缀（${arch}_）的 tag：复用首个匹配项。
# - 否则按 "${arch}_%Y-%m-%d_%H-%M" 在 HEAD 上新建 git tag 并返回。
# - 不在 git 仓库 / git 不可用：仅生成同形名字（不打 tag）。
# 注：用户原指定格式为 "${Arch}_%Y-%m-%d %H:%M"，但空格与冒号在 git/docker tag 中均非法，
# 这里将空格替换为 "_"、冒号替换为 "-"，等价为 "${arch}_%Y-%m-%d_%H-%M"。
ensure_commit_git_tag() {
  local arch="$1"
  local existing="" candidate=""
  candidate="${arch}_$(date '+%Y-%m-%d_%H-%M')"
  if ! command -v git >/dev/null 2>&1 \
     || ! git -C "$REPO_ROOT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    printf '%s' "$candidate"
    return 0
  fi
  existing="$(git -C "$REPO_ROOT" tag --points-at HEAD 2>/dev/null | awk -v pfx="^${arch}_" '$0 ~ pfx { print; exit }' || true)"
  if [[ -n "$existing" ]]; then
    printf '%s' "$existing"
    return 0
  fi
  if git -C "$REPO_ROOT" rev-parse --verify "refs/tags/${candidate}" >/dev/null 2>&1; then
    echo "[buildDocker.sh] 复用同名 git tag（未指向当前 HEAD，按 docker tag 用途使用）: $candidate" >&2
    printf '%s' "$candidate"
    return 0
  fi
  if git -C "$REPO_ROOT" tag "$candidate" >/dev/null 2>&1; then
    echo "[buildDocker.sh] 当前 commit 无 git tag，已新建: $candidate" >&2
  else
    echo "[buildDocker.sh] 警告: 创建 git tag 失败: $candidate（仅作为 docker tag 使用）" >&2
  fi
  printf '%s' "$candidate"
}

# DOCKER_IMAGE_TAG_VERSION 为新名，DOCKER_IMAGE_TAG_TIMESTAMP 为兼容旧名。
TAG_TS="${DOCKER_IMAGE_TAG_VERSION:-${DOCKER_IMAGE_TAG_TIMESTAMP:-$(compute_image_version)}}"

# 未显式指定版本片段时，启用"git tag → docker tag"流程（按架构生成/复用 git tag 并直接作为 docker tag）。
USE_GIT_TAG_AS_DOCKER_TAG=0
if [[ -z "${DOCKER_IMAGE_TAG_VERSION:-}" && -z "${DOCKER_IMAGE_TAG_TIMESTAMP:-}" ]]; then
  USE_GIT_TAG_AS_DOCKER_TAG=1
fi

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

csv_contains_platform() {
  local csv="$1"
  local target="$2"
  local _oifs="$IFS"
  IFS=','
  for _entry in $csv; do
    IFS="$_oifs"
    if [[ "$(trim_spaces "$_entry")" == "$target" ]]; then
      return 0
    fi
    IFS=','
  done
  IFS="$_oifs"
  return 1
}

assert_required_push_platforms() {
  local actual="$1"
  local required_csv="$2"
  local missing=()
  local _oifs="$IFS"
  IFS=','
  for _entry in $required_csv; do
    IFS="$_oifs"
    req="$(trim_spaces "$_entry")"
    [[ -z "$req" ]] && { IFS=','; continue; }
    if ! csv_contains_platform "$actual" "$req"; then
      missing+=("$req")
    fi
    IFS=','
  done
  IFS="$_oifs"
  if [[ "${#missing[@]}" -gt 0 ]]; then
    echo "[buildDocker.sh] 错误: 推送模式要求同时构建并推送以下架构: $required_csv" >&2
    echo "[buildDocker.sh] 当前 DOCKER_PLATFORMS=$actual，缺失: ${missing[*]}" >&2
    echo "[buildDocker.sh] 如需覆盖默认要求，请显式设置 REQUIRED_PUSH_PLATFORMS" >&2
    exit 1
  fi
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
if [[ -n "${NPM_REGISTRY:-}" ]]; then
  BUILD_ARGS+=( --build-arg "NPM_REGISTRY=${NPM_REGISTRY}" )
fi

BX=( docker buildx build )
if [[ -n "${DOCKER_BUILDX_BUILDER:-}" ]]; then
  BX+=( --builder "${DOCKER_BUILDX_BUILDER}" )
fi

if [[ "$DO_PUSH" -eq 1 ]]; then
  assert_required_push_platforms "$PLATFORMS" "$REQUIRED_PUSH_PLATFORMS"

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
    if [[ "$USE_GIT_TAG_AS_DOCKER_TAG" == "1" ]]; then
      echo "[buildDocker.sh] 标签方案: arch_timestamp（按架构使用 git tag 作为 docker tag）" >&2
    else
      printf '[buildDocker.sh] 标签方案: arch_timestamp，版本: %s\n' "${TAG_TS}" >&2
    fi
    echo "[buildDocker.sh] 仓库路径（不含 tag）: $base" >&2
    # 快照版本：避免 Bash 3.2 在含全角括号的 echo 等处误解析 $TAG_TS；循环内仅用快照。
    _tag_ts="${TAG_TS}"
    # macOS /bin/bash 为 3.2，无 read -a；用 IFS 拆分 PLATFORMS。
    _oifs=$IFS
    IFS=','
    for _plat_entry in $PLATFORMS; do
      IFS=$_oifs
      plat="$(trim_spaces "$_plat_entry")"
      [[ -z "${plat}" ]] && continue
      slug="$(platform_to_arch_slug "${plat}")" || exit 1
      if [[ "$USE_GIT_TAG_AS_DOCKER_TAG" == "1" ]]; then
        git_tag="$(ensure_commit_git_tag "${slug}")"
        ref_ts="${base}:$(sanitize_docker_tag "${git_tag}")"
      else
        ref_ts="${base}:${slug}-${_tag_ts}"
      fi
      ref_latest="${base}:${slug}-latest"
      printf '[buildDocker.sh] 构建并推送: %s 与 %s （平台 %s）\n' "${ref_ts}" "${ref_latest}" "${plat}" >&2
      "${BX[@]}" -t "$ref_ts" -t "$ref_latest" "${BUILD_ARGS[@]}" --platform "${plat}" --push "$REPO_ROOT"
    done
    IFS=$_oifs
    if [[ "$USE_GIT_TAG_AS_DOCKER_TAG" == "1" ]]; then
      echo "[buildDocker.sh] 已完成按架构推送（docker tag = 各架构 git tag）。" >&2
    else
      printf '[buildDocker.sh] 已完成按架构推送（版本 %s）。\n' "${_tag_ts}" >&2
    fi
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
  if [[ "$USE_GIT_TAG_AS_DOCKER_TAG" == "1" ]]; then
    git_tag="$(ensure_commit_git_tag "${slug}")"
    LOAD_REF_TS="${img_base}:$(sanitize_docker_tag "${git_tag}")"
  else
    LOAD_REF_TS="${img_base}:${slug}-${TAG_TS}"
  fi
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
