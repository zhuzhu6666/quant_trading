# cTrader 接入说明

> 最后清理: 2026-06-29
> 当前状态: cTrader demo 是唯一执行通道；L2 研究采集并入主连接，历史 MT5 并行路线已归档。

---

## 当前定位

cTrader Open API 负责当前 demo 交易闭环:

- 账户连接与鉴权
- 持仓查询
- market buy / sell
- SL/TP 修改
- 平仓
- 成交同步到 PostgreSQL `state_v1.ctrader_deals`

主要代码:

| 文件 | 用途 |
|---|---|
| `execution/ctrader_bridge.py` | cTrader Open API 桥接 |
| `execution/deal_sync.py` | 从 cTrader 同步成交，用于真实 PnL 和学习闭环 |
| `backend/services/live_service.py` | 实盘 loop，平仓检测，归因和学习触发 |
| `scripts/validate_ctrader_token.py` | Token 有效性验证 |
| `scripts/backfill_ctrader_deals.py` | 历史成交回填 |

2026-06-26 新确认:

- 当前实盘主链只需要 cTrader 的 `spot / account / positions / execution` 即可运行
- 第二数据源当前不再承担“实盘备份源”职责，只保留给未来订单流分析和研究特征
- `tick 缺少高低点和成交量，所以必须依赖另一源才能跑 live` 这个判断不再成立；它只适用于后续研究扩展
- `RuntimeConfig.risk_require_l2_depth=false` 只表示开仓/风控不依赖 L2；`l2_collection_enabled=true` 时仍会在同一条 cTrader 主连接上订阅 depth，作为研究数据异步落库

2026-06-29 新确认:

- cTrader Open API 同一账号类型不再保留第二条 L2 专用连接；当前目标是 `quant-backend.service` 内单 bridge 连接同时承担 spot / account / positions / execution / depth
- `quant-l2-collector.service` / `scripts/run_l2_collector.py` 是历史独立采集方案，当前已移除，不应恢复为默认方案
- L2 写入不再在 depth 回调里逐条写 DuckDB；回调只更新内存簿并入队，后台 writer 批量写 `data/l2.duckdb`
- L2 库使用月库：`data/l2_monthly/l2_YYYY_MM.duckdb`，`data/l2.duckdb` 只作为当前月份兼容链接，并由 writer 跨月自动刷新

---

## 凭证 / 配置

凭证写入 `.env`，不要提交到 git:

```env
CTRADER_CLIENT_ID=...
CTRADER_CLIENT_SECRET=...
CTRADER_ACCESS_TOKEN=...
CTRADER_REFRESH_TOKEN=...
CTRADER_ACCOUNT_ID=...
CTRADER_REDIRECT_URI=http://127.0.0.1:8080/callback
```

验证 token:

```bash
python scripts/validate_ctrader_token.py
```

回填近期成交:

```bash
python scripts/backfill_ctrader_deals.py --days 30
```

---

## 当前数据流

```text
Factor Takeover v4 pipeline
  -> ExecutionGate
  -> live_service
  -> CTraderBridge
  -> cTrader demo account
  -> deal_sync
  -> ctrader_deals
  -> AttributionEngine / recovery_position_state
  -> trade review / experience / supervisor counterfactual
  -> policy_suggestion / governed template switch
```

平仓后的真实 PnL 来自 cTrader deal，同步内容包含 gross / swap / commission / net。归因层和学习闭环应优先使用这条真实成交链路。

2026-06-29 起，归因和退出学习的现网口径为：

- 开仓时把可恢复的 `TradeAttribution` 写入 `recovery_position_state.recovery_meta_json.trade_attribution`
- 服务重启后，`AttributionEngine.restore_open()` 会从 recovery state 恢复仍然活跃仓位的归因上下文
- 平仓 review 会标记 `attribution_integrity=full/recovered/missing`
- `broker_close` 会回溯最近 supervisor verdict，写入 `close_reason_source` 和 `inferred_close_supervisor`
- `supervisor_learning_scheduler` 会定时物化 `supervisor_counterfactual_review`，并生成 supervisor 模板治理建议

当前实盘可以简化理解为:

```text
cTrader spot / account / positions
  -> live_service
  -> RiskPolicyService / position_supervisor
  -> cTrader execution
  -> ledger / trace / review
```

说明:

- 当前主链默认依赖 `spot`，不依赖 `L2 depth`
- `M1 / M5 / M15 / M30 / H1 / H4 / D1` K 线仍然是基础数据资产，要继续同步更新入库
- `L2 depth` 当前作为研究/订单流支路采集，不能成为默认开仓门槛，除非显式打开 `risk_require_l2_depth=true`

L2 研究采集:

```text
cTrader main bridge
  -> subscribe_spots / subscribe_depth
  -> in-memory spot + order book
  -> async L2 writer queue
  -> data/l2.duckdb -> data/l2_monthly/l2_YYYY_MM.duckdb
```

writer 每次批量写入前会按事件时间选择目标月库；如果跨月，会关闭旧月库连接、打开新月库，并把 `data/l2.duckdb` symlink 刷到新月份。

---

## 近期踩坑记录

### CPU 满载并不一定是“交易循环本身太频繁”

2026-06-26 的线上高 CPU 问题，最终确认是三类开销叠加:

- depth 事件高频日志
- depth 事件逐条写 `DuckDB`
- 学习治理接口重复重算 `factor_cards / parameter_templates`

因此排查 cTrader 问题时，不能只盯 `live_loop` tick 频率，也要看:

- `journalctl` 里是否在刷 `depth event` / `depth events (5s)`
- `py-spy dump` 是否卡在 `factor_cards.py` / `parameter_templates.py`
- depth 是否又被改回回调内同步写库，或是否有人恢复了历史独立 L2 collector

### `warming_up` 不一定代表真的断线

之前单次 broker 慢请求会把连接状态打回 `warming_up`，导致前端误以为 broker 断开。现在桥接层已改成:

- 账户 / 持仓优先走事件缓存
- soft timeout 不直接标记整条 cTrader 连接断开
- live 状态尽量依据 bridge cache，而不是每次都同步阻塞请求

但服务重启后的首次鉴权仍可能偶发超时，现阶段属于“可自动恢复，但仍需继续观察”的运行项。

---

## 历史说明

早期文档曾描述 “MT5=数据源、cTrader=执行通道” 的并行方案。该方案已经过时；当前项目约定是:

- cTrader 是唯一执行通道。
- MT5 不再是当前主链路要求。
- 涉及 MT5 的历史描述只能作为迁移背景，不能作为开发依据。

如需恢复或重做多 broker 抽象，应先重新审计 `execution/base.py`、`execution/ctrader_bridge.py` 和 `TODO.md` 里的多品种/价格换算技术债。
