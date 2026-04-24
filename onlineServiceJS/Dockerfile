FROM python:3.12-slim-bookworm
# Python 虚拟环境供容器内 trae-cli；Node 20+ 供 onlineServiceJS（与 skill.md 一致）
RUN apt-get update && apt-get install -y --no-install-recommends \
    git ca-certificates build-essential curl \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY pyproject.toml README.md /app/
COPY trae_agent /app/trae_agent
COPY trae_agent_online /app/trae_agent_online
RUN python -m venv /app/.venv && /app/.venv/bin/pip install --no-cache-dir --upgrade pip && /app/.venv/bin/pip install --no-cache-dir .

COPY onlineServiceJS/package.json onlineServiceJS/package-lock.json /app/onlineServiceJS/
WORKDIR /app/onlineServiceJS
RUN npm ci --omit=dev
COPY onlineServiceJS /app/onlineServiceJS/

WORKDIR /app/onlineServiceJS
EXPOSE 8765
ENV PORT=8765
CMD ["node", "src/server.mjs"]
