# cTrader 接入设计文档 (并行 MT5)

> 最后更新: 2026-06-11 (全流测试通过)
> 状态: Phase 1-3 全完成 — 连通→开仓→SLTP→平仓 全链验证通过

---

## 1. 目标 / 非目标

**目标**:
- cTrader Open API 模拟盘接入 (Pepperstone demo, account=47276606)
- 跟 MT5Bridge 形态对齐 (connect / market_buy / get_positions / fetch_bars / amend_position_sltp / close_position)
- 双 broker 分工: MT5=数据源, cTrader=执行通道

**非目标**:
- 替换 MT5 数据源路径
- cTrader 实盘交易 (demo 够用, 等跑稳)

---

## 2. 路线完成情况

| 阶段 | 状态 | 完成内容 |
|---|---|---|
| **1. PoC 连通** | ✅ 2026-06-05 | App auth + Account auth + Symbol resolve + DRY-RUN |
| **2. 开平仓** | ✅ 2026-06-11 | market_buy/market_sell + close_position + amend_position_sltp |
| **3. SL/TP server 端** | ✅ 2026-06-11 | amend_position_sltp 推 SL/TP 到 server, 0 延迟执行 |
| **4. 全流测试** | ✅ 2026-06-11 | 10 步: connect→close旧→open→amend→close→verify |
| 5. 影子 A/B 互证 | ⏳ 下一阶段 | MT5 vs cTrader 同信号双执行 |

---

## 3. 实测发现 (2026-06-11)

### 3.1 关键 Protobuf 结构

| 字段 | 实际位置 | 坑 |
|---|---|---|
| ProtoOAPosition.symbolId | **`tradeData.symbolId`** (嵌套消息) | 不在顶层, `p.symbolId` 报错 |
| ProtoOAPosition.price | float (type=1=double), 已是真实价格 | **不能除 moneyDigits** (否则 4089→40.9) |
| NewOrderReq 响应 | **`ProtoOAExecutionEvent`** (含 position/order) | 不存在 NewOrderRes |
| ClosePositionReq.volume | **required** (4 字段全 required) | 不设 volume 导致 protobuf encode error |
| AmendPositionSLTP 拒绝 | **`ProtoOAOrderErrorEvent`** | 不是 ProtoOAErrorRes |

### 3.2 5 个已修复的 Bug

| Bug | 文件 | 症状 | 修复 |
|---|---|---|---|
| symbolId 字段位置 | ctrader_bridge.py:771 | get_positions 报 "symbolId" | `p.symbolId` → `td.symbolId` |
| price 错误缩放 | ctrader_bridge.py:781 | 价格 4089→40.9 | 去掉 `/ 10**moneyDigits` |
| close 缺 volume | ctrader_bridge.py:659 | ProtoOAClosePositionReq encode error | volume=None 时 get_positions 自动查 |
| market_buy 解析错 | ctrader_bridge.py:622 | order_id=0 无意义 | 改成解析 ProtoOAExecutionEvent |
| amend 忽略 OrderError | ctrader_bridge.py:575 | 失败当成功 | 检查 resp 类型 + 提取 errorCode |

### 3.3 全流测试结果

```
connect()             ✅ 6.1s (TCP+App+Account auth)
account_info()        ✅ balance=1000.0 JPY
get_positions()       ✅ position_id=265499770, entry=4089.19
amend sl=4084.19 tp=4092.19  ✅ SL/TP 实际生效
close_position()      ✅ 仓位已平 (全流程闭环)
```

---

## 4. 凭证 / 配置

```bash
# .env (不入 git)
CTRADER_CLIENT_ID=27394_xxx
CTRADER_CLIENT_SECRET=xxx
CTRADER_ACCESS_TOKEN=xxx          # OAuth 拿, 可续期
CTRADER_REFRESH_TOKEN=xxx
CTRADER_ACCOUNT_ID=47276606       # Pepperstone demo JPY
CTRADER_REDIRECT_URI=http://127.0.0.1:8080/callback
```

**OAuth 流程**: `python scripts/ctrader_oauth.py listen-callback`
**Token 验证**: `python scripts/validate_ctrader_token.py`
**全流测试**: `python scripts/test_ctrader_full_flow.py`

---

## 5. 架构

```
┌──────────────┐    Proto TCP     ┌────────────────────┐
│ 策略信号      │ ──────────────►  │  CTraderBridge      │
│ (M15 tick)   │                  │  (Twisted reactor)  │
└──────────────┘                  └────────┬───────────┘
                                           │ market_buy / market_sell
                                           │ amend_position_sltp
                                           │ close_position
                                           ▼
                                    ┌──────────────┐
                                    │ demo.ctrader  │
                                    │ api.com:5035  │
                                    └──────────────┘
```

- MT5 = 行情源 (K线/历史 bar) — `execution/mt5_bridge.py`
- cTrader = 交易对手 (开平仓/SLTP/账户) — `execution/ctrader_bridge.py`
- **不要混淆**: MT5 不做交易, cTrader 不拉数据

---

## 6. 文件清单

| 文件 | 用途 |
|---|---|
| `execution/ctrader_bridge.py` | 桥接主类 (929 行, 5 bug 已修) |
| `scripts/ctrader_poc.py` | PoC 入口 (5 步验证) |
| `scripts/ctrader_oauth.py` | OAuth 完整流程 |
| `scripts/test_ctrader_full_flow.py` | ★ 新: 全流回归测试 |
| `scripts/validate_ctrader_token.py` | Token 有效性验证 |
| `scripts/probe_ctrader_hosts.py` | Host 连通探测 |
| `docs/CTRADER_INTEGRATION.md` | 本文件 |

---

## 7. 风险 / 限制

| 风险 | 状态 |
|---|---|
| access_token 过期 (30d) | 可 refresh_token 续, 过期只重连失败 |
| XAUUSD min_volume=1.0 (bridge unit) | 最小 1 单位 = 约 35 JPY 保证金 |
| account=JPY 非 USD | balance 1000 JPY ≈ $6.5, 只能极小仓 |
| send_orders 安全闸 | 默认 False, `--live` 才真发 |
| reactor 停止时资源泄漏 | disconnect() 停 client 但保留 reactor (可复用) |

---

## 8. 参考

- cTrader Open API: openapi.ctrader.com
- ctrader-open-api PyPI: 0.9.2
- SDK 结构: Client.send() → ProtoMessage wrapper → Protobuf.extract()
