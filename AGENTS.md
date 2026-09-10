# Workspace Rules

> Status: active
> Last updated: 2026-09-10
> Scope: 工作区协作规则、架构收敛、测试纪律和硬边界。环境与数据事实只做必要摘要，权威版本见 docs/ 事实源。

这个仓库从现在开始按下面的规则协作。

## 1. 硬边界（不协商）

1. **不可逆操作要口令**：任何不可逆操作必须等待用户回复确认口令后再执行；确认口令由用户指定，没有口令、口令错误或其他回复一律拒绝执行。
2. **默认可逆、直接执行**：Git 回滚/还原/切分支、把文件移到仓库备份目录、跑测试、看 diff、出计划、只读分析都不需要口令。
3. **运行态只读 PostgreSQL `runtime` / `canonical_v2`**：本地 SQLite state 路径一律不用（磁盘上残留的 `data/state.db`、`./state.db` 不是可用入口）。只读查询统一 `.venv/bin/python scripts/state_query.py --sql "..."`；业务代码统一 `backend.core.db.get_state_pg_conn()` / `get_state_conn()`。
4. **已退役链路不恢复**：历史 tick 采集（Dukascopy/cTrader 历史 tick、`ticks_monthly/`、tick timer/writer）、L2 collector（`data/l2_monthly/`、`quant-l2-collector.service`）、服务器端前端构建。
5. **运行态资源先只读确认**：`.env`、运行数据、日志、数据库和 systemd 只在任务明确涉及、并完成只读确认后按 `docs/server-backend-sop.md` 操作；统一开发不扩大运行态变更授权。

cTrader 实时 `ProtoOASpotEvent` 报价保留（实时 bid/ask/mid、持仓保护、执行参考价），不属于历史 tick 采集。

## 2. 工作流

```text
复述需求 -> 最小计划 -> 声明 authority/删除清单 -> 改代码
  -> 跑最小验证 -> 删除被替代实现 -> 领域验证
```

1. **先复述再动手**：用户真正想要什么 / 本次范围 / 明确不做的事 / 怎样算完成。计划不清不改代码。
2. **最小计划**：目标、非目标、验收标准、不改动的范围；并声明本批 canonical authority、被替代路径、删除清单、不新增项。不能证明必要的设计和测试默认不做。
3. **直接改代码**：服务器工作区可直接改 `backend` / `execution` / `alpha` / `risk` / `monitor` / `config` / `scripts` / `tests` 和文档。
4. **最小验证**：`.venv/bin/python -m pytest tests/<target> -q`（`pytest.ini` 的 `testpaths=tests`；`postgres_integration` marker 需独立 PostgreSQL 服务）。运行态只读一律走 `scripts/state_query.py`。
5. **先删旧再判定完成**：删除被替代实现、兼容字段和实现耦合测试后，本批才算完成。
6. **按领域验证**：排障顺序固定为日志 → 接口 → 代码 → 重启验证；平台工具验证见 §5。

完成前自查：只改最小文件集、diff 小且无调试残留；未为未要求场景新增测试；已声明替代对象和删除清单，旧路径已删或列入本批删除。

## 3. 架构收敛（每批必过）

1. 一个事实只有一个生产计算者和一个写入者；API、readiness、replay 和前端只复用或只读投影。
2. 新实现必须声明替代对象和删除清单；不能回答"删除什么"的新 service、wrapper、adapter、表、线程、调度器、阈值或兼容字段默认不准新增。
3. 涉及开仓和风险事实时，Safety、Readiness、Risk sizing 三层权力不得互相重算：
   - Safety 只负责必须立即禁止新增风险的硬事实；
   - Readiness 只读判断当前事实是否足够；
   - Risk sizing 只负责风险计算和最终仓位。
4. 同一 blocker 只能在一个 owner 中计算一次，其他位置复用稳定 reason code，不再叠加同义门控。
5. canonical 路径验证通过但旧路径未删除，本批仍视为未完成；不以"兼容"为由无限期双轨。
6. 新抽象只有在立即删除重复实现、隔离真实变化源或服务多个真实调用方时才允许；单调用方转发层和假想扩展点直接内联。
7. 每批先跑针对性测试；全量测试只在阶段收口、发布门或改动影响面无法可靠界定时运行（见 §4）。
8. 不以拆文件、增加 schema 或新增状态投影代替架构收敛；验收以生产 authority 数量、调用链和净删除结果为准。

**触发即停**：发现自己正在新增抽象/框架/配置层、为未来预设、叠加同义门控、做多文件无关改动、造第二套实现兼容旧逻辑、借机补全套测试，立即停下重写最小计划。

**失败模式**：不懂意图只修表面；根因能干净修复却上补丁/兼容层/双轨/副本；为小概率场景过度设计；依据错导致结论错；该读代码却靠检索猜测拼结论；以"补测试"为由加抽象扩范围。

## 4. 测试纪律 — 只为当前验收服务

测试不负责补齐历史覆盖率，不负责设计未来测试体系。

1. 优先跑与本次改动相关的现有测试；现有测试能证明正确就不新增。
2. 仅两种情况允许新增测试：a) 改了行为但现有测试盖不到；b) 用户明确要求补测试。
3. 新增测试最多覆盖本次改动的 1 个主路径，必要时加 1 个关键失败路径；只用现有 pytest 设施，不引入新测试框架、工具或目录结构。

**新增测试前必答：**
- 验证哪个已被接受的需求？
- 去掉它，现有测试是否无法发现这次回归？
- 它是否比实现本身更复杂？若测试代码比实现更长更绕，默认视为过度工程，应删测试或缩小实现。

## 5. 文档与环境入口

- 用户说“读一遍文档”或“确认当前项目状态”时：先读 `docs/README.md`，获取阶段、运行姿态、当前主线和文档路由；易变事实（代码、服务、PostgreSQL `runtime`/`canonical_v2`、`runtime_kv`、日志、测试）以现查为准，不引用文档快照。
- 系统级改动（后端、交易、风控、因子、学习、自治治理、RuntimeConfig、数据库、API contract）：依次读 `docs/system-source-of-truth.md`（事实源与权力边界）、`docs/legacy-debt-register.md`（历史残留）、`docs/change-impact-checklist.md`（live/shadow/learning/readiness/frontend contract 和回滚影响），改完同步事实源、旧债、验收矩阵和当前状态。
- 启动、日志、迁移、重启、数据库和 systemd 操作：`docs/server-backend-sop.md`；文档治理规则：`docs/documentation-governance.md`（同一事实只在一处展开，其他文档只链接；历史 planning 和旧代码注释只作背景，不作为实现依据）。
- 平台边界：Linux 服务器负责生产运行验证（后端接口、交易循环、风控、cTrader 执行链路、systemd、数据库、日志、公网 API/WSS）；Windows 只在微信开发者工具联调和浏览器兼容性上补充验证。前端代码（`miniprogram_v2` / `web_frontend`）在 Windows 本地仓库维护，服务器是 `main` 分支的后端-only sparse checkout，不含前端代码和构建产物，也不做前端构建；`https://www.zhuzhu666.icu` 只作 API/WSS 入口，Caddy 反代到本机 `127.0.0.1:8000`。Windows 本地产生的必要修正提交到同一 `main`，不形成长期分叉。
- 数据边界（权威表见 `docs/system-source-of-truth.md` §6）：
  - K 线按月库保存：`data/bars_monthly/bars_YYYY_MM.duckdb`，`data/bars.duckdb` 是当前月兼容链接；`data/ctrader_data.duckdb` 只作旧 K 线冷备/兼容库。
  - 外部研究数据在 `data/external_data.duckdb`（`cot_gold`、`etf_holdings`、`cb_gold`、`macro_daily`、`etf_daily`），保留 `release_at` / `fetched_at` / `source`，因子和回测只能在 `release_at` 之后使用；FRED 用 `QUANT_FRED_API_KEY`，未配置时跳过，不阻塞 COT/ETF/events；原始缓存在 `data/external_raw/`、`data/cot/`、`data/sec_gld/`；旧 `DataStore("data/ctrader_data.duckdb")` 的外部表写入会兼容跳转到该库。
  - 经济事件在 `data/events.duckdb`，由 `execution/event_sizing.py` 直接读取。
  - 这些运行数据不进入 GitHub。
- 模型分工：需求澄清和方案审查用较强模型；写代码、改代码、跑测试用中低配/更轻量执行模型。单线做完再决定是否拆分，不默认并行拉起多个 Agent；只启用任务必需的 skill，不引入重流程 skill。
