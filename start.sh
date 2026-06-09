#!/usr/bin/env bash
# Quant Web Console — Unix launcher
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

echo "=== Starting Quant Backend (port 8000) ==="
python3.12 -m backend --port 8000 &
BACKEND_PID=$!
echo "Backend PID: $BACKEND_PID"
sleep 3

echo "=== Starting Quant Frontend (port 5173, Vite) ==="
cd frontend-v2
trap "kill $BACKEND_PID 2>/dev/null" EXIT
npm run dev
