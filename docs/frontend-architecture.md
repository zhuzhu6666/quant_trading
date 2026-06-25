# 旧 Web Console 前端架构

> 状态: 已归档，保留作历史参考。
> 最后清理: 2026-06-25

本文档原描述 `frontend-v2/src` 下的 Vite/React Web Console。当前仓库的权威前端是 `miniprogram_v2`，后端为 API-only FastAPI，见:

- `backend/app.py`
- `miniprogram_v2/README.md`
- `docs/startup.md`

当前 `frontend-v2` 目录只剩历史构建产物和依赖目录，不应作为新功能开发入口。

## 当前前端结构

```text
miniprogram_v2/
├── pages/
│   ├── overview
│   ├── trading
│   ├── learning
│   ├── factors
│   └── ops
├── services/
│   ├── client.js
│   ├── live.js
│   ├── learning.js
│   ├── factors.js
│   └── ops.js
├── stores/
└── components/
```

核心后端依赖:

- `/api/live/*`
- `/api/v4/*`
- `/api/factor-health/latest`
- `/api/control/*`
- `/api/system/db-health`
- `/api/learning/*`

## 开发原则

新 UI 工作默认进入 `miniprogram_v2`。除非明确要恢复浏览器控制台，否则不要继续补 `frontend-v2` 的 React 组件、Vite 配置或旧 Web 路由。
