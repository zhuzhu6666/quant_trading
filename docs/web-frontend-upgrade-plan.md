# Web Frontend Upgrade Plan

> Last updated: 2026-07-01
> Scope: move the full operator console from the WeChat mini-program to a browser Web frontend.

## 1. 背景

当前小程序已经承载了总览、交易、学习、因子、运维、收益图等完整展示能力，调试成本开始偏高。后续前端分工调整为：

```text
Web 端 = 完整操作台
小程序 = 简洁状态面板
后端 = FastAPI + cTrader + 风控 + 学习治理
```

服务器当前公网入口是 Caddy：

```text
https://www.zhuzhu666.icu -> Caddy -> 127.0.0.1:8000
```

`/api/*` 和 `/ws/state` 继续由 FastAPI 提供。Web 前端上线后，可由 Caddy 托管静态构建产物并继续反代 API。

## 2. 目标边界

Web 端负责完整展示和调试：

- 实时交易状态、账户、持仓、PnL 曲线
- 风控摘要、policy verdicts、trade trace、事件缩放
- 学习治理、参数模板、样本、模型审计
- 因子权重、因子健康、v4 stats
- 后端健康、数据库健康、调度器、进化账本、报告和日志

小程序保留轻量能力：

- 登录
- 后端在线状态
- 交易循环状态
- 会话 PnL / 当前权益 / 持仓数量
- 风控是否触发
- 最近更新时间
- 必要时保留极少数紧急入口，并加二次确认

## 3. 推荐技术栈

建议新建：

```text
web_frontend/
```

推荐组合：

- Vite + React + TypeScript
- TanStack Query
- Zustand
- React Router
- ECharts 或 lightweight-charts
- Playwright

旧 `backend/static` Web Console 构建产物已删除。新的 Web 前端只从 `web_frontend/` 建立源码入口。

## 4. MVP 范围

第一阶段只做可用操作台骨架：

- `/login`
- `/overview`
- `/trading`
- `/pnl`
- `/risk`
- `/ops`

首批接口：

```text
POST /api/auth/login
GET  /api/auth/me
GET  /api/health
GET  /api/live/loop-status
GET  /api/live/account
GET  /api/live/positions
GET  /api/live/session-stats
GET  /api/live/realized-pnl-series?scope=all
GET  /api/risk/summary
GET  /api/system/db-health
GET  /api/ops/backend-readiness
WS   /ws/state
```

控制类接口先只做隐藏入口或二次确认：

```text
POST /api/live/start
POST /api/live/stop
POST /api/live/emergency-close
```

## 5. 迁移顺序

1. 建立 `web_frontend` 项目骨架、登录态、API client、布局和路由。
2. 迁移总览和交易状态，优先复用现有 `/api/live/*` 与 `/ws/state`。
3. 迁移 PnL 图表和持仓表，让 Web 先解决当前最难调试的展示问题。
4. 迁移 risk / ops 页面，补齐服务健康、数据库健康和 trade trace。
5. 迁移 factors 页面，展示 `/api/v4/*` 和 `/api/factor-health/latest`。
6. 最后迁移 learning，因为 `/api/learning/*` 面宽、状态多、治理风险最高。
7. Web 稳定后，瘦身 `miniprogram_v2`，保留轻量状态页面。

## 6. 部署建议

Caddy 推荐形态：

```text
www.zhuzhu666.icu {
  encode zstd gzip

  handle /api/* {
    reverse_proxy 127.0.0.1:8000
  }

  handle /ws/* {
    reverse_proxy 127.0.0.1:8000
  }

  handle {
    root * /var/www/quant-web
    try_files {path} /index.html
    file_server
  }
}
```

旧 `/mobile/*` 和 `/vendor/*` 小程序 H5/web-view 静态入口已清理；新的 Web 端上线时再按本节形态增加静态托管。

## 7. 验收标准

- `https://www.zhuzhu666.icu/api/health` 返回 `status=ok`
- Web 登录后可进入 `/overview`
- `/overview` 能展示后端健康、交易循环状态、账户和 PnL 摘要
- `/trading` 能展示持仓和策略状态
- `/pnl` 能展示 realized PnL 曲线
- `/ws/state` 可连接，断开时自动退回轮询
- 控制类按钮有二次确认和错误提示
- Playwright 至少覆盖登录、总览、交易页首屏

## 8. 文档同步点

引入 Web 后需要同步维护：

- `AGENTS.md`
- `docs/development-workflow.md`
- `docs/startup.md`
- `docs/server-backend-sop.md`
