# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent

RUN python -m venv /app/.venv \
    && /app/.venv/bin/pip install --no-cache-dir --upgrade pip \
    && /app/.venv/bin/pip install --no-cache-dir -e /app

COPY onlineService /app/onlineService
RUN /app/.venv/bin/pip install --no-cache-dir -r /app/onlineService/requirements.txt

RUN mkdir -p /app/onlineProject/layers /app/onlineService/runtime

ENV REPO_ROOT=/app
ENV TRAE_VENV=/app/.venv
ARG ACCESS_TOKEN=""
ENV ACCESS_TOKEN="${ACCESS_TOKEN}"
# 任务云容器换票与功能参数（见 machine_container.md）；留空则跳过启动引导，仅本地 ACCESS_TOKEN 生效
ARG TaskApiEndPoint=""
ENV TaskApiEndPoint="${TaskApiEndPoint}"
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
