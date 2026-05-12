# 单容器：Node onlineServiceJS（8765）+ 可选 code-server（8888）。
# 容器内 apt：amd64 用 Ubuntu_24.04_Noble.list；arm64 用 Ubuntu_24.04_Noble-ports.list（内网 → 阿里云；官方兜底在 list 中默认注释，避免任一源失败导致 apt-get update 整失败）。
# 构建参数 SKIP_INTERNAL_APT_MIRROR：默认 1 去掉内网源（笔记本/CI）；内网构建传 0。
# 含 git + openssh-client：onlineServiceJS bootstrap 会 clone 任务关联仓库（常见 git@github.com SSH）。
# PyPI 仍走清华；Node  tarball 清华优先、失败回退 nodejs.org。
# code-server：构建前将 tarball 放入 onlineServiceJS/docker/code-server/（buildDocker.sh 会调用 docker/fetch-code-server-bundles.sh），镜像内仅保留解压结果。
# npm：默认 registry.npmjs.org；可通过构建参数 NPM_REGISTRY 指向镜像（如 npmmirror）缓解跨境 ECONNRESET。
# 构建阶段已调高 fetch-retries / 超时，降低 arm64 等环境下偶发断连失败率。
# 多阶段：编译型 Python 依赖仅在 pydeps 阶段安装，最终镜像不含 build-essential / python3.12-dev。
# ENABLE_CODE_SERVER=1 安装 code-server；运行时 CODE_SERVER_ENABLED=1 启动。

FROM ubuntu:24.04 AS pydeps

ARG DEBIAN_FRONTEND=noninteractive
ARG TARGETARCH
# 设为 1 时从 sources.list 去掉 192.168.3.25 内网源（笔记本/CI 上构建且不在该局域网时必须开启）
ARG SKIP_INTERNAL_APT_MIRROR=1

COPY onlineServiceJS/sourceList/Ubuntu_24.04_Noble.list /tmp/apt-sources-amd64.list
COPY onlineServiceJS/sourceList/Ubuntu_24.04_Noble-ports.list /tmp/apt-sources-arm64.list
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) cp /tmp/apt-sources-amd64.list /etc/apt/sources.list ;; \
        arm64) cp /tmp/apt-sources-arm64.list /etc/apt/sources.list ;; \
        *) echo "unsupported TARGETARCH=$arch for apt sources"; exit 1 ;; \
    esac; \
    rm -f /tmp/apt-sources-amd64.list /tmp/apt-sources-arm64.list; \
    if [ "${SKIP_INTERNAL_APT_MIRROR}" = "1" ]; then sed -i '/192\.168\.3\.25/d' /etc/apt/sources.list; fi

RUN set -eux; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\n' > /etc/apt/apt.conf.d/80-docker-retries.conf; \
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


# code-server：COPY 本地预下载 tarball 并在本阶段解压，最终镜像由后续阶段仅 COPY 目录，不包含 .tar.gz 层体积。
FROM ubuntu:24.04 AS code_server_local

ARG ENABLE_CODE_SERVER=1
ARG TARGETARCH
ARG CODE_SERVER_VERSION=4.96.4

COPY onlineServiceJS/docker/code-server/ /bundle/

RUN set -eux; \
    mkdir -p /opt/code-server; \
    if [ "${ENABLE_CODE_SERVER}" != "1" ]; then \
        touch /opt/code-server/.code_server_disabled; \
        exit 0; \
    fi; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) cs_arch=amd64 ;; \
        arm64) cs_arch=arm64 ;; \
        *) echo "unsupported arch $arch"; exit 1 ;; \
    esac; \
    bundle="/bundle/code-server-${CODE_SERVER_VERSION}-linux-${cs_arch}.tar.gz"; \
    test -f "$bundle"; \
    tar xzf "$bundle" -C /opt/code-server --strip-components=1; \
    test -x /opt/code-server/bin/code-server; \
    /opt/code-server/bin/code-server --version


FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG ENABLE_CODE_SERVER=1
# 与 code-server  tarball 架构一致；未传时以镜像内 dpkg 为准（避免 arm64 构建误用默认 amd64 的 Node 二进制）
ARG TARGETARCH
ARG SKIP_INTERNAL_APT_MIRROR=1
ARG CODE_SERVER_VERSION=4.96.4
ARG NODE_VERSION=22.14.0
# 留空则不改 registry；可传 https://registry.npmmirror.com 等
ARG NPM_REGISTRY=

ENV CODE_SERVER_ENABLED=1 \
    CODE_SERVER_WORKDIR=/app

COPY onlineServiceJS/sourceList/Ubuntu_24.04_Noble.list /tmp/apt-sources-amd64.list
COPY onlineServiceJS/sourceList/Ubuntu_24.04_Noble-ports.list /tmp/apt-sources-arm64.list
RUN rm -f /etc/apt/sources.list.d/ubuntu.sources; \
    arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
        amd64) cp /tmp/apt-sources-amd64.list /etc/apt/sources.list ;; \
        arm64) cp /tmp/apt-sources-arm64.list /etc/apt/sources.list ;; \
        *) echo "unsupported TARGETARCH=$arch for apt sources"; exit 1 ;; \
    esac; \
    rm -f /tmp/apt-sources-amd64.list /tmp/apt-sources-arm64.list; \
    if [ "${SKIP_INTERNAL_APT_MIRROR}" = "1" ]; then sed -i '/192\.168\.3\.25/d' /etc/apt/sources.list; fi

RUN set -eux; \
    printf 'Acquire::Retries "5";\nAcquire::http::Timeout "120";\nAcquire::https::Timeout "120";\n' > /etc/apt/apt.conf.d/80-docker-retries.conf; \
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

COPY --from=code_server_local /opt/code-server /opt/code-server
RUN set -eux; \
    if [ "${ENABLE_CODE_SERVER}" != "1" ] || [ -f /opt/code-server/.code_server_disabled ]; then \
        rm -rf /opt/code-server; \
        exit 0; \
    fi; \
    ln -sf /opt/code-server/bin/code-server /usr/local/bin/code-server; \
    code-server --version

COPY --chmod=0755 onlineServiceJS/docker/onlineService-entrypoint.sh /usr/local/bin/onlineService-entrypoint.sh

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent
COPY trae_agent_online /app/trae_agent_online
COPY --from=pydeps /app/.venv /app/.venv

COPY onlineServiceJS/package.json onlineServiceJS/package-lock.json /app/onlineServiceJS/
WORKDIR /app/onlineServiceJS
RUN set -eux; \
    npm config set fetch-retries 10; \
    npm config set fetch-retry-mintimeout 20000; \
    npm config set fetch-retry-maxtimeout 180000; \
    if [ -n "${NPM_REGISTRY}" ]; then npm config set registry "${NPM_REGISTRY}"; fi; \
    npm ci --omit=dev
COPY onlineServiceJS /app/onlineServiceJS/

WORKDIR /app/onlineServiceJS
EXPOSE 8765 8888
ENTRYPOINT ["/usr/local/bin/onlineService-entrypoint.sh"]
