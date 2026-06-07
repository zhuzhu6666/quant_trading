#!/usr/bin/env bash
# Quant Web Console — prod launcher (single port :8000)
set -e
PROJECT_ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_ROOT"

echo "=== Building frontend for static export ==="
cd frontend
npm run build
cd ..

echo "=== Copying static output to backend/static/ ==="
rm -rf backend/static
mkdir -p backend/static
cp -r frontend/out/* backend/static/

echo "=== Starting uvicorn on port 8000 (serves API + static frontend) ==="
python3.12 -m backend --port 8000
