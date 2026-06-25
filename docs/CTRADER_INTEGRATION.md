# cTrader 接入说明

> 最后清理: 2026-06-25
> 当前状态: cTrader demo 是唯一执行通道；历史 MT5 并行路线已归档。

---

## 当前定位

cTrader Open API 负责当前 demo 交易闭环:

- 账户连接与鉴权
- 持仓查询
- market buy / sell
- SL/TP 修改
- 平仓
- 成交同步到 `data/state.db` 的 `ctrader_deals`

主要代码:

| 文件 | 用途 |
|---|---|
| `execution/ctrader_bridge.py` | cTrader Open API 桥接 |
| `execution/deal_sync.py` | 从 cTrader 同步成交，用于真实 PnL 和学习闭环 |
| `backend/services/live_service.py` | 实盘 loop，平仓检测，归因和学习触发 |
| `scripts/validate_ctrader_token.py` | Token 有效性验证 |
| `scripts/backfill_ctrader_deals.py` | 历史成交回填 |

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
  -> AttributionEngine / learning backfill
```

平仓后的真实 PnL 来自 cTrader deal，同步内容包含 gross / swap / commission / net。归因层和学习闭环应优先使用这条真实成交链路。

---

## 历史说明

早期文档曾描述 “MT5=数据源、cTrader=执行通道” 的并行方案。该方案已经过时；当前项目约定是:

- cTrader 是唯一执行通道。
- MT5 不再是当前主链路要求。
- 涉及 MT5 的历史描述只能作为迁移背景，不能作为开发依据。

如需恢复或重做多 broker 抽象，应先重新审计 `execution/base.py`、`execution/ctrader_bridge.py` 和 `TODO.md` 里的多品种/价格换算技术债。
