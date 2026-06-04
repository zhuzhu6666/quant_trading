# Realtime Paper + cTrader + Learning Loop Design

> 2026-06-03  design draft
> Owner: zhu
> Status: **proposal**, awaiting W0 go-ahead

This document describes the bridge between (a) the K-line signal pipeline that
already lives in this repo and (b) the cTrader DEMO account that the user will
connect. The goal is a closed loop: live K-line -> strategy signal -> cTrader
DEMO fill -> recorded outcome -> trained scorer that adjusts the next live run.

The shape of the loop is **not** a research artifact. It is the production path
for everything that already runs as paper replay today.

---

## 1. Scope and Non-Goals

### In scope
- Real-time K-line driven signal generation on top of `data/live_sync/daemon.py`
- Submitting orders to a **cTrader DEMO** account, never live
- Recording every open/close/PnL/commission/slippage into a learning database
- Feeding those outcomes back into a trained model that scores future signals
- Risk gates (CircuitBreaker, PreTradeChecker) wrapped around the cTrader path

### Out of scope
- Any live, real-money execution. cTrader DEMO only.
- MT5 order execution. `execution/mt5_bridge.py` stays for fetch only, `live/`
  runtime path is replaced by the new cTrader bridge.
- Replacing `MABPaperRunner` for offline backtest/replay. That stays as the
  research tool. The new runner is for **real-time** runs against cTrader.
- Re-engineering the strategy layer. Strategies still produce `Signal` and
  consume `bar`. The interface is unchanged.

---

## 2. Architecture

```
+----------------+     +-------------------+     +----------------------+
| MT5 terminal   | --> | data/live_sync    | --> | data/market_data.db  |
| (GUI)          |     | daemon (existing) |     | bars / ticks / events|
+----------------+     +-------------------+     +----------------------+
                                                          |
                                                          v
                              +--------------------------------------------+
                              | live/feed_watcher.py (new)                 |
                              |  - poll newest bar per timeframe           |
                              |  - emit BarEvent to EventBus                |
                              +--------------------------------------------+
                                                          |
                                                          v
+----------------+     +-------------------+     +----------------------+
| strategy/*     | --> | risk/pre_trade.py | --> | execution/oms.py     |
| on_bar -> Signal     | (size + checks)   |     | NEW: CTraderBridge   |
+----------------+     +-------------------+     +----------+-----------+
                                                          |
                                                          v
                                          +-----------------------------+
                                          | cTrader Open API  (DEMO)    |
                                          +-----------------------------+
                                                          |
                                                  fill callback
                                                          v
                              +--------------------------------------------+
                              | live/realtime_paper_runner.py (new)       |
                              |  - reconcile fill, write paper_outcomes.db |
                              |  - update position, check SL/TP/exit       |
                              +--------------------------------------------+
                                                          |
                                                          v
                              +--------------------------------------------+
                              | data/paper_outcomes.db (new)               |
                              |  - paper_fills, paper_positions,           |
                              |    paper_features_snapshot, paper_train_q  |
                              +--------------------------------------------+
                                                          |
                                              nightly / on-N trades
                                                          v
                              +--------------------------------------------+
                              | trainer/outcome_trainer.py (new)           |
                              |  - join fills + bar features               |
                              |  - fit XGBoost: features -> realized PnL   |
                              |  - emit model.pkl                          |
                              +--------------------------------------------+
                                                          |
                                                  scorer reload
                                                          v
                              +--------------------------------------------+
                              | alpha/scorer.py / probability_calibrator  |
                              |  (consume model.pkl on next live run)      |
                              +--------------------------------------------+
```

The dashed line in the existing code between **strategy -> signal** and
**execution -> fill** is exactly where the new wiring sits. Everything above
the dashed line is unchanged.

---

## 3. Module Inventory

### 3.1 Reused as-is
| Module                       | Role                                            |
|------------------------------|-------------------------------------------------|
| `data/live_sync/daemon.py`   | K-line pull (M5/M15/H1/etc) into `market_data.db` |
| `data/store.py`              | bar/tick/event reads                             |
| `data/external_loader.py`    | DXY / SLV / real-yield / event calendar         |
| `data/news_cache.py`         | NFP / FOMC / CPI windows + GVZ                  |
| `strategy/*`                 | produces `Signal`                                |
| `risk/pre_trade.py`          | size + max-lot / max-trade checks                |
| `risk/circuit.py`            | daily-loss / consecutive-loss / vol-shock trip   |
| `execution/oms.py`           | order state machine                              |
| `execution/algos.py`         | TWAP / VWAP / POV / IS (for larger orders)       |
| `execution/event_filter.py`  | T13 NFP/FOMC/CPI/GVZ skip                        |
| `alpha/scorer.py`            | consumes `model.pkl` (no change, just reload)    |
| `alpha/probability_calibrator.py` | consumes `model.pkl`                       |
| `live/factor_monitor.py`     | live factor IC tracking                          |
| `live/meta_learner_monitor.py` | live calibration tracking                      |
| `monitor/alerter.py`         | toast / log alerts                               |

### 3.2 New modules
| Module                                  | Role                                            |
|-----------------------------------------|-------------------------------------------------|
| `live/feed_watcher.py`                  | poll newest bar in `market_data.db`, emit BarEvent |
| `live/ctrader_bridge.py`                | wraps cTrader Open API; user implements transport |
| `live/realtime_paper_runner.py`         | bar -> signal -> order -> fill -> outcome loop   |
| `trainer/outcome_trainer.py`            | nightly trainer; reads `paper_outcomes.db`, writes `model.pkl` |
| `trainer/feature_joiner.py`             | join fills with bar features + factor snapshots |
| `data/paper_outcomes_store.py`          | CRUD for `data/paper_outcomes.db`               |
| `scripts/realtime_paper_daemon.py`      | supervisor process (cTrader heartbeat, restart) |
| `scripts/train_outcomes_cron.py`        | cron entry for nightly trainer                  |

### 3.3 Stays but marked deprecated
- `execution/mt5_bridge.py` -> fetch only. Order methods (`place_order`,
  `modify_sl_tp`, `close_position`) get a `# deprecated: use CTraderBridge`
  warning. The fetch path is the only thing in active use for K-line import.

### 3.4 Removed
- `live/executor.py` -> superseded by `live/realtime_paper_runner.py`. The
  old file stays in git history but is not imported.

---
## 4. Data Schema: `data/paper_outcomes.db`

This is the centerpiece of the learning loop. Every cTrader fill is written
here with the feature snapshot that produced the signal.

```sql
-- One row per FILL (open or close, partial included).
CREATE TABLE IF NOT EXISTS paper_fills (
    fill_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id         TEXT    NOT NULL,
    position_id      TEXT    NOT NULL,
    symbol           TEXT    NOT NULL,
    direction        INTEGER NOT NULL,
    volume           REAL    NOT NULL,
    fill_price       REAL    NOT NULL,
    requested_price  REAL    NOT NULL,
    slippage_usd     REAL    NOT NULL,
    commission_usd   REAL    NOT NULL,
    swap_usd         REAL    NOT NULL DEFAULT 0,
    fill_time        REAL    NOT NULL,
    bar_ts           REAL    NOT NULL,
    strategy         TEXT    NOT NULL,
    signal_id        INTEGER,
    signal_meta      TEXT    NOT NULL DEFAULT '',
    regime           TEXT    NOT NULL DEFAULT '',
    is_event_window  INTEGER NOT NULL DEFAULT 0,
    runner_run_id    TEXT    NOT NULL,
    pnl_realized_usd REAL    NOT NULL DEFAULT 0,
    holding_minutes  REAL    NOT NULL DEFAULT 0,
    notes            TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_fills_position ON paper_fills(position_id);
CREATE INDEX IF NOT EXISTS idx_fills_strategy_time ON paper_fills(strategy, fill_time);
CREATE INDEX IF NOT EXISTS idx_fills_runner ON paper_fills(runner_run_id);
CREATE INDEX IF NOT EXISTS idx_fills_bar_ts ON paper_fills(bar_ts);

-- One row per OPEN position lifecycle.
CREATE TABLE IF NOT EXISTS paper_positions (
    position_id       TEXT PRIMARY KEY,
    symbol            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    open_fill_id      INTEGER NOT NULL,
    open_time         REAL NOT NULL,
    open_price        REAL NOT NULL,
    open_volume       REAL NOT NULL,
    sl_price          REAL NOT NULL DEFAULT 0,
    tp_price          REAL NOT NULL DEFAULT 0,
    close_fill_id     INTEGER,
    close_time        REAL,
    close_price       REAL,
    close_volume      REAL,
    pnl_gross_usd     REAL,
    pnl_net_usd       REAL,
    commission_total  REAL NOT NULL DEFAULT 0,
    swap_total        REAL NOT NULL DEFAULT 0,
    max_favorable_usd REAL,
    max_adverse_usd   REAL,
    holding_minutes   REAL,
    status            TEXT NOT NULL DEFAULT 'open',
    runner_run_id     TEXT NOT NULL,
    signal_meta       TEXT NOT NULL DEFAULT '',
    regime            TEXT NOT NULL DEFAULT '',
    notes             TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON paper_positions(status);
CREATE INDEX IF NOT EXISTS idx_positions_strategy ON paper_positions(strategy, open_time);
CREATE INDEX IF NOT EXISTS idx_positions_runner ON paper_positions(runner_run_id);

-- Snapshot of factors at the time the SIGNAL was generated (not the fill time).
CREATE TABLE IF NOT EXISTS paper_signal_features (
    signal_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    bar_ts          REAL    NOT NULL,
    strategy        TEXT    NOT NULL,
    runner_run_id   TEXT    NOT NULL,
    features        TEXT    NOT NULL,
    regime          TEXT    NOT NULL DEFAULT '',
    confidence      REAL,
    decision        TEXT    NOT NULL,
    created_at      REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_features_bar ON paper_signal_features(bar_ts);
CREATE INDEX IF NOT EXISTS idx_features_strategy ON paper_signal_features(strategy, bar_ts);

-- Audit log of every order action.
CREATE TABLE IF NOT EXISTS paper_order_log (
    log_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    action        TEXT    NOT NULL,
    order_id      TEXT,
    position_id   TEXT,
    request       TEXT    NOT NULL,
    response      TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT '',
    runner_run_id TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_order_log_ts ON paper_order_log(ts);
```

### 4.1 Feature snapshot
The `paper_signal_features.features` JSON includes, at minimum:
- factor values that were in `Signal.factor_scores`
- regime tags (TRENDING_UP, RANGING, HIGH_VOL)
- the strategy that produced the signal
- the confidence value
- the bar's `atr_14`, `volume`, `time_of_day`, `day_of_week`
- external context snapshot: `dxy_close`, `real_yield_10y`, `gvz_close`,
  hours to next NFP/FOMC/CPI

This is the input to the trainer. It is frozen at signal time, not mutated
later. The trainer joins to `paper_fills` on `signal_id` and to realized
PnL via the matching `paper_positions` row.

### 4.2 Realized PnL
For a round-trip position (open + close), the realized PnL is computed as:
```
gross = (close_price - open_price) * direction * volume * contract_size
net   = gross - commission_total - swap_total
```
For XAUUSD+ the contract_size is 100 oz/lot, which is also what MT5 reports
and what `paper_engine.py` already uses.

---
