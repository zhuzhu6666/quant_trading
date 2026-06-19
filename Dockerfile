# =============================================================================
# Quant Trading Backend + Frontend — Multi-stage Dockerfile
# cTrader execution + factor pipeline + Web UI, deployable to any Linux server.
# =============================================================================

# ---- Stage 1: Frontend build ----
FROM node:20-alpine AS frontend-builder
WORKDIR /frontend
COPY frontend-v2/package.json frontend-v2/package-lock.json* ./
RUN npm ci
COPY frontend-v2/ ./
RUN npm run build

# ---- Stage 2: Python dependencies ----
FROM python:3.11-slim AS builder
WORKDIR /app
COPY requirements.txt .
RUN sed '/MetaTrader5/d' requirements.txt > requirements-docker.txt && \
    pip install --no-cache-dir -r requirements-docker.txt

# ---- Stage 3: Runtime ----
FROM python:3.11-slim
WORKDIR /app

# Copy Python packages
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Copy application code
COPY backend/ backend/
COPY alpha/ alpha/
COPY config/ config/
COPY data/ data/
COPY monitor/ monitor/
COPY risk/ risk/
COPY research/ research/
COPY execution/ execution/

# Copy root-level scripts (scheduler invokes these via subprocess)
COPY _pull_dukascopy_incremental.py ./
COPY start-all.py ./

# Copy built frontend into backend/static
COPY --from=frontend-builder /frontend/dist backend/static/

EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
