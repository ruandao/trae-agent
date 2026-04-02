#!/usr/bin/env bash
# 在仓库根目录构建 onlineService 镜像。支持 linux/amd64 与 linux/arm64。
# 推送时会额外生成按 `${架构}-${YYYYMMDDHHMMSS}` 规则的单架构 tag，并合并出多架构 manifest（保留用户传入的 IMAGE）。
# 用法：
#   ./onlineService/docker-build.sh
#   PUSH_IMAGE=1 IMAGE=registry.example.com/trae-online:latest ./onlineService/docker-build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE="${IMAGE:-trae-online-service:latest}"
DOCKERFILE="${DOCKERFILE:-onlineService/Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

DATETIME="$(date '+%Y%m%d%H%M%S')"

# 将镜像引用拆分为仓库部分（不含 :tag / @digest）
# 规则：只有当最后一个 '/' 之后存在 ':' 时，才认为这是 :tag 分隔符。
IMAGE_NO_DIGEST="$IMAGE"
if [[ "$IMAGE_NO_DIGEST" == *@* ]]; then
  IMAGE_NO_DIGEST="${IMAGE_NO_DIGEST%@*}"
fi
if [[ "${IMAGE_NO_DIGEST##*/}" == *":"* ]]; then
  IMAGE_REPO="${IMAGE_NO_DIGEST%:*}"
else
  IMAGE_REPO="$IMAGE_NO_DIGEST"
fi

platforms_array=()
IFS=',' read -r -a platforms_array <<< "$PLATFORMS"

platform_to_arch() {
  # buildx platform 常见格式：linux/amd64, linux/arm64
  # 取最后一段作为架构名；其中 amd64 会映射为 x86_64
  local platform="$1"
  local arch="${platform##*/}"
  # 统一使用 x86_64 命名
  if [[ "$arch" == "amd64" ]]; then
    echo "x86_64"
  else
    echo "$arch"
  fi
}

build_and_tag_one() {
  local platform="$1"
  local arch="$2"
  local arch_tag="${IMAGE_REPO}:${arch}-${DATETIME}"

  if [[ "${PUSH_IMAGE:-0}" == "1" ]]; then
    docker buildx build \
      --platform "$platform" \
      -f "$DOCKERFILE" \
      -t "$arch_tag" \
      . \
      --push
    echo "Pushed: $arch_tag ($platform)" >&2
  else
    # 本地 load 模式建议仅用于单平台；多平台时仍可依次构建并加载，但可能耗时更久。
    docker buildx build \
      --platform "$platform" \
      -f "$DOCKERFILE" \
      -t "$arch_tag" \
      . \
      --load
    echo "Loaded locally: $arch_tag ($platform)" >&2
  fi

  echo "$arch_tag"
}

per_arch_tags=()

if [[ "${PUSH_IMAGE:-0}" == "1" ]]; then
  # 逐平台构建并推送：按 `${架构}-${当前日期时间}` 生成 tag
  for p in "${platforms_array[@]}"; do
    p="${p//[[:space:]]/}"
    arch="$(platform_to_arch "$p")"
    per_arch_tag="$(build_and_tag_one "$p" "$arch")"
    per_arch_tags+=("$per_arch_tag")
  done

  # 再合并为多架构 manifest，保留用户传入的 IMAGE（例如 :latest）
  # 这样既满足新 tag 规范，也不破坏原先的统一拉取方式（IMAGE:tag）。
  docker buildx imagetools create --tag "$IMAGE" "${per_arch_tags[@]}"
  echo "Created multi-arch tag: $IMAGE (from: ${per_arch_tags[*]})"
else
  if [[ "${#platforms_array[@]}" -gt 1 ]]; then
    echo "NOTE: 本地仅能加载单架构镜像；将按平台逐个构建并加载（不会创建多架构 manifest）。"
    for p in "${platforms_array[@]}"; do
      p="${p//[[:space:]]/}"
      arch="$(platform_to_arch "$p")"
      build_and_tag_one "$p" "$arch" > /dev/null
    done
  else
    # 单平台：同时加载 arch-tag 与用户指定的 IMAGE tag（保持旧行为）
    p="${platforms_array[0]}"
    p="${p//[[:space:]]/}"
    arch="$(platform_to_arch "$p")"
    arch_tag="${IMAGE_REPO}:${arch}-${DATETIME}"

    docker buildx build \
      --platform "$p" \
      -f "$DOCKERFILE" \
      -t "$IMAGE" \
      -t "$arch_tag" \
      . \
      --load
    echo "Loaded locally: $IMAGE and $arch_tag ($p)"
  fi
fi
