# Workspace Rules

> Status: active
> Last updated: 2026-08-29
> Scope: unified workspace collaboration rules and platform-specific verification boundaries.

这个仓库从现在开始按下面的规则协作：

## 0. 总则 — 最小充分

用最小充分方案完成当前任务，禁止过度工程化。

- 规划可以偏强，执行必须偏轻；不要全程开最高推理档位。
- 不能证明必要的设计默认不做，不能证明必要的测试默认不加。
- 先确认意图，再用最小改动完成验收。
- 判断依据错了，推理再完整结论也是错的 — 优先读代码验证事实，不靠检索/猜测拼结论。

**常见失败模式（触发即停）：**
1. 没理解真实意图只修表面；2. 该做干净根因修复却用补丁/兼容层/双轨/副本堆代码；3. 为小概率场景过度设计抬高维护成本；4. 依据错导致结论错；5. 该读代码却用检索猜测替代；6. 以“补测试”为由加抽象扩范围。

## 1. 统一开发工作区

`/home/ubuntu/quant_trading` 是服务器端统一开发与运行事实工作区，可直接修改：

- `backend` / `execution` / `alpha` / `risk` / `monitor` / `config` / `scripts` / 文档与测试

前端代码（`miniprogram_v2` / `web_frontend`）已迁移到 Windows 本地，服务器不再包含前端代码、不拉取前端目录、不做前端构建。

服务器改动仍需遵守各领域事实源、安全边界和针对性验证要求。

`.env`、运行数据、日志、数据库和 systemd 仍只在任务明确涉及且完成只读确认后按服务器 SOP 操作；统一开发不扩大运行态变更授权。

## 2. 平台专属验证

Linux 服务器继续负责生产运行验证：

- 后端接口 / 交易循环 / 风控逻辑 / cTrader 执行链路 / 环境变量 / systemd / 数据库 / 日志排查 / 后端 API、WebSocket、systemd、数据库和公网 API/WSS 验证

Windows 仅在需要平台工具时用于补充验证：

- 微信开发者工具联调 / Windows/浏览器兼容性检查

平台工具限制不再限制源代码修改位置。

## 3. 默认工作流

```text
理解需求 -> 产出最小计划（目标/非目标/验收/不改范围）
  -> 确认唯一生产 authority 和待删除旧路径
  -> 直接修改前端或后端
  -> 运行对应的最小测试/构建
  -> 删除被替代实现、兼容字段和实现耦合测试
  -> 按领域做服务、浏览器或平台工具验证
```

### 3.0 前置：理解与最小计划

1. 先理解需求再动手，不要先改代码再猜意图；动手前先复述：用户真正想要什么 / 本次范围 / 明确不做的事 / 怎样算完成。
2. 规划阶段可用较高推理，执行阶段默认中低推理/轻量模型落地。
3. 不要默认并行拉起多个 Agent，单线做完再决定是否拆分；只启用完成任务所必需的 skill，不安装重流程 skill。
4. 必须先产出最小计划再执行，计划写清：目标、非目标、验收标准、不改动的范围；计划不清不改代码。

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
7. 每批先跑针对性测试；全量测试只在阶段收口、发布门或改动影响面无法可靠界定时运行（详见 §8 测试纪律）。
8. 不以拆文件、增加 schema 或新增状态投影代替架构收敛；验收以生产 authority 数量、调用链和净删除结果为准。

> 与 §7 行动边界联动：若发现正在新增抽象/框架/配置层、为未来预设、叠加约束、多文件无关改动、造第二套实现兼容旧逻辑、借机补全套测试，即触发本节收敛规则，必须停下来改小方案。

## 3.1 当前分支/工作区约定

- 服务器统一使用 `main` 分支，sparse checkout 只包含后端、脚本和文档，不包含 `miniprogram_v2/`、`web_frontend/` 及前端构建产物。
- 前端代码（`miniprogram_v2` / `web_frontend`）在 Windows 本地独立仓库维护。
- 运行态、数据库、systemd 和日志验证仍以 Linux 服务器为准。
- Windows 本地产生的必要修正提交到同一 `main`，不得形成长期分叉。

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
- 运行时状态主库是 PostgreSQL `runtime`，事实与学习事件主库是 `canonical_v2`：
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

## 3.3 客户端与服务器边界

- 前端代码（`miniprogram_v2` / `web_frontend`）已全部迁移到 Windows 本地，服务器工作树不包含任何前端代码、构建产物或静态资源。
- 服务器只提供后端 API 与 `/ws/state` WebSocket：
  - `https://www.zhuzhu666.icu` 仅作为 API/WSS 公网入口；
  - Caddy 反代到本机 `127.0.0.1:8000`，不托管前端静态资源。
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
4. 用代码、服务、PostgreSQL `runtime`/`canonical_v2`、`runtime_kv`、日志和测试刷新易变事实。

完整文档治理规则见 [docs/documentation-governance.md](docs/documentation-governance.md)。

## 7. 行动边界与不可逆操作

1. **动手前复述**（见 §3.0）：用户真正想要什么 / 本次范围 / 明确不做 / 怎样算完成。
2. **不可逆操作需确认口令**：任何不可逆操作必须等待用户回复确认口令后再执行；确认口令由用户指定，没有口令、口令错误或其他回复一律拒绝执行。
3. **默认可逆、可直接执行**：Git 回滚/还原/切分支、把文件移动到当前仓库备份目录、跑测试/查看 diff/生成计划/只读分析。
4. **触发即停、改小方案**：执行中若发现 §3.0.1 已禁止的行为（新增不必要抽象/框架/配置层、为未来预设、叠加约束、多文件无关改动、造第二实现兼容旧逻辑、借机补全套测试），必须立即停下来重写最小计划。

## 8. 测试纪律 — 只为当前验收服务

测试不负责补齐历史覆盖率，不负责设计未来测试体系。

1. 优先跑与本次改动相关的现有测试；现有测试能证明正确就不要新增。
2. 仅两种情况允许新增测试：a) 改了行为但现有测试盖不到；b) 用户明确要求补测试。
3. 新增测试最多覆盖本次改动的 1 个主路径，必要时加 1 个关键失败路径。
4. 禁止：为更完整而扩大范围、补无关模块、引入新测试框架/工具/基建、写大量快照/参数化矩阵/端到端套件、为未要求边界写测试、先改测试倒逼产品变复杂、把测试变绿当成继续加抽象的理由。

**新增测试前必答：**
- 验证哪个已被接受的需求？
- 去掉它，现有测试是否无法发现这次回归？
- 它是否比实现本身更复杂？若测试代码比实现更长更绕，默认视为过度工程，应删测试或缩小实现。

## 9. 模型分工与推理档位

- 需求澄清和方案审查：用较强模型。
- 写代码、改代码、跑测试：用中低配/更轻量执行模型。
- 执行模型若开始叠架构、加兼容、扩范围、补大套测试：立刻停下来，重写最小计划（回到 §3.0）。

## 10. 完成前检查

- [ ] 已复述意图和验收标准，已标明非目标
- [ ] 方案是最小方案，不是最大方案
- [ ] 优先读了相关代码，而非靠检索拼结论
- [ ] 只改了完成任务所需的最小文件集
- [ ] 相关现有测试已跑过；未为未要求场景新增测试
- [ ] 若新增测试，只锁本次行为、条数很少、未引入新依赖或目录结构
- [ ] diff 小，无多余文件，无残留调试代码，无为完整而额外施工
- [ ] 已按 §3.0.1 完成收敛：声明替代对象、删除清单，旧路径已删或已列入本批删除
