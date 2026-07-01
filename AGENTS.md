# Workspace Rules

> Last updated: 2026-07-01

这个仓库从现在开始按下面的规则协作：

## 1. 本地 Windows

本地默认只做这些内容：

- `miniprogram_v2`
- `web_frontend`（新 Web 操作台，规划/开发中）
- 小程序页面、交互、展示
- Web 前端页面、交互、展示
- 微信开发者工具验证
- 文档

本地默认不要改这些内容：

- `backend`
- `execution`
- `alpha`
- `risk`
- `monitor`
- `.env`
- `config`
- `data`
- `logs`

## 2. Linux 服务器

服务器默认负责这些内容：

- 后端接口
- 交易循环
- 风控逻辑
- cTrader 执行链路
- 环境变量
- systemd
- 数据库
- 日志排查

服务器默认不做这些内容：

- 小程序页面开发
- 微信开发者工具联调

## 3. 默认工作流

```text
本地做前端（小程序 + Web）
服务器只做后端
```

## 3.1 当前分支/工作区约定

- 本地 Windows 和 Linux 服务器统一使用 `main` 分支。
- 本地 Windows 已启用 sparse checkout，默认只保留：
  - `miniprogram_v2/`
  - `web_frontend/`（建立后纳入）
  - `docs/`
  - `AGENTS.md`
  - `README.md`
  - `.gitignore`
- 后端、交易、数据库、systemd、日志相关改动一律在服务器 `main` 上完成。
- 小程序和 Web 前端改动一律在本地 Windows 的 `main` 上完成。
- 文档/规则类改动统一提交到 `main`，本地和服务器都从 `main` 拉取。

## 3.2 当前数据约定

- K 线数据在服务器上按月库保存：
  - `data/bars_monthly/bars_YYYY_MM.duckdb`
  - `data/bars.duckdb` 是指向当前月份库的兼容链接
  - `data/ctrader_data.duckdb` 暂保留为旧 K 线冷备/兼容库，不再作为 live K 线主写入入口
- 外部研究数据主库：
  - `data/external_data.duckdb`
  - 承载 `cot_gold`、`etf_holdings`、`cb_gold`、`macro_daily`、`etf_daily`
  - 外部表需要保留 `release_at`、`fetched_at`、`source`，因子/回测只能在 `release_at` 之后使用
  - FRED 宏观数据使用 `QUANT_FRED_API_KEY`；未配置时跳过，不阻塞 COT/ETF/events
  - 原始响应/文件缓存放在 `data/external_raw/`、`data/cot/`、`data/sec_gld/`
  - 旧路径 `DataStore("data/ctrader_data.duckdb")` 的外部表写入会兼容跳转到该库
- 经济事件日历独立保存：
  - `data/events.duckdb`
  - 风控事件缩放模块 `execution/event_sizing.py` 直接读取该库
- tick 数据在服务器上按月库保存：
  - `data/ticks_monthly/ticks_YYYY_MM.duckdb`
  - `data/ticks.duckdb` 是指向当前月份库的兼容链接
- L2 数据在服务器上由 cTrader 主连接采集，不再使用独立 Open API 连接：
  - `quant-l2-collector.service` 和 `scripts/run_l2_collector.py` 属于旧方案，已移除；不要再恢复为默认方案
  - 月库：`data/l2_monthly/l2_YYYY_MM.duckdb`
  - `data/l2.duckdb` 是指向当前月份库的兼容链接，由 L2 writer 跨月自动刷新
  - `risk_require_l2_depth=false` 只表示交易风控不依赖 L2；研究采集由 `l2_collection_enabled` 控制
- 这些运行数据不进入 GitHub。

## 3.3 当前小程序图表约定

- 小程序收益图当前使用原生页面：
  - `miniprogram_v2/pages/pnl-chart/index`
- 图表库使用小程序本地 vendored `uCharts`：
  - `miniprogram_v2/vendor/ucharts/u-charts.min.js`
- 不再按 `web-view` / nginx 静态 H5 / `lightweight-charts` 方案理解。
- 当前小程序没有 `web-view` 业务域名配置权限，因此不要再要求配置 `www.zhuzhu666.icu` 为 web-view 业务域名。

## 3.4 Web 前端升级约定

- 新 Web 前端用于接替小程序的完整操作台能力，目录约定为 `web_frontend/`。
- 小程序最终只保留简洁状态界面；复杂图表、交易明细、风控、学习治理、因子治理、运维调试放到 Web 端。
- 当前公网入口由服务器 Caddy 承接：
  - `https://www.zhuzhu666.icu`
  - Caddy 反代到本机 `127.0.0.1:8000`
- 旧 Web Console 打包产物、旧小程序 H5/web-view 静态入口、旧 Nginx H5 路线均不再保留。

## 4. 遇到问题时的默认顺序

```text
先看日志
  -> 再看接口
  -> 再改代码
  -> 最后重启验证
```

## 5. 详细规则

完整说明见：

- [docs/development-workflow.md](docs/development-workflow.md)
- [docs/server-backend-sop.md](docs/server-backend-sop.md)
