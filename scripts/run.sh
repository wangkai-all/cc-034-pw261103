#!/usr/bin/env bash
# Start the delivery console (backend serves the built frontend).
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ ! -x .venv/bin/python ]]; then
  echo "缺少虚拟环境：请先运行 python3 -m venv .venv 并安装 backend/requirements.txt" >&2
  exit 1
fi

if [[ ! -f frontend/dist/index.html ]]; then
  echo "前端尚未构建：运行 cd frontend && npm install && npm run build" >&2
fi

exec .venv/bin/python -m uvicorn app.main:app \
  --app-dir backend \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-8000}" \
  "$@"
