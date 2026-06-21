# =============================================================================
# Quant Trading Backend — Dockerfile (WeChat mini program 专用)
# 去掉了前端构建，体积更小，build 更快。
# =============================================================================

FROM python:3.11-slim AS builder
WORKDIR /app

# 系统依赖（numpy/pandas/scipy 需要 gcc）
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.txt .
RUN sed '/MetaTrader5/d' requirements.txt > requirements-docker.txt && \
    pip install --no-cache-dir -r requirements-docker.txt

# ---- Runtime ----
FROM python:3.11-slim
WORKDIR /app

# 运行时系统依赖（DuckDB 和网络相关）
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# 复制 Python 包
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# 复制项目代码（不含 node_modules、前端等）
COPY backend/ backend/
COPY alpha/ alpha/
COPY config/ config/
COPY monitor/ monitor/
COPY risk/ risk/
COPY research/ research/
COPY execution/ execution/
COPY start-all.py ./

# 数据目录作为 volume（持久化 DuckDB、charts、logs）
VOLUME ["/app/data", "/app/logs"]

EXPOSE 8000

# 健康检查
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/api/health')" || exit 1

CMD ["uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8000"]
