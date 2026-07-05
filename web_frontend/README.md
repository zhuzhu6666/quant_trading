# Web Frontend (React + Vite)

> Status: active
> Last verified: 2026-07-06
> Scope: full browser operator console.

完整浏览器操作台，用于承接交易状态、PnL、风控、运维、学习治理和因子治理等复杂视图。小程序只保留轻量状态面。

## 启动方式

```bash
cd web_frontend
npm install
npm run dev
```

开发环境默认监听 `http://127.0.0.1:5173`（或终端输出端口）。

## 环境变量

- `VITE_API_BASE_URL`（可选）
  例：`https://www.zhuzhu666.icu`、`http://127.0.0.1:8000`
  留空时默认同源（例如反代到后端 API 与前端同域）。

## 常用命令

- `npm run typecheck`：TypeScript 类型检查
- `npm run build`：构建产物
- `npm run test`：基础文件存在性 smoke test
- `npm run preview`：本地预览构建结果

## 路由

- `/login` 登录
- `/overview` 总览
- `/trading` 交易
- `/pnl` PnL
- `/risk` 风控
- `/ops` 运维
