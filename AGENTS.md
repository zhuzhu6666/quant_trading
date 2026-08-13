# Workspace Rules

> Status: active
> Last updated: 2026-07-26
> Scope: unified workspace collaboration rules and platform-specific verification boundaries.

这个仓库从现在开始按下面的规则协作：

## 1. 统一开发工作区

`/home/ubuntu/quant_trading` 是前后端统一开发与运行事实工作区，可直接修改：

- `miniprogram_v2`
- `web_frontend`
- `backend`
- `execution`
- `alpha`
- `risk`
- `monitor`
- `config`
- 文档与测试

不再按“Windows 只做前端、Linux 只做后端”分工。改动仍需遵守各领域事实源、
安全边界和针对性验证要求。

`.env`、运行数据、日志、数据库和 systemd 仍只在任务明确涉及且完成只读确认后
按服务器 SOP 操作；统一开发不扩大运行态变更授权。

## 2. 平台专属验证

Linux 服务器继续负责生产运行验证：

- 后端接口
- 交易循环
- 风控逻辑
- cTrader 执行链路
- 环境变量
- systemd
- 数据库
- 日志排查
- 后端 API、WebSocket、systemd、数据库和公网 API/WSS 验证

Windows 仅在需要平台工具时用于补充验证：

- 微信开发者工具联调；
- Windows/浏览器兼容性检查。

平台工具限制不再限制源代码修改位置。

## 3. 默认工作流

```text
统一工作区确认事实源和影响面
  -> 确认唯一生产 authority 和待删除旧路径
  -> 直接修改前端或后端
  -> 运行对应的最小测试/构建
  -> 删除被替代实现、兼容字段和实现耦合测试
  -> 按领域做服务、浏览器或平台工具验证
```

### 3.0.1 强制架构收敛规则

所有修复和功能批次都必须遵守：

1. 一个事实只能有一个生产计算者和一个写入者；API、readiness、replay 和前端只能复用或只读投影。
2. 新实现必须声明替代对象和删除清单；不能回答“删除什么”的新 service、wrapper、adapter、表、线程、调度器、阈值或兼容字段默认不准新增。
3. 涉及开仓和风险事实时，Safety、Readiness、Risk sizing 三层权力不得互相重算：
   - Safety 只负责必须立即禁止新增风险的硬事实；
   - Readiness 只读判断当前事实是否足够；
   - Risk sizing 只负责风险计算和最终仓位。
4. 同一 blocker 只能在一个 owner 中计算一次，其他位置复用稳定 reason code，不再叠加同义门控。
5. canonical 路径验证通过但旧路径未删除，批次仍视为未完成；不得以“兼容”为由无限期双轨。
6. 新抽象只有在立即删除重复实现、隔离真实变化源或服务多个真实调用方时才允许；单调用方转发层和假想扩展点直接内联。
7. 每批先跑针对性测试；全量测试只在阶段收口、发布门或改动影响面无法可靠界定时运行。
8. 不以拆文件、增加 schema 或新增状态投影代替架构收敛；验收以生产 authority 数量、调用链和净删除结果为准。

## 3.1 当前分支/工作区约定

- 所有工作区统一使用 `main` 分支。
- Windows 可继续使用 sparse checkout，默认保留：
  - `miniprogram_v2/`
  - `web_frontend/`
  - `docs/`
  - `AGENTS.md`
  - `README.md`
  - `.gitignore`
- 前端、后端、文档和规则均可在 Linux 统一工作区直接修改。
- 运行态、数据库、systemd 和日志验证仍以 Linux 服务器为准。
- Windows 平台验证产生的必要修正也提交到同一 `main`，不得形成长期分叉。

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
- 运行时状态与学习审计主库是 PostgreSQL `state_v1`：
  - `data/state.db` 活跃路径不再保留，也不再保留本地 SQLite 冷备
  - 排查运行态状态禁止用 `sqlite3 data/state.db` 或手写 `sqlite3.connect("data/state.db")`
  - 只读查询统一用 `.venv/bin/python scripts/state_query.py --sql "..."`
  - 业务代码统一用 `backend.core.db.get_state_pg_conn()` / `get_state_conn()`，不要新增生产路径写入 SQLite state
- 历史 tick 采集链已于 2026-07-11 退役：
  - Dukascopy/cTrader 历史 tick 拉取、月库、健康检查、调度任务和本地数据均已删除
  - cTrader 主连接的实时 `ProtoOASpotEvent` 报价必须保留；它用于实时 bid/ask/mid、持仓保护和执行参考价，不属于历史 tick 采集
  - 不得恢复 `ticks.duckdb`、`ticks_monthly/`、Dukascopy tick timer 或历史 tick writer
- L2 数据链路已于 2026-07-11 退役：
  - cTrader 主连接仅保留 spot/account/positions/execution，depth protobuf、订阅、内存簿、writer、配置和风控字段均已删除
  - `data/l2_monthly/` 和 `data/l2.duckdb` 已删除，不保留历史 L2 数据
  - 退役原因：cTrader 该深度源的 size 是固定对称档位，无法代表真实挂单量或 imbalance
  - `quant-l2-collector.service` 和 `scripts/run_l2_collector.py` 已移除，不得恢复
- 这些运行数据不进入 GitHub。

## 3.3 当前小程序图表约定

- 小程序收益图当前使用原生页面：
  - `miniprogram_v2/pages/pnl-chart/index`
- 图表库使用小程序本地 vendored `uCharts`：
  - `miniprogram_v2/vendor/ucharts/u-charts.min.js`
- 不再按 `web-view` / nginx 静态 H5 / `lightweight-charts` 方案理解。
- 当前小程序没有 `web-view` 业务域名配置权限，因此不要再要求配置 `www.zhuzhu666.icu` 为 web-view 业务域名。

## 3.4 客户端与服务器边界

- `web_frontend/` 是完整 Tauri 桌面端的 renderer 源码；操作台只在本人本地桌面壳中运行，
  不作为服务器上的公网浏览器静态站点部署。
- 小程序只保留简洁状态界面；复杂图表、交易明细、风控、学习治理、因子治理和运维调试
  由本地 Tauri 桌面端承接。
- 服务器只提供客户端共用的后端 API 与 `/ws/state` WebSocket：
  - `https://www.zhuzhu666.icu` 仅作为 API/WSS 公网入口；
  - Caddy 只反代到本机 `127.0.0.1:8000`，不托管前端 `index.html`、`dist` 或静态 asset；
  - 服务器工作树采用后端-only sparse checkout，不拉取或保留 `web_frontend/`、`miniprogram_v2/`
    和前端构建产物。
- 旧 Web Console 打包产物、旧小程序 H5/web-view 静态入口、旧 Nginx H5 路线均不再保留。

## 4. 遇到问题时的默认顺序

```text
先看日志
  -> 再看接口
  -> 再改代码
  -> 最后重启验证
```

## 5. 系统级改动前的文档治理

涉及后端、交易、风控、因子、学习、自主治理、RuntimeConfig、数据库或 API contract 的改动，默认先按下面顺序确认影响面：

1. 先看 [docs/system-source-of-truth.md](docs/system-source-of-truth.md)，确认当前事实源和权力边界。
2. 再看 [docs/legacy-debt-register.md](docs/legacy-debt-register.md)，确认有没有历史残留或废弃口径。
3. 再按 [docs/change-impact-checklist.md](docs/change-impact-checklist.md) 扫 live、shadow、learning、readiness、frontend contract 和回滚影响。
4. 在修改前写清本批 canonical authority、被替代路径、删除清单和不新增项。
5. 改动完成后先删除旧路径，再同步事实源、旧债、验收矩阵和当前状态。

历史 planning 文档和旧代码注释只能作为背景，不能单独作为实现依据。

## 6. 新对话与文档入口

用户要求“读一遍文档”或“确认当前项目状态”时，不再逐份读取历史设计：

1. 读 [docs/README.md](docs/README.md)，获取阶段、运行姿态、当前主线和文档路由。
2. 系统级修改再依次读事实源、活跃旧债和影响面清单。
3. 只按任务读取对应领域合同或 [docs/server-backend-sop.md](docs/server-backend-sop.md)。
4. 用代码、服务、PostgreSQL `state_v1`、`runtime_kv`、日志和测试刷新易变事实。

完整文档治理规则见 [docs/documentation-governance.md](docs/documentation-governance.md)。
