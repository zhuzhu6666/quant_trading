"""
MA Cross Strategy — H4 SMA(20)/SMA(50) 金叉死叉

逻辑:
  - SMA(20) 上穿 SMA(50) → 做多 (金叉)
  - SMA(20) 下穿 SMA(50) → 做空 (死叉)
  - 持仓时无信号 (等 SL/TP 触发)
  - SL = 2.0 × ATR(14), TP = 3.0 × ATR(14), CD = 3 bars
  - 时间框架 H4 (paper_trader 会按 H4 调用)

置信度: 0.6 固定 (简单均线, 无更多信息)

因子:
  - factor_scores: {"sma20_sma50_ratio": (sma20 - sma50) / sma50,
                     "atr": ...}
"""
import logging
from collections import deque

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='ma_cross_h4', timeframes=['H4'])
class MACrossH4Strategy(BaseStrategy):
    """
    H4 均线交叉 — SMA20 / SMA50

    Params:
        sma_fast (int):       快线周期, default=20
        sma_slow (int):       慢线周期, default=50
        atr_period (int):     ATR 周期, default=14
        sl_atr (float):       止损 ATR 倍数, default=2.0
        tp_atr (float):       止盈 ATR 倍数, default=3.0
        cooldown_bars (int):  平仓后冷却, default=3
    """
    params = {
        'sma_fast': 20,
        'sma_slow': 50,
        'atr_period': 14,
        'sl_atr': 2.0,
        'tp_atr': 3.0,
        'cooldown_bars': 3,
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

    def __init__(self, name: str, symbol: str, timeframe: str = "H4"):
        super().__init__(name, symbol, timeframe)
        p = self.params
        cache_size = max(p['sma_slow'] * 2, p['atr_period'] * 3) + 20
        self._bars = deque(maxlen=cache_size)
        self._min_bars = p['sma_slow'] + 5
        self._prev_diff: float | None = None  # 上根 bar 的 sma_fast - sma_slow

    def _sma(self, period: int) -> float:
        n = len(self._bars)
        if n < period:
            return float('nan')
        closes = np.array([b['close'] for b in list(self._bars)[-period:]], dtype=np.float64)
        return float(closes.mean())

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

        sma_f = self._sma(self.params['sma_fast'])
        sma_s = self._sma(self.params['sma_slow'])
        if np.isnan(sma_f) or np.isnan(sma_s) or sma_s <= 0:
            return None

        diff = sma_f - sma_s
        ratio = diff / sma_s  # 因子: 偏离度

        # 检测金叉/死叉: 上根 diff 与本根 diff 异号
        direction = 0
        if self._prev_diff is not None:
            if self._prev_diff <= 0 < diff:
                direction = 1  # 金叉
            elif self._prev_diff >= 0 > diff:
                direction = -1  # 死叉
        self._prev_diff = diff

        if direction == 0:
            return None

        conf = 0.6  # 固定
        entry = bar['close']
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
            factor_scores={'sma20_sma50_ratio': round(ratio, 5), 'atr': round(atr, 2)},
            confidence=round(conf, 3),
        )
