# Quant Trading Web Console — Startup Guide

## 开发模式

两进程并行：Vite dev server (:5173) + FastAPI backend (:8000)

```bash
# 终端 1: 启动后端
cd C:\Users\zhu\quant_trading
"C:\Users\zhu\AppData\Local\Programs\Python\Python312\python.exe" -m backend --port 8000

# 终端 2: 启动前端（带 HMR）
cd C:\Users\zhu\quant_trading\frontend-v2
npx vite
```

打开 `http://localhost:5173`。前端自动代理 `/api/*` 到后端。

## 生产模式

单端口部署，后端同时提供 API 和静态文件

```bash
start-prod.bat
```

流程：
1. `npm run build` 构建前端到 `frontend-v2/dist/`
2. 复制 `dist/*` 到 `backend/static/`
3. 启动 uvicorn 监听 `:8000`

打开 `http://localhost:8000`。

## 环境要求

- Python 3.12+ (后端)
- Node.js 18+ (前端)
- MT5/cTrader 终端 (可选，用于实盘数据)

## 依赖安装

```bash
# 后端
pip install -r requirements.txt

# 前端
cd frontend-v2 && npm install
```

## 登录

默认使用任意密码登录，后端返回 HS256 JWT (24h 过期)。

## 启动脚本说明

| 脚本 | 用途 |
|------|------|
| `start.bat` | 开发模式：启动后端 + Vite 前端 |
| `start-prod.bat` | 生产模式：构建前端 + 复制静态文件 + 启动 uvicorn |

## 验证

1. 打开 http://localhost:8000
2. 输入任意密码登录
3. 仪表盘显示实时 KPI 卡片
4. 点击功能按钮（交易/因子/实验/数据/系统）验证下滑面板
