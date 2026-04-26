#!/bin/sh
# 业务服务为 Node onlineServiceJS（前台 PID 1）。
# 可选：CODE_SERVER_ENABLED=1|true 时在后台启动 code-server（VS Code Web），监听容器内 8888。
set -e
enabled="${CODE_SERVER_ENABLED:-0}"
case "$enabled" in 1|true|yes|TRUE|YES|on|ON) ;; *) enabled=0 ;; esac

if [ "$enabled" != "0" ]; then
  if command -v code-server >/dev/null 2>&1; then
    echo "[onlineServiceJS] code-server listening on 0.0.0.0:8888 (map with docker -p ...:8888)"
    code-server --bind-addr 0.0.0.0:8888 --auth none "${CODE_SERVER_WORKDIR:-/app}" >>/tmp/code-server.log 2>&1 &
  else
    echo "[onlineServiceJS] code-server not installed, skip"
  fi
fi

cd /app/onlineServiceJS
exec node src/server.mjs
