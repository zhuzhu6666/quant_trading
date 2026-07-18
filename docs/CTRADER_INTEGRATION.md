# cTrader 接入说明

> Status: active
> Last verified: 2026-07-06
> Scope: cTrader demo execution channel, deal sync.
> 当前状态: cTrader demo 是唯一执行通道，历史 MT5 并行路线已归档。

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
| `backend/services/broker_execution_intent.py` | 开仓 mutation intent、unknown 计数与重启恢复门闩 |
| `execution/deal_sync.py` | 从 cTrader 同步成交，用于真实 PnL 和学习闭环 |
| `backend/services/live_service.py` | 实盘 loop，平仓检测，归因和学习触发 |
| `scripts/validate_ctrader_token.py` | Token 有效性验证 |
| `scripts/backfill_ctrader_deals.py` | 历史成交回填 |

2026-06-26 新确认:

- 当前实盘主链只需要 cTrader 的 `spot / account / positions / execution` 即可运行
- 第二数据源当前不再承担“实盘备份源”职责，只保留给未来订单流分析和研究特征
- `tick 缺少高低点和成交量，所以必须依赖另一源才能跑 live` 这个判断不再成立；它只适用于后续研究扩展

2026-06-29 新确认:


---

## 凭证 / 配置

凭证写入 `.env`，不要提交到 git:

```env
QUANT_JWT_SECRET=...
QUANT_AUTH_USER=...
QUANT_PASSWORD_HASH=...
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

## 执行结果与显式对账契约

- `reconcile_positions()` / `reconcile_account()` 返回不可变结果，状态严格为 `fresh/cache/event/failed`。只有带非空 reconcile ID 和 broker `observed_at` 的 `fresh` 表示本次 RPC 得到的 broker 全量事实；fresh 空 tuple 才是确认空仓，failed 空 tuple 不是空仓。
- position reconcile 的 authoritative 范围只包括 identity、volume、SL/TP。current price 只接受 15 秒内 cTrader spot，PnL 只接受独立 broker PnL RPC；entry price、账户 equity 差额和默认零值都不能填补未知 current price/PnL。组件状态随 `PositionReconcileResult` 和 API `_fact` 发布。
- `refresh_positions()` / `refresh_account_info()` 继续返回旧 list/dataclass，仅用于兼容展示；startup、safety、emergency 和 execution recovery 必须消费显式 reconcile 结果。
- cTrader mutation 结果严格为 `confirmed/rejected/unknown/simulated`；`success=true` 只对应 confirmed/simulated。accepted、timeout、未知 protobuf 或无法唯一关联 position 都是 unknown。
- 启用发布期开关 `ctrader_execution_outcome_v2_enabled` 后，market RPC 前在 PostgreSQL `broker_execution_intent` 依次提交 prepared、submitting；请求携带 UUID `clientOrderId`、UUID `clientMsgId` 和 comment token。结果只允许由 execution response 与 order/deal/position 差分唯一确认。
- 任一 prepared/submitting/unknown intent 都会按 broker account + symbol 阻断下一次开仓；恢复只能重新拉取 broker positions/deals 并解析旧 intent，绝不重发旧订单。

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

- `M1 / M5 / M15 / M30 / H1 / H4 / D1` K 线仍然是基础数据资产，要继续同步更新入库


---

## 近期踩坑记录

### CPU 满载并不一定是“交易循环本身太频繁”

2026-06-26 的线上高 CPU 问题，最终确认是三类开销叠加:

- depth 事件高频日志
- depth 事件逐条写 `DuckDB`
- 学习治理接口重复重算 `factor_cards / parameter_templates`

因此排查 cTrader 问题时，不能只盯 `live_loop` tick 频率，也要看:

- `journalctl` 里是否在刷 `depth event` / `depth events`
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

如需恢复或重做多 broker 抽象，应先重新审计 `execution/base.py`、`execution/ctrader_bridge.py` 和 [legacy-debt-register.md](legacy-debt-register.md) 里的多品种/价格换算技术债。
