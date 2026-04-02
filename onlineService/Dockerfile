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
ENV PYTHONUNBUFFERED=1

EXPOSE 8765
WORKDIR /app/onlineService
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8765"]
