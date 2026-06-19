"""Factor Takeover v4 全流程回测 (M5 优化版)。

预计算全部因子值 → 逐 bar 跑归一化/组合/闸门/模拟交易。
"""
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# ═══════════════════════════════════════════════════════════
# 参数
# ═══════════════════════════════════════════════════════════
os.environ.setdefault("BT_TIMEFRAME", "M5")
TIMEFRAME = os.environ["BT_TIMEFRAME"]
MONTH = os.environ.get("BT_MONTH", "2026-05")
CONTRACT_SIZE = 100      # XAUUSD+ 100 oz/lot
LOT_SIZE = 0.01          # 固定 0.01 lot
SWAP_PER_LOT_DAY = -15.0 # 过夜利息 $15/手/天
INITIAL_CAPITAL = 10000.0

# ═══════════════════════════════════════════════════════════
# 交易成本
# ═══════════════════════════════════════════════════════════
def _calc_pnl(entry, exit_price, direction):
    return (exit_price - entry) * direction * LOT_SIZE * CONTRACT_SIZE

def _calc_trade_cost():
    return 0.12 + 6.0 * LOT_SIZE  # 点差 + 佣金

def _calc_swap(entry_ts, exit_ts):
    hours = (exit_ts - entry_ts) / 3600
    return SWAP_PER_LOT_DAY / 24 * hours * LOT_SIZE

# ═══════════════════════════════════════════════════════════
# 加载数据
# ═══════════════════════════════════════════════════════════
print(f":: 加载 {TIMEFRAME} K 线 ({MONTH}) ...")
conn = sqlite3.connect(str(ROOT / "data/ctrader_data.duckdb"))
# 时间范围
month_map = {"2026-05": (1777593600, 1780272000), "2026-04": (1775001600, 1777593600)}
if MONTH in month_map:
    ts_from, ts_to = month_map[MONTH]
else:
    # fallback: 最近 N 天
    max_ts = conn.execute("SELECT MAX(time) FROM bars WHERE timeframe=?", (TIMEFRAME,)).fetchone()[0]
    ts_from, ts_to = max_ts - 30 * 86400, max_ts
df = pd.read_sql_query(
    "SELECT time, open, high, low, close, volume FROM bars "
    "WHERE symbol='XAUUSD+' AND timeframe=? AND time >= ? AND time < ? ORDER BY time ASC",
    conn, params=(TIMEFRAME, ts_from, ts_to), parse_dates=False,
)
conn.close()
print(f"   共 {len(df)} 根 bar, 时间: {pd.to_datetime(df['time'].iloc[0], unit='s').date()} "
      f"~ {pd.to_datetime(df['time'].iloc[-1], unit='s').date()}")
df.index = pd.to_datetime(df["time"], unit="s")
df.index.name = "time"

# ═══════════════════════════════════════════════════════════
# 预计算所有因子 (向量化, 一次性)
# ═══════════════════════════════════════════════════════════
print("\n:: 预计算 39+ 因子值 (向量化) ...")
from alpha.registry import factor_registry
t0 = time.time()
factor_values = {}  # {name: np.ndarray}
for name in factor_registry.list():
    try:
        fn = factor_registry.get(name)
        if fn is None:
            continue
        vals = fn(df)
        factor_values[name] = np.asarray(vals, dtype=float)
    except Exception as e:
        print(f"   ⚠ {name}: {e}")
print(f"   完成: {len(factor_values)} 个因子, {time.time()-t0:.1f}s")

# ═══════════════════════════════════════════════════════════
# 初始化管道
# ═══════════════════════════════════════════════════════════
print("\n:: 初始化因子管道 ...")
from config.runtime_config import RuntimeConfig
cfg = RuntimeConfig()

from alpha.signal_normalizer import SignalNormalizer
from alpha.portfolio_compositor import PortfolioCompositor
from alpha.execution_gate import ExecutionGate

normalizer = SignalNormalizer(cfg.factor_signal_config)

# 合并 config
all_names = set(cfg.factor_signal_config) | set(cfg.factor_portfolio_weights)
compositor_cfg = {}
for name in all_names:
    sc = cfg.factor_signal_config.get(name, {})
    wc = cfg.factor_portfolio_weights.get(name, 1.0)
    weight = wc if isinstance(wc, (int, float)) else wc.get("weight", 1.0)
    compositor_cfg[name] = {
        "weight": weight, "tags": sc.get("tags", []),
        "mode": sc.get("mode", "rank_mapping"),
        "enabled": sc.get("enabled", True),
    }
compositor_cfg["_tactical_alpha"] = cfg.factor_tactical_alpha
compositor_cfg["_signal_threshold"] = cfg.factor_signal_threshold
compositor = PortfolioCompositor(compositor_cfg)

gate = ExecutionGate({
    "signal_threshold": cfg.factor_signal_threshold,
    "cooldown_bars": 3,
})

# ═══════════════════════════════════════════════════════════
# 回测主循环
# ═══════════════════════════════════════════════════════════
print("\n:: 运行回测 ...")
WARMUP_BARS = 50

class Trade:
    __slots__ = ("entry_time", "entry_price", "direction", "sl", "tp",
                 "exit_time", "exit_price", "pnl", "reason", "_score", "_bars_held")

trades = []
position = None
balance = INITIAL_CAPITAL
equity_curve = [balance]

n_bars = len(df)
factor_names = list(factor_values.keys())
last_pct = 0

for i in range(n_bars):
    row = df.iloc[i]
    bar = {
        "open": float(row["open"]), "high": float(row["high"]),
        "low": float(row["low"]), "close": float(row["close"]),
        "volume": float(row["volume"]), "time": float(row["time"]),
        "timeframe": TIMEFRAME, "complete": True,
    }
    ts = bar["time"]

    # ── 检查持仓 SL/TP ──
    if position is not None:
        hi, lo = bar["high"], bar["low"]
        hit = False
        if position.direction == 1:
            if lo <= position.sl: position.exit_price = position.sl; hit = True; position.reason = "SL"
            elif hi >= position.tp: position.exit_price = position.tp; hit = True; position.reason = "TP"
        else:
            if hi >= position.sl: position.exit_price = position.sl; hit = True; position.reason = "SL"
            elif lo <= position.tp: position.exit_price = position.tp; hit = True; position.reason = "TP"
        if hit:
            position.exit_time = ts
            position.pnl = (_calc_pnl(position.entry_price, position.exit_price, position.direction)
                            - _calc_trade_cost() + _calc_swap(position.entry_time, ts))
            trades.append(position)
            balance += position.pnl
            equity_curve.append(balance)
            position = None

    # ── 冷启动: buffer 不足时预热 normalizer ──
    if i < WARMUP_BARS:
        fv = {name: (float(vals[i]) if not (np.isnan(vals[i]) or np.isinf(vals[i])) else None)
              for name, vals in factor_values.items()}
        normalizer.normalize(fv)
        gate.tick()
        equity_curve.append(balance)
        continue

    # ── 因子信号 (无需 engine, 直接取预计算值) ──
    fv = {name: (float(vals[i]) if not (np.isnan(vals[i]) or np.isinf(vals[i])) else None)
          for name, vals in factor_values.items()}
    signals = normalizer.normalize(fv)
    composite = compositor.compose(signals, fv)
    gate_result = gate.filter(composite, fv, bar)
    gate.tick()

    # ── 开仓 ──
    if position is None and gate_result.passed and composite.direction != 0:
        atr_val = fv.get("atr_ratio", 0)
        atr_price = atr_val * bar["close"] if atr_val and atr_val > 0 else 0
        if atr_price <= 0:
            atr_price = bar["close"] * 0.001  # 保底 0.1%

        # 动态 SL/TP: 信号越强, 容忍度越大
        score = abs(composite.score)
        sl_mult = 1.5 + score * 2.5   # score 0.4→2.5×, 0.9→3.75×
        tp_mult = 2.0 + score * 4.0   # score 0.4→3.6×, 0.9→5.6×

        # 共识加成: 多个因子同向 → 更宽
        n_aligned = sum(1 for s in signals.values()
                        if s and abs(s) > 0.2 and s * composite.direction > 0)
        sl_mult += n_aligned * 0.05   # 10 个同向 → +0.5
        tp_mult += n_aligned * 0.1

        sl_dist = atr_price * sl_mult
        tp_dist = atr_price * tp_mult
        entry = bar["close"]
        t = Trade()
        t.entry_time = ts; t.entry_price = entry; t.direction = composite.direction
        t.sl = entry - sl_dist if t.direction == 1 else entry + sl_dist
        t.tp = entry + tp_dist if t.direction == 1 else entry - tp_dist
        t._score = composite.score  # 记录信号强度, 用于动态调整
        t._bars_held = 0
        position = t

    # ── 如果持仓中, 动态调整止损 (前 3 bar 给呼吸空间) ──
    if position is not None:
        position._bars_held = getattr(position, "_bars_held", 0) + 1
        # 前 3 bar: 如果还没触发, 把止损放宽到 2 倍, 防假突破
        if position._bars_held <= 3:
            atr_val = fv.get("atr_ratio", 0)
            atr_price = atr_val * bar["close"] if atr_val and atr_val > 0 else bar["close"] * 0.001
            wide_sl = atr_price * 4.0 * position.direction
            if position.direction == 1:
                candidate_sl = bar["close"] - wide_sl
                if candidate_sl < position.sl:  # 只放宽, 不收紧
                    position.sl = candidate_sl
            else:
                candidate_sl = bar["close"] + wide_sl
                if candidate_sl > position.sl:
                    position.sl = candidate_sl

    equity_curve.append(balance)

    # 进度
    pct = (i + 1) * 100 // n_bars
    if pct > last_pct and pct % 5 == 0:
        print(f"   {pct}% ({i+1}/{n_bars})")
        last_pct = pct

# ── 平最后持仓 ──
if position is not None:
    last_close = float(df.iloc[-1]["close"])
    position.exit_price = last_close
    position.exit_time = float(df.iloc[-1]["time"])
    position.pnl = (_calc_pnl(position.entry_price, last_close, position.direction)
                    - _calc_trade_cost() + _calc_swap(position.entry_time, position.exit_time))
    position.reason = "END"
    trades.append(position)
    balance += position.pnl
    equity_curve.append(balance)
    position = None

# ═══════════════════════════════════════════════════════════
# 报告
# ═══════════════════════════════════════════════════════════
print("\n" + "=" * 60)
print(f"回测结果 — Factor Takeover v4 ({TIMEFRAME})")
print("=" * 60)

n_trades = len(trades)
n_wins = sum(1 for t in trades if t.pnl > 0)
win_rate = n_wins / n_trades if n_trades else 0
total_pnl = balance - INITIAL_CAPITAL

# Sharpe
eq_arr = np.array(equity_curve)
ret = np.diff(eq_arr) / eq_arr[:-1]
bpv = {"M1": 525600, "M5": 105120, "M15": 35040, "M30": 17520, "H1": 8760}
sharpe = np.mean(ret) / np.std(ret) * np.sqrt(bpv.get(TIMEFRAME, 35040)) if np.std(ret) > 0 else 0

# Max DD
peak = np.maximum.accumulate(eq_arr)
max_dd = np.min((eq_arr - peak) / peak) * 100

# 持仓时间统计
hold_hours = []
for t in trades:
    if t.exit_time and t.entry_time:
        hold_hours.append((t.exit_time - t.entry_time) / 3600)

print(f"\n交易统计:")
print(f"  总 bar 数:      {n_bars}")
print(f"  交易次数:       {n_trades}  ({n_trades/((df['time'].iloc[-1]-df['time'].iloc[0])/86400):.1f}/天)")
print(f"  胜率:           {win_rate:.1%} ({n_wins}W / {n_trades-n_wins}L)")
print(f"  平均持仓:       {np.mean(hold_hours):.1f}h" if hold_hours else "  --")
print(f"  过夜单占比:     {sum(1 for h in hold_hours if h > 24)/max(len(hold_hours),1):.1%}" if hold_hours else "  --")

print(f"\n收益统计:")
print(f"  初始资金:       ${INITIAL_CAPITAL:,.2f}")
print(f"  最终资金:       ${balance:,.2f}")
print(f"  总盈亏:         ${total_pnl:,.2f} ({total_pnl/INITIAL_CAPITAL*100:+.2f}%)")
print(f"  Sharpe (年化):  {sharpe:.3f}")
print(f"  最大回撤:       {max_dd:.2f}%")

print(f"\n风控统计:")
print(f"  SL 平仓:        {sum(1 for t in trades if t.reason == 'SL')}")
print(f"  TP 平仓:        {sum(1 for t in trades if t.reason == 'TP')}")
# 计算总成本
total_costs = sum(_calc_trade_cost() for _ in trades)
total_swap = sum(_calc_swap(t.entry_time, t.exit_time) for t in trades if t.exit_time)
print(f"  总交易成本:     ${total_costs:.2f}")
print(f"  总过夜利息:     ${total_swap:.2f}")

# 保存结果
out = ROOT / "data" / "charts" / f"backtest_v4_{TIMEFRAME}.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps({
    "timeframe": TIMEFRAME, "n_bars": n_bars, "n_trades": n_trades,
    "win_rate": round(win_rate, 4), "total_pnl": round(total_pnl, 2),
    "total_return_pct": round(total_pnl/INITIAL_CAPITAL*100, 2),
    "sharpe": round(sharpe, 4), "max_drawdown_pct": round(max_dd, 2),
    "sl": sum(1 for t in trades if t.reason == 'SL'),
    "tp": sum(1 for t in trades if t.reason == 'TP'),
    "total_costs": round(total_costs, 2), "total_swap": round(total_swap, 2),
}, indent=2))
print(f"\n结果保存: {out}")
