#!/usr/bin/env bash
# 将 code-server 官方 tarball 下载到 docker/code-server/，供 Dockerfile 多阶段 COPY 使用。
# 由 buildDocker.sh 自动调用；也可单独执行以预热缓存。
#
# 用法：
#   ./docker/fetch-code-server-bundles.sh                    # 默认 linux/amd64 + linux/arm64
#   ./docker/fetch-code-server-bundles.sh linux/arm64       # 仅指定平台（可多个）
# 环境变量：
#   CODE_SERVER_VERSION  默认 4.96.4（须与 Dockerfile 中 ARG 一致）
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUTDIR="${SCRIPT_DIR}/code-server"
VER="${CODE_SERVER_VERSION:-4.96.4}"
MIN_BYTES="${CODE_SERVER_MIN_BYTES:-10485760}"

mkdir -p "$OUTDIR"

file_size() {
  local f="$1"
  if stat -f%z "$f" >/dev/null 2>&1; then
    stat -f%z "$f"
  else
    stat -c%s "$f"
  fi
}

platform_to_cs_arch() {
  case "$(printf '%s' "$1" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')" in
    linux/amd64) printf '%s' amd64 ;;
    linux/arm64) printf '%s' arm64 ;;
    *)
      echo "[fetch-code-server-bundles] 错误: 不支持的平台: $1（仅 linux/amd64、linux/arm64）" >&2
      return 1
      ;;
  esac
}

fetch_one() {
  local cs_arch="$1"
  local out="${OUTDIR}/code-server-${VER}-linux-${cs_arch}.tar.gz"
  if [[ -f "$out" ]]; then
    local sz
    sz="$(file_size "$out")"
    if [[ "$sz" -ge "$MIN_BYTES" ]]; then
      echo "[fetch-code-server-bundles] 已存在且大小合格: $out ($sz)" >&2
      return 0
    fi
    echo "[fetch-code-server-bundles] 删除过小或损坏文件: $out ($sz)" >&2
    rm -f "$out"
  fi
  local tuna gh
  tuna="https://mirrors.tuna.tsinghua.edu.cn/github-release/coder/code-server/v${VER}/code-server-${VER}-linux-${cs_arch}.tar.gz"
  gh="https://github.com/coder/code-server/releases/download/v${VER}/code-server-${VER}-linux-${cs_arch}.tar.gz"
  echo "[fetch-code-server-bundles] 下载 code-server ${VER} (${cs_arch}) …" >&2
  if curl -fsSL --connect-timeout 30 "$tuna" -o "$out" 2>/dev/null; then
    sz="$(file_size "$out")"
    if [[ "$sz" -ge "$MIN_BYTES" ]]; then
      echo "[fetch-code-server-bundles] 完成（清华）: $out" >&2
      return 0
    fi
  fi
  rm -f "$out"
  curl -fsSL --connect-timeout 30 \
    --retry 12 --retry-delay 10 --retry-all-errors \
    "$gh" -o "$out"
  sz="$(file_size "$out")"
  if [[ "$sz" -lt "$MIN_BYTES" ]]; then
    echo "[fetch-code-server-bundles] 错误: 下载结果过小 ($sz)，可能损坏: $out" >&2
    rm -f "$out"
    return 1
  fi
  echo "[fetch-code-server-bundles] 完成（GitHub）: $out" >&2
}

need_platforms=()
if [[ "$#" -gt 0 ]]; then
  for _p in "$@"; do
    [[ -z "${_p// }" ]] && continue
    need_platforms+=("$_p")
  done
fi
if [[ "${#need_platforms[@]}" -eq 0 ]]; then
  need_platforms=(linux/amd64 linux/arm64)
fi

# Bash 3.2 + set -u：空数组 "${arr[@]}" 会报 unbound，这里用字符串去重代替二次数组遍历。
seen=""
for p in "${need_platforms[@]}"; do
  a="$(platform_to_cs_arch "$p")" || exit 1
  case " ${seen} " in *" ${a} "*) continue ;; esac
  seen="${seen}${a} "
  fetch_one "$a"
done
