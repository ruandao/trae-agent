#!/usr/bin/env bash
# 在仓库根目录构建 onlineService 镜像。支持 linux/amd64 与 linux/arm64。
# 用法：
#   ./onlineService/docker-build.sh
#   PUSH_IMAGE=1 IMAGE=registry.example.com/trae-online:latest ./onlineService/docker-build.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

IMAGE="${IMAGE:-trae-online-service:latest}"
DOCKERFILE="${DOCKERFILE:-onlineService/Dockerfile}"
PLATFORMS="${PLATFORMS:-linux/amd64,linux/arm64}"

if [[ "${PUSH_IMAGE:-0}" == "1" ]]; then
  docker buildx build \
    --platform "$PLATFORMS" \
    -f "$DOCKERFILE" \
    -t "$IMAGE" \
    . \
    --push
  echo "Pushed: $IMAGE ($PLATFORMS)"
else
  docker buildx build \
    --platform "$PLATFORMS" \
    -f "$DOCKERFILE" \
    -t "$IMAGE" \
    . \
    --load
  echo "Loaded locally: $IMAGE (当前 Docker 仅支持加载单一平台时，请改用 PUSH_IMAGE=1 推送到仓库)"
fi
