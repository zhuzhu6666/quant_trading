# Session 总结 — 2026-06-05 (cTrader 接入阶段 1)

> Owner: zhu + Claude Opus 4.8
> Status: 阶段 1 (PoC) ✅ 通; 阶段 2 (Live Runner) ✅ 写完待 live 验证
> 上一份总结: 2026-06-04 (28 commit, 89 test, 31/48 fixed)

---

## 1. 主要工作

### 1.1 cTrader Open API 接入 — 阶段 1 (PoC) ✅

**目标**: 验证 demo.ctraderapi.com:5035 + Pepperstone demo 账户能否真连。

**踩坑过程** (5 个修复点):

1. **`.env` clientId 大小写错** (肉眼复制) — clientId 第 8 字符大写 → 小写 (portal 字符串原样小写)
2. **OAuth 拿新 access_token** — 老 token 过期,跑 `scripts/ctrader_oauth.py listen-callback` 走完 OAuth 流
3. **`.env` account_id 改用新 demo 账户** — broker 给了 5 个新 ID (masked: `[A1, B2, C3, D4, E5]`,其中 **C3** 是 $1k 标准 demo,**B2** 是 $100k 测试账户。旧 ID 已被 broker 废
4. **bridge bug #1**: `permissionScope` 字段位置错 — `ProtoOACtidTraderAccount` 没这字段,在父 `ProtoOAGetAccountListByAccessTokenRes` 上
5. **bridge bug #2**: `_resolve_symbol_id` 用 `LightSymbol` 拿 `digits/lotSize` 失败 — `LightSymbol` 只有 7 基础字段,完整 metadata 要走二段式: `SymbolsListReq` 找 ID + `SymbolByIdReq` 拿 digits

**PoC 5 步全过** (`scripts/ctrader_poc.py`):
```
[1] connect() ... OK (5.9s)
[2] account_info() ... balance=1000.0
[3] get_positions() ... 0 positions
[5] market_buy (DRY-RUN) ... success=True
```

**Symbol resolve 实测**:
- `XAUUSD` → id=41, digits=2, lot_size=10000(centi-units), min_volume=1.0

### 1.2 cTrader Live Runner — 阶段 2 (broker-simulator) ✅

**目标**: 独立脚本,MT5 拉 bar 喂 strategy → cTrader 真发单 → 记录成交。不集成 paper_engine / event_sizing / pre_trade(用户决定)。

**新增文件**:
- `scripts/ctrader_live_runner.py` (~280 行) — 主程序
- `tests/test_ctrader_live_runner.py` (25 个 test 全过) — 单元测试

**修了 bridge 2 个新 bug**:
- `close_position` (L475-483) STUB → 真发 `ProtoOAClosePositionReq`
- `_send_market_order` SL/TP 注释移除 — 阶段 2 MVP SL/TP **不上 server**(cTrader MARKET 单不支持),本地 Python 层做检查 + close_position

**核心数据流**:
```
MT5Bridge.fetch_bars("M15", 5000)
  → DataFrame
  → 逐 bar 调 strategy.on_bar(bar)
  → Optional[Signal]
  → if signal.direction == 1: bridge.market_buy(...)
  → if signal.direction == -1: bridge.market_sell(...)
  → if signal.direction == 2 (CLOSE): bridge.close_position(...)
  → 落 jsonl: {"ts", "event", "side", "volume", "price", "sl", "tp", "pnl", "exit_reason", ...}
```

**PnL 算式**: `(exit - entry) * volume * 100` (XAUUSD contract_size=100)

**用法**:
```bash
# Dry-run (默认, 不真发)
py scripts/ctrader_live_runner.py --n-bars 100

# 真发 cTrader demo
py scripts/ctrader_live_runner.py --live --n-bars 5000

# 测试
py -m pytest tests/test_ctrader_live_runner.py -v
```

**已知风险** (阶段 2 MVP 接受):
- SL/TP 不上 server,引擎挂了 broker 持仓裸奔
- 走轮询 `get_positions()` 对账(1 bar 延迟)
- 没实现 `ProtoOAExecutionEvent` push handler
- volume 转换基于 cTrader 文档(1 lot = 100 centi-units),真发第一次要看 broker 实际成交

### 1.3 cTrader 文档抓取 (联网测试 + 备料)

**为修 bug 备料抓了 13 个文件 → `data/charts/ctrader_docs/`**:
- 9 个 GitHub raw markdown / SDK Python (OpenApiPy 仓库 main 分支)
- 3 个 help.ctrader.com SPA 抓取 (Playwright 渲染)
- 1 张 viewport 截图
- `INDEX.md` 总结 + 5 个 key finding

**核心抓源修正**: Explore agent 给的 7 个 URL 全 404(分支错 `master` → `main`,文件名错),我用 GitHub API 探测后重抓。

**意外收获**:
- cTrader `ProtoMessage.clientMsgId` 字段是 wire 协议字段(不是 callback key)
- 官方 sample 不传 `clientMsgId`,SDK 默认 `str(id(deferred))`(我们测过会被拒,server 误报)
- `MARKET` 单**不支持** SL/TP 字段(必须 `MARKET_RANGE` 或 `AmendOrderReq`)

### 1.4 event_sizing 接入 paper_engine (P19/P24 跨阶段集成)

**新增**:
- `execution/event_sizing.py` (331 行) — 事件感知仓位
- `tests/test_event_sizing.py` (314 行) — 多场景测试

**接入**:
- `execution/paper_engine.py` 加 `event_sizing` 字段,SL/TP 计算后乘 `event_mult`
- `execution/mab_paper_runner.py` 透传 `event_sizing` 给 `PaperTrader`
- `main.py` 加 `--use-event-sizing` / `--no-event-sizing` flag

**配置** (`config/settings.yaml`):
```yaml
event_sizing:
  enabled: true
  db_path: data/market_data.db
  event_times: {FOMC: "19:00", NFP: "13:30", CPI: "13:30", PCE: "13:30"}
  tiers:
    3:  # HIGH: FOMC, NFP, CPI
      - {max_hours_before: 4,  multiplier: 0.2}
      - {max_hours_before: 24, multiplier: 0.5}
      - {max_hours_before: 72, multiplier: 0.8}
    2:  # MEDIUM: PCE
      - {max_hours_before: 4,  multiplier: 0.5}
      - {max_hours_before: 24, multiplier: 0.8}
```

### 1.5 文档更新

- `docs/CTRADER_INTEGRATION.md` — 阶段路线重写(阶段 2 改成 paper 接入,不走 cTrader 行情)
- `docs/REALTIME_PAPER_DESIGN.md` — 2026-06-03 设计稿保留
- `config/settings.yaml` — 加 ctrader + event_sizing 配置块
- `requirements.txt` — 加 `ctrader-open-api>=0.9` + `service_identity>=18.0`

---

## 2. 修复清单 (本次 commit 范围)

### 新增文件
- `data/charts/ctrader_docs/` (13 个文件,含 INDEX.md)
- `docs/CTRADER_INTEGRATION.md`
- `docs/REALTIME_PAPER_DESIGN.md`
- `execution/_env.py` — 自动从 .env 加载 CTRADER_* 凭证
- `execution/ctrader_bridge.py` — cTrader Open API 桥接
- `execution/event_sizing.py` — 事件感知仓位
- `scripts/ctrader_poc.py` — 端到端 PoC
- `scripts/ctrader_oauth.py` — OAuth 流程工具
- `scripts/ctrader_live_runner.py` — Live broker-simulator
- `scripts/fetch_events_calendar.py` — events 表拉取
- `scripts/probe_ctrader_hosts.py` — 主机连通性探测
- `scripts/validate_ctrader_token.py` — token 死活诊断
- `tests/test_ctrader_live_runner.py` (25 个 test)
- `tests/test_event_sizing.py`

### 修改文件
- `.gitignore` — 加 `.env` 防泄凭证
- `config/settings.yaml` — ctrader 块 + event_sizing 块 + kelly sizing + max_position_lots
- `data/news_cache.py` — 加 `load_events_with_importance` 给 EventSizing
- `execution/mab_paper_runner.py` — 透传 `event_sizing`
- `execution/paper_engine.py` — 加 `event_sizing` 字段 + mult 应用
- `execution/paper_trader.py` — 配套
- `main.py` — 加 `--use-event-sizing` / `--no-event-sizing` flag + 注入到 paper engine
- `requirements.txt` — `ctrader-open-api>=0.9` + `service_identity>=18.0`
- `scripts/daily_paper_dryrun.py` — 配套 event_sizing

### 不 commit (运行时 / 缓存 / 无关)
- `.claude-setup/`, `.playwright-mcp/`, `C...txt` — Claude/Playwright 缓存
- `data/charts/ctrader_poc_report.txt` — 含凭证痕迹(masked 但保守)
- `data/charts/framework_audit_20260604.md` — 2026-06-04 旧文件,不相关
- `data/charts/factor_lifecycle_log.jsonl`, `live_sync_status.json` — 运行时状态

---

## 3. 测试状态

- `tests/test_ctrader_live_runner.py` — **25/25 全过** ✅
- `tests/test_event_sizing.py` — 现有(上一轮已通,本次没动)
- 阶段 1 PoC (`scripts/ctrader_poc.py`) — 手动跑过,5 步全过
- 阶段 2 Live Runner — **没跑过 live 端到端**(等用户跑 `--live` 验证)

---

## 4. 下一步 (下次会话)

1. **跑 live 验证**: `py scripts/ctrader_live_runner.py --live --n-bars 100` 看 cTrader 当前 demo 账户真成交 + jsonl 记录对账
2. **PnL 对账**: 比对 jsonl pnl 跟 broker `account_info().balance` 差
3. **阶段 3 预备**:
   - 实现 `ProtoOAAmendPositionSLTPReq` 把 SL/TP 推 server
   - 实现 `ProtoOAExecutionEvent` push handler 替代轮询
   - `MARKET_RANGE` 订单类型支持 SL/TP
4. **OPEN issues**:
   - `account_info()` 返 `equity=None / leverage=0` — 缺 `GetPositionUnrealizedPnLReq`
   - `get_positions()` 返 `profit=None` — 同上
   - `lot_size=10000` (centi-units) 跟 PoC 注释 "XAUUSD=100 oz" 不一致 — 跑 live 时第一次开仓看 broker 返回确认

---

## 5. 教训(下次记得)

1. **复制凭证一定要脚本对比**,不能肉眼看(0/O、I/1/l 视觉混淆)
2. **cTrader `wrong random id` 是误导性错误** — 实际几乎是凭证错,server 不区分 clientId / secret / token / clientMsgId 错
3. **PROTO OA MARKET 单不支持 SL/TP** — 必须 `MARKET_RANGE` 或 `AmendOrderReq`,文档没一眼显眼
4. **GitHub 文档真源是 `main` 分支**(不是 `master`),文件名要 GitHub API 探测
5. **cTrader PoC 后,bridge 字段名要 dump 实际 `_pb2.py`**,文档与 SDK 编译版本可能不一致(`permissionScope` 位置就是反例)
6. **任何凭证 / token 痕迹不进 git** — `.gitignore` 加 `.env`, 报告 masked 后也别 commit
