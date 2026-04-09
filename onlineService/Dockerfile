# 不声明 # syntax=docker/dockerfile:1，避免构建前置拉取该前端镜像（弱网常见 TLS 超时）。
# 基于 nastool 官方 Debian 变体：预装 git/curl/ffmpeg 等，减少任务运行时再 apt 装工具。
# 该基础镜像自带 Python 3.10；trae-agent 要求 >=3.12，故从同发行版 bullseye 的官方 Python 镜像并入 /usr/local。
FROM nastool/nas-tools:3.2.3-debian

ENTRYPOINT []

COPY --from=python:3.12-slim-bullseye /usr/local /usr/local
RUN ldconfig 2>/dev/null || true

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent

# 一次安装 trae-agent + online 依赖，避免第二次 pip 再拉 PyPI（易遇 SSL 截断 / JSONDecodeError）
ENV PIP_DEFAULT_TIMEOUT=120
RUN set -e; \
    python3.12 -m venv /app/.venv; \
    /app/.venv/bin/pip install --no-cache-dir --upgrade pip; \
    for i in 1 2 3 4 5; do \
      /app/.venv/bin/pip install --no-cache-dir --retries 10 -e ".[online]" && break; \
      if [ "$i" = 5 ]; then echo "pip install failed after 5 attempts"; exit 1; fi; \
      echo "pip install failed (attempt ${i}/5), retrying..."; \
      sleep $((i * 3)); \
    done

COPY onlineService /app/onlineService

RUN mkdir -p /app/onlineProject_state/runtime /app/onlineProject_state/logs /app/onlineProject_state/reqLogs /app/onlineProject_state/layers

ENV REPO_ROOT=/app
ENV TRAE_VENV=/app/.venv
ARG ACCESS_TOKEN=""
ENV ACCESS_TOKEN="${ACCESS_TOKEN}"
# 任务云容器换票与功能参数（见 machine_container.md）；留空则跳过启动引导，仅本地 ACCESS_TOKEN 生效
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
ENV PYTHONUNBUFFERED=1

EXPOSE 8765
WORKDIR /app/onlineService
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
