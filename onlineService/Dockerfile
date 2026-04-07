# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    for i in 1 2 3; do \
      apt-get update && apt-get install -y --no-install-recommends git ca-certificates && break; \
      echo "apt install failed (attempt ${i}/3), retrying..."; \
      sleep 2; \
    done; \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent

# 一次安装 trae-agent + online 依赖，避免第二次 pip 再拉 PyPI（易遇 SSL 截断 / JSONDecodeError）
ENV PIP_DEFAULT_TIMEOUT=120
RUN set -e; \
    python -m venv /app/.venv; \
    /app/.venv/bin/pip install --no-cache-dir --upgrade pip; \
    for i in 1 2 3 4 5; do \
      /app/.venv/bin/pip install --no-cache-dir --retries 10 -e ".[online]" && break; \
      if [ "$i" = 5 ]; then echo "pip install failed after 5 attempts"; exit 1; fi; \
      echo "pip install failed (attempt ${i}/5), retrying..."; \
      sleep $((i * 3)); \
    done

COPY onlineService /app/onlineService

RUN mkdir -p /app/onlineProject/layers /app/onlineService/runtime

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
