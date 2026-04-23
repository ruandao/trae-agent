# 构建上下文须为 trae-agent 仓库根目录（与 onlineService/Dockerfile 相同约定）。
# 不声明 # syntax=docker/dockerfile:1，避免弱网拉取前端语法镜像失败。
FROM node:22-bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
  && rm -rf /var/lib/apt/lists/*

WORKDIR /app
ENV REPO_ROOT=/app

COPY trae_config.yaml.example /app/
COPY onlineServiceJS/static /app/onlineServiceJS/static

WORKDIR /app/onlineServiceJS
COPY onlineServiceJS/package.json ./
RUN npm install --omit=dev

COPY onlineServiceJS/src ./src
COPY onlineServiceJS/skill.md ./skill.md

RUN mkdir -p /app/onlineProject_state/runtime /app/onlineProject_state/logs /app/onlineProject_state/reqLogs /app/onlineProject_state/layers

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
EXPOSE 8765

CMD ["node", "src/server.mjs"]
