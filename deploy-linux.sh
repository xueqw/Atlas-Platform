#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/api"

python3 -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/python -m pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
fi

mkdir -p data logs
if [ -f atlas.pid ] && kill -0 "$(cat atlas.pid)" 2>/dev/null; then
  kill "$(cat atlas.pid)"
  sleep 2
fi

nohup ./.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "${APP_PORT:-8000}" > logs/atlas.out.log 2> logs/atlas.err.log &
echo $! > atlas.pid
sleep 3

curl -fsS "http://127.0.0.1:${APP_PORT:-8000}/api/health"
echo
echo "Atlas started: pid=$(cat atlas.pid), port=${APP_PORT:-8000}"
