"""
Breakout Strategy — M15 Donchian 20 高低点突破

逻辑:
  - 收盘价 > 最近 20 根 bar (含当前) 最高 high → 做多突破
  - 收盘价 < 最近 20 根 bar (含当前) 最低 low → 做空突破
  - SL = 2.5 × ATR(14), TP = 4.0 × ATR(14), CD = 8 bars
  - 持仓时无信号 (等 SL/TP 触发)

置信度: 突破幅度相对 ATR 越大越高
  - 突破 1.5×ATR → conf=0.7
  - 突破 3.0×ATR → conf=0.9
  - 线性插值

因子:
  - factor_scores: {"breakout_atr": (突破点距通道边界 / ATR),
                     "atr": ...}
"""
import logging
from collections import deque

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='breakout', timeframes=['M15'])
class BreakoutStrategy(BaseStrategy):
    """
    M15 Donchian 突破

    Params:
        donchian_period (int): Donchian 通道回看 bar 数, default=20
        atr_period (int):      ATR 周期, default=14
        sl_atr (float):        止损 ATR 倍数, default=2.5
        tp_atr (float):        止盈 ATR 倍数, default=4.0
        cooldown_bars (int):   平仓后冷却, default=8
    """
    params = {
        'donchian_period': 20,
        'atr_period': 14,
        'sl_atr': 2.5,
        'tp_atr': 4.0,
        'cooldown_bars': 8,
        # REFACTOR-3 (audit 2026-06-06): 4 个 enable_* 事件 skip 字段 (capability 对称)
        # multi_factor_m15 已有, 这里给辅助策略补齐, 默认全 False (向后兼容)
        # 实际 skip 由 MABPaperRunner 主循环调 SharedEventFilter 统一处理
        # 这里加字段是给未来"4 策略独立 PaperEngine"路径 (refactor-1 拆解) 准备的
        'enable_nfp_skip': False,
        'nfp_skip_days': 1,
        'enable_dual_event_skip': False,
        'enable_fomc_boost': False,
        'fomc_boost_mult': 1.5,
        'enable_gvz_gate': False,
        'gvz_drop_pct': -2.0,
    }

    def __init__(self, name: str, symbol: str, timeframe: str = "M15"):
        super().__init__(name, symbol, timeframe)
        p = self.params
        cache_size = max(p['donchian_period'] * 2, p['atr_period'] * 3) + 20
        self._bars = deque(maxlen=cache_size)
        self._min_bars = p['donchian_period'] + 5

    def _compute_atr(self) -> float:
        n = len(self._bars)
        if n < self.params['atr_period'] + 1:
            return float('nan')
        period = self.params['atr_period']
        tr = np.zeros(n - 1)
        for i in range(1, n):
            tr[i - 1] = max(
                self._bars[i]['high'] - self._bars[i]['low'],
                abs(self._bars[i]['high'] - self._bars[i - 1]['close']),
                abs(self._bars[i]['low'] - self._bars[i - 1]['close']),
            )
        atr = float(np.mean(tr[:period]))
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
        return atr

    def on_bar(self, bar: dict) -> Signal | None:
        self._bars.append(bar)
        self._bar_count += 1

        if self._cooldown > 0:
            self._cooldown -= 1
            return None
        if len(self._bars) < self._min_bars:
            return None

        atr = self._compute_atr()
        if np.isnan(atr) or atr <= 0:
            return None
        self.last_atr = atr

        period = self.params['donchian_period']
        # Donchian 通道: 过去 period 根 bar 的高低 (不含当前 bar)
        # bars[-1] 是当前, [-period-1:-1] 是过去 period 根
        past = list(self._bars)[-period - 1:-1]
        if len(past) < period:
            return None
        highs = np.array([b['high'] for b in past])
        lows = np.array([b['low'] for b in past])
        upper = float(highs.max())
        lower = float(lows.min())

        close = bar['close']
        direction = 0
        breakout_atr = 0.0
        if close > upper:
            direction = 1
            breakout_atr = (close - upper) / atr
        elif close < lower:
            direction = -1
            breakout_atr = (lower - close) / atr
        else:
            return None

        # 置信度: 1.5x ATR → 0.7, 3.0x ATR → 0.9
        conf = 0.7 + 0.2 * max(0.0, (breakout_atr - 1.5) / 1.5)
        conf = min(0.9, max(0.7, conf))

        entry = close
        sl = entry - self.params['sl_atr'] * atr if direction == 1 else entry + self.params['sl_atr'] * atr
        tp = entry + self.params['tp_atr'] * atr if direction == 1 else entry - self.params['tp_atr'] * atr

        self._cooldown = self.params['cooldown_bars']

        return Signal(
            strategy=self.name,
            symbol=self.symbol,
            direction=direction,
            strength=conf,
            sl_atr=self.params['sl_atr'],
            tp_atr=self.params['tp_atr'],
            atr=atr,
            price=entry,
            timestamp=bar['time'],
            meta={'sl': sl, 'tp': tp, 'lots': 0.01},
            factor_scores={'breakout_atr': round(breakout_atr, 2), 'atr': round(atr, 2)},
            confidence=round(conf, 3),
        )
