"""
strategies/backtest_strategy.py — backtrader 回测策略 (共享 CLI + Web 用)

audit 2026-06-08: 从 main.py:run_backtest() 抽出, 避免 CLI 和 Web
backtest_runner.py 实现漂移。原 main.py 代码保留 import 此模块。
"""
from __future__ import annotations

import numpy as np


# ── 公共常量 (跟 paper 配置对齐) ──
CONTRACT_SIZE = 100       # XAUUSD+ 100 oz/lot
INITIAL_BALANCE = 500.0
COMMISSION = 0.00003      # $6/100oz, round-turn $12
WARMUP_BARS = 500


def run_one(df, sl_atr: float, tp_atr: float, cooldown_bars: int,
            risk_pct: float | None = None,
            initial_balance: float = INITIAL_BALANCE) -> dict:
    """在 df 上跑一次回测, 返 {trades, win_rate, net_pnl, total_return, sharpe, max_drawdown}.

    参数跟 main.py:run_backtest 的 _run_one 一致。
    """
    import backtrader as bt

    c = bt.Cerebro(stdstats=False)
    c.adddata(bt.feeds.PandasData(dataname=df))
    c.broker.setcommission(commission=COMMISSION, leverage=1)
    c.broker.setcash(initial_balance)
    c.addanalyzer(bt.analyzers.SharpeRatio, _name="sharpe", riskfreerate=0.0, annualize=True)
    c.addanalyzer(bt.analyzers.DrawDown, _name="dd")
    c.addanalyzer(bt.analyzers.Returns, _name="returns")
    c.addanalyzer(bt.analyzers.TradeAnalyzer, _name="trades")

    class _ScanStrategy(bt.Strategy):
        """Multi-Factor M15 投票策略的 backtrader 实现.
        从 main.py:274-345 抽出. 使用 bt.ind 内置指标."""
        params = (
            ("_sl_atr", sl_atr),
            ("_tp_atr", tp_atr),
            ("_cooldown_bars", cooldown_bars),
            ("warmup_bars", WARMUP_BARS),
            ("_contract_size", CONTRACT_SIZE),
        )

        di_period = 14; rsi_period = 14; stoch_period = 14
        macd_fast = 12; macd_slow = 26; macd_signal = 9
        bb_period = 20; bb_std = 2.0; atr_period = 14

        def __init__(self):
            self._cooldown = 0
            self._bb_widths = bt.LineBuffer()
            self._rsi = bt.ind.RSI(self.data.close, period=self.rsi_period)
            self._atr = bt.ind.ATR(self.data, period=self.atr_period)
            self._pdi = bt.ind.PlusDI(self.data, period=self.di_period)
            self._ndi = bt.ind.MinusDI(self.data, period=self.di_period)
            self._stoch = bt.ind.Stochastic(self.data, period=self.stoch_period, period_dfast=3, period_dslow=3)
            self._macd = bt.ind.MACD(self.data.close, period_me1=self.macd_fast, period_me2=self.macd_slow, period_signal=self.macd_signal)
            self._bb = bt.ind.BollingerBands(self.data.close, period=self.bb_period, devfactor=self.bb_std)

        def _di_spread(self): return self._pdi[0] - self._ndi[0]
        def _stoch_k(self): return self._stoch.lines.percK[0]
        def _bb_width(self): return self._bb.lines.top[0] - self._bb.lines.bot[0]
        def _hist(self): return self._macd.lines.macd[0] - self._macd.lines.signal[0]

        def next(self):
            p = self.params
            if len(self) <= p.warmup_bars: return
            if self._cooldown > 0: self._cooldown -= 1; return
            a = self._atr[0]; r = self._rsi[0]; d = self._di_spread(); s = self._stoch_k()
            bw = self._bb_width(); h = self._hist()
            if any(x is None for x in (a, r, d, s, bw, h)): return
            self._bb_widths.extend([bw])
            if len(self._bb_widths) >= 20:
                thresh = float(np.percentile(list(self._bb_widths), 80))
                if bw >= thresh: return
            vl = vs = 0
            if d > 0: vl += 1
            elif d < 0: vs += 1
            if r > 50: vl += 1
            elif r < 50: vs += 1
            if s > 50: vl += 1
            elif s < 50: vs += 1
            if vl < 2 and vs < 2: return
            direction = 1 if vl >= 2 else -1
            if direction == 1 and h > 0: return
            if direction == -1 and h < 0: return
            self._cooldown = p._cooldown_bars
            entry = self.data.close[0]
            sl = entry - p._sl_atr * a if direction == 1 else entry + p._sl_atr * a
            tp = entry + p._tp_atr * a if direction == 1 else entry - p._tp_atr * a
            if risk_pct and risk_pct > 0:
                sl_dist = abs(entry - sl)
                if sl_dist > 0:
                    risk_dollars = self.broker.getvalue() * (risk_pct / 100.0)
                    size = risk_dollars / (sl_dist * p._contract_size)
                else:
                    size = 0.01
            else:
                size = 0.01
            self.buy_bracket(exectype=bt.Order.Close, size=size, stopprice=sl, limitprice=tp)

    c.addstrategy(_ScanStrategy)
    results = c.run(runonce=True)
    strat = results[0]

    ta = strat.analyzers.trades.get_analysis()
    dd = strat.analyzers.dd.get_analysis()
    sh = strat.analyzers.sharpe.get_analysis()
    final_value = strat.broker.getvalue()
    net_pnl = final_value - initial_balance
    total_return = net_pnl / initial_balance * 100
    sharpe = sh.get("sharperatio", None) or 0.0
    max_dd = dd.get("max", {}).get("drawdown", 0.0) or 0.0
    total_t = ta.get("total", {}).get("total", 0) or 0
    won_t = ta.get("won", {}).get("total", 0) or 0
    win_rate = (won_t / total_t * 100) if total_t > 0 else 0.0
    return {
        "trades": total_t, "win_rate": win_rate,
        "net_pnl": net_pnl, "total_return": total_return,
        "sharpe": sharpe, "max_drawdown": max_dd,
    }
