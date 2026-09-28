#!/usr/bin/env bash
set -euo pipefail

project_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
api_python="$project_root/api/.venv/bin/python"

if [[ ! -x "$api_python" ]]; then
  python3 -m venv "$project_root/api/.venv"
  "$api_python" -m pip install -r "$project_root/api/requirements.txt"
fi

if [[ ! -d "$project_root/web/node_modules" ]]; then
  npm ci --prefix "$project_root/web"
fi

(
  cd "$project_root/api"
  "$api_python" -m uvicorn app.main:app --reload --port 8000
) &
api_pid=$!

(
  cd "$project_root/web"
  npm run dev
) &
web_pid=$!

cleanup() {
  kill "$api_pid" "$web_pid" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "Atlas workspace: http://localhost:5173"
echo "Atlas API docs: http://localhost:8000/docs"
wait "$api_pid" "$web_pid"
