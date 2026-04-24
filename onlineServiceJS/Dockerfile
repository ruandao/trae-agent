# 构建上下文须为 trae-agent 仓库根目录。
# 不声明 # syntax=docker/dockerfile:1，避免弱网拉取前端语法镜像失败。
#
# 镜像内需含 Python + trae-cli：否则 Node 服务对 command_kind=trae 会走占位 stub（立即退出）。
# Node 从官方 node 镜像复制，避免构建期依赖 deb.nodesource。
FROM node:22-bookworm-slim AS node_base

FROM python:3.12-slim-bookworm

# 仅装 git；trae-agent 依赖尽量走 wheel。若 pip 需编译本机扩展，可在此恢复 build-essential。
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

COPY --from=node_base /usr/local/bin/node /usr/local/bin/node
COPY --from=node_base /usr/local/lib/node_modules /usr/local/lib/node_modules
# COPY 会跟随 /usr/local/bin 下的 npm/npx 符号链并落成普通文件，破坏 npm-cli 内相对 require；
# 与官方镜像一致：在 node_modules/npm 就绪后显式建链。
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
  && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

WORKDIR /app
ENV REPO_ROOT=/app

COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent
COPY trae_agent_online /app/trae_agent_online

RUN python -m venv /app/.venv \
  && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
  && /app/.venv/bin/pip install --no-cache-dir .

COPY trae_config.yaml.example /app/
COPY onlineServiceJS/static /app/onlineServiceJS/static

WORKDIR /app/onlineServiceJS
COPY onlineServiceJS/package.json ./
# 仅生产依赖：去掉 devDependencies，避免弱网下仍解析/拉取 @playwright/test → playwright（镜像内不跑 e2e）
RUN node -e "const fs=require('fs');const p=JSON.parse(fs.readFileSync('package.json','utf8'));delete p.devDependencies;fs.writeFileSync('package.json',JSON.stringify(p,null,2)+'\n');" && npm install --omit=dev

COPY onlineServiceJS/src ./src
COPY onlineServiceJS/skill.md ./skill.md

RUN mkdir -p /app/onlineProject_state/runtime /app/onlineProject_state/logs /app/onlineProject_state/reqLogs /app/onlineProject_state/layers \
  && mkdir -p /app/onlineProject/layers

ARG ACCESS_TOKEN=""
ENV ACCESS_TOKEN="${ACCESS_TOKEN}"
ARG TaskApiEndPoint=""
ENV TaskApiEndPoint="${TaskApiEndPoint}"
ARG BusinessApiEndPoint=""
ENV BusinessApiEndPoint="${BusinessApiEndPoint}"
ARG tenantId=""
ENV tenantId="${tenantId}"
ARG workspaceId=""
ENV workspaceId="${workspaceId}"
ARG taskId=""
ENV taskId="${taskId}"

ENV NODE_ENV=production
# 与 buildGitCloneArgs 中 git -4 配合，降低容器内走 IPv6 导致 HTTPS 克隆失败（Docker Desktop 等）
ENV GIT_HTTP_IPV4=1
EXPOSE 8765

CMD ["node", "src/server.mjs"]
