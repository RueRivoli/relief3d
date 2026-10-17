#!/usr/bin/env bash
# Start the backend (port 8765) and the Vite dev server (port 5288) together.
set -euo pipefail
cd "$(dirname "$0")"
(cd backend && .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload --reload-dir app) &
BACKEND=$!
trap 'kill $BACKEND 2>/dev/null' EXIT
cd frontend && npm run dev
