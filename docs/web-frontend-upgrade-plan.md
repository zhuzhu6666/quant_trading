# Web Frontend Console Contract

> Status: active
> Last verified: 2026-07-06
> Scope: current browser Web frontend role and remaining console expansion path.

## 1. 背景

当前前端分工已经调整为：

```text
Web 端 = 完整操作台
小程序 = 简洁状态面板
后端 = FastAPI + cTrader + 风控 + 学习治理
```

服务器当前公网入口是 Caddy：

```text
https://www.zhuzhu666.icu -> Caddy -> 127.0.0.1:8000
```

`/api/*` 和 `/ws/state` 继续由 FastAPI 提供。Web 前端由 `web_frontend/` 源码维护，生产入口通过 Caddy 与后端同域承接。

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

## 3. 技术栈

源码目录：

```text
web_frontend/
```

当前组合：

- Vite + React + TypeScript
- TanStack Query
- Zustand
- React Router
- ECharts 或 lightweight-charts
- Playwright

旧 `backend/static` Web Console 构建产物已删除。新的 Web 前端只从 `web_frontend/` 建立源码入口。

## 4. 当前核心范围

核心路由：

- `/login`
- `/overview`
- `/trading`
- `/pnl`
- `/risk`
- `/ops`
- `/learning`
- `/models`
- `/v15`

核心接口：

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
GET  /api/ops/v15/phase0
GET  /api/ops/replay/latest
GET  /api/ops/incident-control
GET  /api/ops/release/latest
GET  /api/v4/catalog
WS   /ws/state
```

控制类接口必须保留二次确认：

```text
POST /api/live/start
POST /api/live/stop
POST /api/live/emergency-close
```

## 5. 后续扩展顺序

1. 保持 overview/trading/pnl/risk/ops 作为完整控制台骨架。
2. 因子治理页优先消费 `/api/v4/catalog` 和 Factor Cards 后端语义，不在前端重新推断 role。
3. Learning/governance 页面优先消费后端 governance/status/progress 展示字段，前端只做渲染。
4. Readiness、overlay、catalog snapshot、governance run、rollback 状态进入运维页面。
5. V15 cockpit 已由 `/v15` 承接 Runtime、Factors、Governance、Replay、Risk、Learning、Incidents、Release 八个工作面；控制按钮保留二次确认并调用后端受控接口。
6. 小程序继续保持轻量状态面，不恢复复杂图表、治理详情或旧 web-view 路线。

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

旧 `/mobile/*` 和 `/vendor/*` 小程序 H5/web-view 静态入口已清理；不要恢复旧 web-view/H5 路线。

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
