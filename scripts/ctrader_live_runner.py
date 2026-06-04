"""
scripts/ctrader_live_runner.py — cTrader Live Runner (broker-simulator)

形态: MT5 拉 bar 喂 strategy → 出 signal → cTrader 真发单 → 记录成交 → jsonl 落盘

不集成 paper_engine / event_sizing / pre_trade — 极简 demo runner。
SL/TP 在本地 Python 层监控 (cTrader MARKET 单不支持 SL/TP 上 server, 阶段 3 再补)。

凭证: 全走 .env 的 CTRADER_*  (execution/_env.py 自动加载)

用法:
  python scripts/ctrader_live_runner.py --dry-run --n-bars 100 --timeframe M15
  python scripts/ctrader_live_runner.py --live --n-bars 5000
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# 让脚本能从项目根 import
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from execution._env import load_env  # noqa: E402
load_env()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ctrader_live")


# ── 本地持仓状态 ─────────────────────────────────────────────────

@dataclass
class LocalPosition:
    """本地持仓镜像 — 每根 bar 后跟 broker get_positions() 对账"""
    position_id: int = 0          # broker 给的 ID
    direction: int = 0            # 1=long, -1=short
    volume: float = 0.0           # lots
    entry_price: float = 0.0      # broker 报的实际成交价
    sl_price: float = 0.0         # 本地止损 (server 端没挂)
    tp_price: float = 0.0         # 本地止盈
    entry_time: float = 0.0       # epoch
    entry_bar_idx: int = 0        # 用于算 bars_held
    strategy: str = ""
    sl_atr: float = 0.0
    tp_atr: float = 0.0
    atr: float = 0.0
    comment: str = ""


@dataclass
class TradeRecord:
    """落 jsonl 的单条记录"""
    ts: float
    event: str                    # "open" | "close"
    symbol: str = "XAUUSD"
    side: str = ""                # "buy" | "sell" | "" (平仓)
    volume: float = 0.0
    price: float = 0.0
    sl: float = 0.0
    tp: float = 0.0
    atr: float = 0.0
    strength: float = 1.0
    strategy: str = ""
    position_id: int = 0
    order_id: int = 0
    exit_price: float = 0.0
    pnl: float = 0.0
    exit_reason: str = ""         # "tp_hit" | "sl_hit" | "signal_flip" | "close_call" | "eod"
    bars_held: int = 0
    comment: str = ""


# ── JSONL 落盘 ─────────────────────────────────────────────────

class TradeLogger:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 不 append, 每次跑覆盖 (跟 reports 一致)
        self.path.write_text("", encoding="utf-8")
        self.count = 0

    def write(self, rec: TradeRecord):
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(rec), ensure_ascii=False) + "\n")
        self.count += 1

    def read_all(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


# ── SL/TP 本地监控 ─────────────────────────────────────────────

def check_sl_tp(pos: LocalPosition, bar: dict) -> str | None:
    """返回 None / "sl_hit" / "tp_hit". 用 bar.low / bar.high 跟 sl/tp 比"""
    if pos.direction == 0:
        return None
    high = bar.get("high", 0.0)
    low = bar.get("low", 0.0)
    if pos.direction == 1:  # long
        if pos.sl_price > 0 and low <= pos.sl_price:
            return "sl_hit"
        if pos.tp_price > 0 and high >= pos.tp_price:
            return "tp_hit"
    else:  # short
        if pos.sl_price > 0 and high >= pos.sl_price:
            return "sl_hit"
        if pos.tp_price > 0 and low <= pos.tp_price:
            return "tp_hit"
    return None


# ── 仓位同步 (本地 ↔ broker) ──────────────────────────────────

def reconcile_position(bridge, local: LocalPosition, symbol: str) -> tuple[bool, Optional[dict]]:
    """拉 broker 真持仓, 跟本地比对.

    Returns:
        (changed, broker_pos_or_None)
        - changed=True 如果 broker 持仓跟本地不一致 (新增/消失/方向变)
        - broker_pos_or_None: broker 当前该 symbol 的 position (dict) 或 None
    """
    try:
        positions = bridge.get_positions(symbol=symbol)
    except Exception as e:
        log.warning(f"get_positions failed: {e}")
        return False, None
    if not positions:
        return local.position_id != 0, None
    # 取第一个匹配的 (XAUUSD demo 应该只有一个)
    pos = positions[0]
    return True, pos


# ── 信号 → 订单转换 ───────────────────────────────────────────

def sl_tp_prices(signal) -> tuple[float, float]:
    """从 signal 算 sl/tp 绝对价. signal.price + signal.atr * signal.{sl,tp}_atr"""
    if signal.direction == 1:  # long
        sl = signal.price - signal.atr * signal.sl_atr
        tp = signal.price + signal.atr * signal.tp_atr
    elif signal.direction == -1:  # short
        sl = signal.price + signal.atr * signal.sl_atr
        tp = signal.price - signal.atr * signal.tp_atr
    else:
        sl = tp = 0.0
    return round(sl, 2), round(tp, 2)


# ── 主流程 ─────────────────────────────────────────────────────

def run_live(args):
    from execution.ctrader_bridge import CTraderBridge
    from execution.mt5_bridge import MT5Bridge, fetch_history, rates_to_dataframe
    from strategy.registry import strategy_registry

    # ── cTrader bridge ──
    bridge = CTraderBridge(
        client_id=args.ctrader_client_id,
        client_secret=args.ctrader_client_secret,
        access_token=args.ctrader_access_token,
        account_id=args.ctrader_account_id,
        host=args.host,
        port=args.port,
        symbol=args.symbol,
        rate_limit_per_sec=5,
        request_timeout_sec=10.0,
        send_orders=not args.dry_run,   # --dry-run → send_orders=False
    )
    log.info(f"cTrader bridge: host={args.host}:{args.port} symbol={args.symbol} "
             f"account={args.ctrader_account_id} send_orders={not args.dry_run}")
    if not bridge.connect():
        log.error("cTrader connect failed, exit")
        return 1
    log.info(f"cTrader connected: is_connected={bridge.is_connected}")

    # ── MT5 拉 bar ──
    log.info(f"MT5 fetching {args.n_bars} {args.timeframe} bars for {args.mt5_symbol}...")
    mt5 = MT5Bridge()
    if not mt5.connect():
        log.error("MT5 connect failed, exit")
        return 1
    df = mt5.fetch_bars(timeframe=_tf_enum(args.timeframe), n_bars=args.n_bars)
    mt5.disconnect()
    if df is None or df.empty:
        log.error(f"MT5 returned no bars, exit")
        return 1
    log.info(f"MT5 bars: {len(df)} rows, range {df.index[0]} → {df.index[-1]}")

    # ── Strategy 实例化 ──
    strat = strategy_registry.create(args.strategy, symbol=args.symbol, timeframe=args.timeframe)
    log.info(f"strategy: {strat.name} ({args.strategy})")

    # ── Trade logger ──
    logger_ = TradeLogger(args.log)
    log.info(f"trade log: {args.log}")

    # ── Main loop: 逐 bar 喂 strategy ──
    local_pos = LocalPosition()
    bar_count = 0
    last_balance = None

    for idx, (ts, row) in enumerate(df.iterrows()):
        bar_count += 1
        bar = {
            "time": ts.timestamp() if hasattr(ts, 'timestamp') else float(ts),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
            "complete": True,
            "timeframe": args.timeframe,
        }

        # ── 1. 本地 SL/TP 检查 (用 broker 实际持仓 ID) ──
        if local_pos.position_id != 0:
            trigger = check_sl_tp(local_pos, bar)
            if trigger:
                log.info(f"  [{idx}] LOCAL {trigger} triggered: bar.low={bar['low']:.2f} bar.high={bar['high']:.2f} "
                         f"sl={local_pos.sl_price:.2f} tp={local_pos.tp_price:.2f}")
                res = bridge.close_position(position_id=local_pos.position_id)
                if res.success:
                    # 拿 account balance 算 PnL (简化: 用 close_price = bar open 作为 slip 价)
                    close_price = bar["open"]  # 简化: bar 开盘价
                    if local_pos.direction == 1:
                        pnl = (close_price - local_pos.entry_price) * local_pos.volume * 100  # 1 lot = 100 oz
                    else:
                        pnl = (local_pos.entry_price - close_price) * local_pos.volume * 100
                    logger_.write(TradeRecord(
                        ts=bar["time"], event="close", symbol=args.symbol,
                        position_id=local_pos.position_id,
                        exit_price=close_price, pnl=round(pnl, 4),
                        exit_reason=trigger, bars_held=idx - local_pos.entry_bar_idx,
                        comment=f"local_{trigger}",
                    ))
                    local_pos = LocalPosition()
                else:
                    log.error(f"  close_position failed: {res.comment}")

        # ── 2. 对账 broker 持仓 ──
        changed, broker_pos = reconcile_position(bridge, local_pos, args.symbol)
        if broker_pos and local_pos.position_id == 0:
            # broker 有, 本地无 → 新开仓
            local_pos.position_id = int(broker_pos.get("position_id", 0))
            local_pos.direction = 1 if broker_pos.get("type", "").upper() in ("BUY", "LONG", 1) else -1
            local_pos.volume = float(broker_pos.get("volume", 0))
            local_pos.entry_price = float(broker_pos.get("price_open", 0))
            local_pos.entry_time = bar["time"]
            local_pos.entry_bar_idx = idx
            log.info(f"  [{idx}] RECONCILE: broker has pos_id={local_pos.position_id} "
                     f"dir={local_pos.direction} vol={local_pos.volume} entry={local_pos.entry_price:.2f}")
        elif not broker_pos and local_pos.position_id != 0:
            # broker 无, 本地有 → 已平仓 (可能是 SL/TP trigger 完 + broker 状态还没刷新, 重复 close 安全)
            log.info(f"  [{idx}] RECONCILE: broker no pos, was pos_id={local_pos.position_id} "
                     f"sl={local_pos.sl_price:.2f} tp={local_pos.tp_price:.2f}")
            local_pos = LocalPosition()

        # ── 3. 调 strategy.on_bar ──
        try:
            signal = strat.on_bar(bar)
        except Exception as e:
            log.error(f"  strategy.on_bar failed at bar {idx}: {e!r}")
            continue

        if signal is None or signal.direction == 0:
            continue

        log.info(f"  [{idx}] SIGNAL: {signal.strategy} dir={signal.direction} "
                 f"price={signal.price:.2f} atr={signal.atr:.2f} "
                 f"sl_atr={signal.sl_atr} tp_atr={signal.tp_atr} strength={signal.strength:.2f}")

        # ── 4. 处理 CLOSE 信号 (direction=2) ──
        if signal.direction == 2 and local_pos.position_id != 0:
            res = bridge.close_position(position_id=local_pos.position_id)
            if res.success:
                close_price = bar["close"]
                if local_pos.direction == 1:
                    pnl = (close_price - local_pos.entry_price) * local_pos.volume * 100
                else:
                    pnl = (local_pos.entry_price - close_price) * local_pos.volume * 100
                logger_.write(TradeRecord(
                    ts=bar["time"], event="close", symbol=args.symbol,
                    position_id=local_pos.position_id,
                    exit_price=close_price, pnl=round(pnl, 4),
                    exit_reason="signal_close", bars_held=idx - local_pos.entry_bar_idx,
                ))
                local_pos = LocalPosition()
            continue

        # ── 5. 处理 OPEN 信号 ──
        if signal.direction not in (1, -1):
            continue
        # 已有持仓不重复开
        if local_pos.position_id != 0:
            log.info(f"  [{idx}] skip open, already have pos_id={local_pos.position_id}")
            continue

        sl_p, tp_p = sl_tp_prices(signal)
        volume = args.volume  # 极简, hardcode
        comment = f"{signal.strategy}|d={signal.direction}|s={signal.strength:.2f}"

        if signal.direction == 1:
            res = bridge.market_buy(volume=volume, sl=0.0, tp=0.0, comment=comment)
        else:
            res = bridge.market_sell(volume=volume, sl=0.0, tp=0.0, comment=comment)

        log.info(f"  [{idx}] ORDER: side={'buy' if signal.direction == 1 else 'sell'} vol={volume} "
                 f"res.success={res.success} order_id={res.order_id} comment={res.comment!r}")

        if res.success:
            # 立即写 open 记录 (entry_price 后续从 broker 拉)
            logger_.write(TradeRecord(
                ts=bar["time"], event="open", symbol=args.symbol,
                side="buy" if signal.direction == 1 else "sell",
                volume=volume, price=signal.price,
                sl=sl_p, tp=tp_p, atr=signal.atr,
                strength=signal.strength, strategy=signal.strategy,
                position_id=res.position_id, order_id=res.order_id,
                comment=comment,
            ))
            # 注: entry_price 等下一轮 reconcile_position 时从 broker get_positions() 拉

    # ── 总结 ──
    bridge.disconnect()
    records = logger_.read_all()
    opens = [r for r in records if r["event"] == "open"]
    closes = [r for r in records if r["event"] == "close"]
    pnl_sum = sum(r["pnl"] for r in closes)
    report_lines = [
        "=" * 70,
        f"  cTrader Live Runner Report — {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "=" * 70,
        f"  mode         = {'DRY-RUN' if args.dry_run else 'LIVE'}",
        f"  strategy     = {args.strategy}",
        f"  symbol       = {args.symbol} (cTrader) / {args.mt5_symbol} (MT5)",
        f"  bars walked  = {bar_count}",
        f"  volume       = {args.volume} lot",
        f"  trade log    = {args.log}",
        "",
        f"  opens  = {len(opens)}",
        f"  closes = {len(closes)}",
        f"  pnl    = ${pnl_sum:+.2f} (本地估算, 含 slippage 误差)",
        f"  tp_hit = {sum(1 for r in closes if r.get('exit_reason') == 'tp_hit')}",
        f"  sl_hit = {sum(1 for r in closes if r.get('exit_reason') == 'sl_hit')}",
        f"  signal = {sum(1 for r in closes if r.get('exit_reason') == 'signal_close')}",
        "=" * 70,
    ]
    report = "\n".join(report_lines)
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(report, encoding="utf-8")
    log.info("\n" + report)
    log.info(f"report written: {args.report}")
    return 0


def _tf_enum(s: str) -> int:
    """字符串 timeframe → MT5 enum."""
    import MetaTrader5 as mt5
    return {
        "M1": mt5.TIMEFRAME_M1, "M5": mt5.TIMEFRAME_M5, "M15": mt5.TIMEFRAME_M15,
        "M30": mt5.TIMEFRAME_M30, "H1": mt5.TIMEFRAME_H1, "H4": mt5.TIMEFRAME_H4,
        "D1": mt5.TIMEFRAME_D1,
    }.get(s.upper(), mt5.TIMEFRAME_M15)


def main():
    import os
    p = argparse.ArgumentParser()
    p.add_argument("--strategy", default="multi_factor_m15", help="strategy_registry name")
    p.add_argument("--symbol", default="XAUUSD", help="cTrader symbol (Pepperstone demo: XAUUSD, 无 +)")
    p.add_argument("--mt5-symbol", default="XAUUSD+", help="MT5 symbol (with broker suffix +)")
    p.add_argument("--timeframe", default="M15")
    p.add_argument("--n-bars", type=int, default=5000)
    p.add_argument("--volume", type=float, default=0.01, help="手数 (XAUUSD 0.01 = 1 oz)")
    p.add_argument("--dry-run", action="store_true", help="DRY-RUN (send_orders=False), 不真发")
    p.add_argument("--live", action="store_true", help="LIVE (send_orders=True, 真发 cTrader)")
    p.add_argument("--host", default="demo.ctraderapi.com")
    p.add_argument("--port", type=int, default=5035)
    p.add_argument("--ctrader-client-id", default=os.environ.get("CTRADER_CLIENT_ID", ""))
    p.add_argument("--ctrader-client-secret", default=os.environ.get("CTRADER_CLIENT_SECRET", ""))
    p.add_argument("--ctrader-access-token", default=os.environ.get("CTRADER_ACCESS_TOKEN", ""))
    p.add_argument("--ctrader-account-id", type=int, default=int(os.environ.get("CTRADER_ACCOUNT_ID", "0")))
    p.add_argument("--log", default="data/charts/ctrader_live_trades.jsonl")
    p.add_argument("--report", default="data/charts/ctrader_live_report.txt")
    args = p.parse_args()
    if not args.live and not args.dry_run:
        # 默认 DRY-RUN (安全)
        args.dry_run = True
    if args.live:
        args.dry_run = False
    return run_live(args)


if __name__ == "__main__":
    sys.exit(main())
