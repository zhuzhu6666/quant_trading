# Desktop Renderer (React + Vite)

> Status: active
> Last verified: 2026-08-13
> Scope: personal local Tauri desktop renderer; no production browser deployment.

这是本人本地 Tauri 操作台的 React renderer 源码，用于承接交易状态、PnL、风控、运维、
学习治理和因子治理等复杂视图。它不作为服务器上的公网浏览器前端部署；服务器只提供
桌面端和小程序共用的 API/WSS。小程序只保留轻量状态面。本项目不做 Windows 对外发行；
签名安装包、GitHub Releases 和自动更新不属于日常使用路径。

本人已确认本机认证和基本使用通过；当前剩余问题集中在工作区排版和数据流，按
`docs/frontend-refactor-status.md` 与 `docs/frontend-refactor-acceptance-matrix.md` 逐项定位。

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
  Tauri 留空时使用当前生产后端 `https://www.zhuzhu666.icu`，仍可用该变量覆盖到 staging
  或本地后端。`npm run dev` 仅用于本机 renderer/Tauri 联调，不代表需要部署浏览器站点。

## 常用命令

- `npm run typecheck`：TypeScript 类型检查
- `npm run build`：构建产物
- `npm run test`：renderer、Fact/auth、WS、架构删除和工作区合同测试
- `npm run preview`：本地预览构建结果
- `npm run tauri -- dev`：本人本地 Tauri 开发运行
- `npm run tauri -- build`：本人本地构建桌面 executable；不代表已签名或已发布

## 路由

- `/login` 登录
- `/trade-ops` 交易运营
- `/risk-desk` 风险台
- `/research` 研究实验室
- `/governance` 治理中心
- `/ops` 运维
