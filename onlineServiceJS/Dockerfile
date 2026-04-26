# 单容器：Node onlineServiceJS（8765）+ 可选 code-server（8888）。
# 容器内 apt 使用 Ubuntu 官方源（避免部分网络下清华 apt 证书/镜像异常导致构建失败）。
# 含 git + openssh-client：onlineServiceJS bootstrap 会 clone 任务关联仓库（常见 git@github.com SSH）。
# PyPI 仍走清华；Node  tarball 清华优先、失败回退 nodejs.org；code-server 同 skill 说明。
# npm 包无清华官方 Registry，构建阶段使用默认 registry.npmjs.org。
# 多阶段：编译型 Python 依赖仅在 pydeps 阶段安装，最终镜像不含 build-essential / python3.12-dev。
# ENABLE_CODE_SERVER=1 安装 code-server；运行时 CODE_SERVER_ENABLED=1 启动。

FROM ubuntu:24.04 AS pydeps

ARG DEBIAN_FRONTEND=noninteractive

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl \
        python3.12 python3.12-venv python3.12-dev \
        build-essential pkg-config \
    ; \
    apt-get clean; rm -rf /var/lib/apt/lists/*

ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent
COPY trae_agent_online /app/trae_agent_online

RUN python3.12 -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir . \
    && /app/.venv/bin/python -c "import sys; assert sys.version_info[:2] >= (3, 12), sys.version"


FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ENABLE_CODE_SERVER=1
# 与 code-server  tarball 架构一致；未传时以镜像内 dpkg 为准（避免 arm64 构建误用默认 amd64 的 Node 二进制）
ARG TARGETARCH
ARG CODE_SERVER_VERSION=4.96.4
ARG NODE_VERSION=22.14.0

ENV CODE_SERVER_ENABLED=1 \
    CODE_SERVER_WORKDIR=/app

RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates curl xz-utils \
        python3.12 python3.12-venv \
        libatomic1 \
        git openssh-client \
    ; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) NODE_ARCH=x64 ;; \
        arm64) NODE_ARCH=arm64 ;; \
        *) echo "unsupported arch $arch"; exit 1 ;; \
    esac; \
    rm -f /tmp/node.txz; \
    tuna_node="https://mirrors.tuna.tsinghua.edu.cn/nodejs-release/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    official_node="https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${NODE_ARCH}.tar.xz"; \
    if ! curl -fsSL "$tuna_node" -o /tmp/node.txz; then curl -fsSL "$official_node" -o /tmp/node.txz; fi; \
    test -s /tmp/node.txz; \
    tar -xJf /tmp/node.txz -C /usr/local --strip-components=1; \
    rm -f /tmp/node.txz; \
    apt-get purge -y xz-utils; \
    apt-get autoremove -y; \
    apt-get clean; rm -rf /var/lib/apt/lists/*; \
    node -v

# ---- code-server：优先清华 github-release，失败则回退 GitHub ----
RUN set -eux; \
    if [ "${ENABLE_CODE_SERVER}" != "1" ]; then echo "skip code-server"; exit 0; fi; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) cs_arch=amd64 ;; \
        arm64) cs_arch=arm64 ;; \
        *) echo "unsupported arch $arch"; exit 1 ;; \
    esac; \
    tuna="https://mirrors.tuna.tsinghua.edu.cn/github-release/coder/code-server/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-${cs_arch}.tar.gz"; \
    gh="https://github.com/coder/code-server/releases/download/v${CODE_SERVER_VERSION}/code-server-${CODE_SERVER_VERSION}-linux-${cs_arch}.tar.gz"; \
    rm -f /tmp/cs.tgz; \
    if ! curl -fsSL "$tuna" -o /tmp/cs.tgz; then curl -fsSL "$gh" -o /tmp/cs.tgz; fi; \
    mkdir -p /opt/code-server; \
    tar xzf /tmp/cs.tgz -C /opt/code-server --strip-components=1; \
    ln -sf /opt/code-server/bin/code-server /usr/local/bin/code-server; \
    rm -f /tmp/cs.tgz; \
    code-server --version

COPY --chmod=0755 onlineServiceJS/docker/onlineService-entrypoint.sh /usr/local/bin/onlineService-entrypoint.sh

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent
COPY trae_agent_online /app/trae_agent_online
COPY --from=pydeps /app/.venv /app/.venv

COPY onlineServiceJS/package.json onlineServiceJS/package-lock.json /app/onlineServiceJS/
WORKDIR /app/onlineServiceJS
RUN npm ci --omit=dev
COPY onlineServiceJS /app/onlineServiceJS/

WORKDIR /app/onlineServiceJS
EXPOSE 8765 8888
ENTRYPOINT ["/usr/local/bin/onlineService-entrypoint.sh"]
