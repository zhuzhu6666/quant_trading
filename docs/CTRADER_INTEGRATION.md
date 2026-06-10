# cTrader 接入设计文档 (并行 MT5)

> 2026-06-04 起, 增量接入
> 目标: 多 broker 互证 (MT5 主 + cTrader 影子), 不替换

---

## 1. 目标 / 非目标

**目标**:
- cTrader Open API 模拟盘接入 (Pepperstone demo, account 见 .env `CTRADER_ACCOUNT_ID`)
- 跟 MT5Bridge 形态对齐 (connect / market_buy / get_positions / fetch_bars)
- 4 阶段推进: PoC → 行情 → paper 接入 → bar 同步

**非目标** (本次不做):
- 替换 MT5 路径
- cTrader 实盘交易 (要等 demo 跑通 + 2-3 月稳定)
- cTrader 交易算法 (TWAP/VWAP 之类, 用 broker algo 不自己写)

---

## 2. 阶段路线

| 阶段 | 状态 | 目标 | 关键文件 |
|---|---|---|---|
| **1. PoC** | ✅ 已通(2026-06-05) | API 连 + App auth + Account auth + Symbol resolve + DRY-RUN market_buy | `execution/ctrader_bridge.py` + `scripts/ctrader_poc.py` |
| **2. paper 接入** | 🔄 下一步 | MT5 拉 bar 喂 paper_engine, **撮合走 cTrader**(paper 用 cTrader 价格撮合, 不真发单) | `main.py --mode paper --bridge ctrader` + `execution/paper_engine.py` |
| **3. 实盘接入(隔离)** | ⏳ | cTrader 真下单路径(走 `send_orders=True`),**双 broker 互为 fallback** | `execution/ctrader_bridge.py:send_orders` 开关 |
| **4. 影子 A/B** | ⏳ | MT5 vs cTrader PnL 互证(同策略双 broker 跑) | `scripts/p_ctrader_shadow.py` |

**路线调整(2026-06-05)**:
- ❌ ~~阶段 2 拉 cTrader bar 落 db~~ — MT5 demo 行情更稳,单源够
- ✅ cTrader 定位:**只做 broker**(exec + account mgmt),**行情/历史 bar 仍走 MT5**
- 双 broker 职责清晰:
  - MT5 = 行情源(spot/bar/history)
  - cTrader = 交易对手(账户、订单、持仓)
- 撮合层 `paper_engine` 把 MT5 bar 喂进去,撮合用 cTrader bid/ask 价格(防止回测用未来价)
- 阶段 3 之前 `send_orders=False`,撮合层模拟成交(DRY-RUN),等阶段 3 切真发

阶段 1 必修(实测发现 3 个 bug, 已修):
1. `.env` `CTRADER_CLIENT_ID` 第 8 字符大写 `R` → 小写 `r`(肉眼复制错)
2. `permissionScope` 字段位置:从 `ProtoOACtidTraderAccount` 移到父 `ProtoOAGetAccountListByAccessTokenRes`
3. `_resolve_symbol_id` 二段式:`SymbolsList` 拿 ID + `SymbolByIdReq` 拿 `digits/lotSize`(LightSymbol 没 metadata)

凭证/账户记录(具体值见 .env, 不在此处固化):
- clientId: portal 拿的 Pepperstone demo app (Active, sandbox)
- access_token: 走 OAuth 流刷(旧 token 过期, 跑 `scripts/ctrader_oauth.py listen-callback`)
- demo 账户 ID: 见 .env `CTRADER_ACCOUNT_ID` ($1k 起始, broker 给的 5 个新 ID 里最合适)
- 旧 account_id broker 已废, 别再用

---

## 3. 架构 (跟 MT5Bridge 对齐)

```
┌─────────────┐         ┌────────────────────┐         ┌──────────────┐
│ main.py     │  ────►  │  CTraderBridge     │  ────►  │ demo.ctrader │
│ --bridge    │         │  (Twisted async,   │  proto  │ api.com:5035 │
│ ctrader     │         │   Deferred→同步)   │         └──────────────┘
└─────────────┘         └────────────────────┘
                              │
                              │ 同样形态: market_buy / get_positions /
                              │ fetch_bars / account_info
                              ▼
                        ┌──────────────┐
                        │ MT5Bridge    │  (并行, 不替换)
                        └──────────────┘
```

**设计原则**:
- 形态对齐 → 未来抽 `BaseBridge` 抽象层 (3/4 阶段)
- 异步→同步转换在 `CTraderBridge._send()` 内部, 主线程调用方无感
- 安全闸 `send_orders=False` 默认禁真单, PoC 必须显式 `--live`

---

## 4. 凭证 / 配置

### 4.1 必需环境变量 (.env / shell, 不入 git)

```bash
# 应用凭证 (注册 application 时拿, 长期有效)
CTRADER_CLIENT_ID=<from_portal>
CTRADER_CLIENT_SECRET=<from_portal>
CTRADER_REDIRECT_URI=http://127.0.0.1:8080/callback    # 必须跟 app 注册时一致

# 账户凭证 (OAuth 拿, 30 天左右过期, scripts/ctrader_oauth.py 自动续)
CTRADER_ACCESS_TOKEN=...        # listen-callback 后落到 .env
CTRADER_REFRESH_TOKEN=...       # 同上
CTRADER_ACCOUNT_ID=<from_broker_demo>
```

> 鉴权流程(2026-06-04 确认, 从 SDK 源码反推):
> 1. 浏览器跳 `https://openapi.ctrader.com/apps/auth?client_id=...&redirect_uri=...&scope=trading`
> 2. 登 cTrader ID → 授权 application → 跳回 `redirect_uri?code=<authCode>`
> 3. POST `https://openapi.ctrader.com/apps/token?grant_type=authorization_code&code=...` → access_token + refresh_token
> 4. ProtoOAApplicationAuthReq(clientId, clientSecret) + ProtoOAAccountAuthReq(ctidTraderAccountId, accessToken)
> 5. access_token 过期 → 走 grant_type=refresh_token 续期
> 6. refresh_token 也过期 → 重新走 1-3

> Pepperstone demo 账户是 ctidTraderAccountId, accessToken 是注册 application 的 cTrader ID 账户 OAuth 出来的,
> 但 scope 选 trading + 选对应 demo 账户, token 就能操作那个 demo 账户.

**凭证安全**: `.env` 已在 `.gitignore` (2026-06-04 加), 永不 commit.

### 4.2 config/settings.yaml (新增段, 2026-06-04)

```yaml
ctrader:
  client_id_env: CTRADER_CLIENT_ID
  client_secret_env: CTRADER_CLIENT_SECRET
  access_token_env: CTRADER_ACCESS_TOKEN
  account_id_env: CTRADER_ACCOUNT_ID
  host: demo.ctraderapi.com    # demo=模拟, live 实盘换 broker host
  port: 5035
  symbol: XAUUSD               # Pepperstone 是 XAUUSD (无 +)
  rate_limit_per_sec: 5
  request_timeout_sec: 10
  send_orders: false           # PoC 安全闸
```

### 4.3 关键差异 (vs MT5)

| 项 | MT5 | cTrader |
|---|---|---|
| 协议 | Win32 IPC pipe (Blocking) | TCP + TLS (Twisted async) |
| 凭证 | login + password + server | clientId/secret + accessToken + accountId |
| 反应器 | 同步 + polling | Twisted reactor + Deferred |
| 品种名 | XAUUSD+ (suffix) | XAUUSD (无 +) |
| Volume 单位 | 0.01 lot = 1 oz | 0.01 lot = 1 oz (1 lot=100 centi) |
| 余额单位 | 1.0 USD | 1.0 USD (broker 内部 ÷100) |
| Filling mode | FOK/IOC/RETURN 探测 | broker 决定, 单选 |

---

## 5. 文件清单

| 文件 | 状态 | 用途 |
|---|---|---|
| `execution/ctrader_bridge.py` | ✅ 新建 (2026-06-04) | 桥接主类, 跟 MT5Bridge 形态对齐 |
| `scripts/ctrader_poc.py` | ✅ 新建 (2026-06-04) | PoC 入口, 5 步验证 |
| `scripts/ctrader_oauth.py` | ✅ 新建 (2026-06-04) | OAuth 完整流程: print-auth-url / listen-callback / exchange / refresh |
| `config/settings.yaml` | ✅ 加 `ctrader:` 段 | 静态配置 |
| `requirements.txt` | ✅ 加 ctrader-open-api + service_identity | 依赖 |
| `.gitignore` | ✅ 加 `.env` | 凭证不入 git |
| `docs/CTRADER_INTEGRATION.md` | ✅ 本文件 | 设计文档 |
| `data/charts/ctrader_poc_report.txt` | ⏳ PoC 跑后落 | 验证报告 |

---

## 6. PoC 阶段验证清单

- [ ] 1. App auth 成功 (`clientMsgId` 回包)
- [ ] 2. Account auth 成功 (`accountId=见 .env CTRADER_ACCOUNT_ID` Pepperstone demo)
- [ ] 3. `account_info()` 拿回 balance / leverage / currency
- [ ] 4. `get_positions()` 返回空 (新 demo 账户)
- [ ] 5. `fetch_bars(M15, 100)` 拿到 100 根 K 线, last close ≈ 4512 USD
- [ ] 6. `market_buy(0.01, DRY-RUN)` 返回 success=True, comment="DRY-RUN"
- [ ] 7. `--live` 真单: orderId 真实返回 (谨慎, 先 0.01 lot)
- [ ] 8. disconnect() 无 leak (Twisted reactor 停掉)

---

## 7. 风险 / 阻塞

| 风险 | 应对 |
|---|---|
| Open API proto 复杂度 (60+ message 类型) | 只 import 用到的, 不全 import; 跟 SDK 0.9.x 锁版本 |
| Twisted reactor 跟主程序 asyncio 冲突 | reactor 跑在 daemon 线程, 主线程不直接进 reactor |
| Pepperstone demo 限速 (5 msg/s) | `rate_limit_per_sec=5` SDK 自带 |
| `accessToken` 过期 (broker 给 24h-30d) | env 设; 过期只重连失败, 不崩 |
| `send_orders` 误开真单 | 双重闸: env 缺失 → 报 env 错; send_orders=False → 早 return |
| SDK 0.9.x 跟 proto 不匹配 | 锁版本; 升级前跑回归 |

---

## 8. 后续阶段详细

### 阶段 2: 行情层 (T16 替代方案)
- `data/live_sync/ctrader_puller.py`: 拉 trendbar 增量 → 写 `bars` 表
- 跟 MT5 live_sync 共享 db_inserter, 减重复
- 解 T16 IPC pipe hash 不匹配的阻塞

### 阶段 3: paper 接入
- `main.py` 加 `--bridge ctrader` 选项
- 把 cTraderBridge 的 fetch_bars / get_positions 接入 paper_engine
- 验证 cTrader 价格下 5000 bar PnL, 跟 MT5 数据集对比 (< 0.5% 差异)

### 阶段 4: 影子 A/B
- `scripts/p_ctrader_shadow.py`: dual-bridge, MT5 主 + cTrader 影子
- 复刻 P1-D 思路: 同一信号, 两个 broker 执行, PnL 差异 < $50
- 这块做完才算真"互证"

### SL/TP 上 server (✅ 2026-06-10 shipped)
cTrader Open API 的 MARKET 单不支持在 `market_buy`/`market_sell` 协议字段里传
SL/TP, 之前 SL/TP 只能靠本地 Python 在下一根 bar 的 high/low 上检查
(1 bar 延迟). 现在改成: market 成交后立即 `bridge.amend_position_sltp()`
推 server, server 端 0 延迟执行. 实现位置:
`backend/services/live_service.py::_process_tick`（实现位于 line 849-975）在 `market_buy`/`market_sell`
fill 之后 try/except 调 amend, 成功调 `_track_local_sl_tp(position_id, sl, tp)`
把 SL/TP 镜像写到模块级 `_local_positions: dict[int, _LocalSLTP]` 里(给下次
amend 失败时 reconciliation 用). amend 失败 / 异常都不崩, 下根 tick 重试.
测试: `tests/test_live_service_tick.py` (9 tests, 覆盖 LONG/SHORT amend、
envelope shim、amend-failure-no-update、amend-exception-no-crash、dry-run).

---

## 9. 参考

- cTrader Open API docs: openapi.ctrader.com (注册 + proto 文档)
- ctrader-open-api PyPI: 0.9.2 (Twisted-based, 跟项目栈一致)
- Twisted reactor 线程模型: `Factory.forProtocol` + `clientFromString("ssl:host:port")`
- 类似 P1-D 影子 A/B 设计: `scripts/p1_d_shadow.py`
