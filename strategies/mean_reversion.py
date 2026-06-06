"""
Mean Reversion Strategy — M15 RSI(14) 超买超卖反转

逻辑:
  - RSI(14) < 30 → 做多 (超卖反弹)
  - RSI(14) > 70 → 做空 (超买回落)
  - 持仓时无信号返回 None (不重复开仓)
  - SL = 2.0 × ATR(14), TP = 3.0 × ATR(14), CD = 5 bars

置信度: 反向越极端越高
  - 多头信号 RSI=10 → conf=0.9, RSI=30 → conf=0.5
  - 空头信号 RSI=90 → conf=0.9, RSI=70 → conf=0.5
  - 线性插值

因子:
  - factor_scores: {"rsi": ..., "atr": ...}

持仓: 平仓由 SL/TP 触发, 本策略不主动平仓
"""
import logging
from collections import deque
from typing import Optional

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='mean_reversion', timeframes=['M15'])
class MeanReversionStrategy(BaseStrategy):
    """
    M15 均值回归 — RSI(14) 极端反向开仓

    Params:
        rsi_period (int):     RSI 周期, default=14
        rsi_oversold (float): 超卖阈值, default=30
        rsi_overbought (float): 超买阈值, default=70
        atr_period (int):     ATR 周期, default=14
        sl_atr (float):       止损 ATR 倍数, default=2.0
        tp_atr (float):       止盈 ATR 倍数, default=3.0
        cooldown_bars (int):  平仓后冷却, default=5
    """
    params = {
        'rsi_period': 14,
        'rsi_oversold': 30.0,
        'rsi_overbought': 70.0,
        'atr_period': 14,
        'sl_atr': 2.0,
        'tp_atr': 3.0,
        'cooldown_bars': 5,
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
        # RSI 需 period+1 个 close, ATR 需 period+1 个 bar
        # 取最大 + 安全余量 + 5
        cache_size = max(p['rsi_period'] * 3, p['atr_period'] * 3) + 20
        self._bars = deque(maxlen=cache_size)
        self._min_bars = max(p['rsi_period'] + 5, p['atr_period'] + 5)

    def _compute_atr(self) -> float:
        """Wilder 平滑 ATR(14) 最近值"""
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

    def _compute_rsi(self) -> float:
        """Wilder 平滑 RSI(14) 最近值"""
        n = len(self._bars)
        period = self.params['rsi_period']
        if n < period + 2:
            return float('nan')
        closes = np.array([b['close'] for b in self._bars], dtype=np.float64)
        deltas = np.diff(closes)
        gains = np.maximum(deltas, 0.0)
        losses = np.maximum(-deltas, 0.0)
        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))
        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        if avg_loss < 1e-12:
            return 100.0 if avg_gain > 1e-12 else 50.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    def on_bar(self, bar: dict) -> Signal | None:
        # 1. 缓存
        self._bars.append(bar)
        self._bar_count += 1

        # 2. 冷却
        if self._cooldown > 0:
            self._cooldown -= 1
            return None

        # 3. 持仓检查: 由 PaperEngine 负责
        #    (信号 → 持仓时跳过新开仓, SL/TP 触发由 _check_exit 处理)
        #    策略不需要 self.position 属性, 直接返回信号

        # 4. Warmup
        if len(self._bars) < self._min_bars:
            return None

        # 5. 算指标
        rsi = self._compute_rsi()
        atr = self._compute_atr()
        if np.isnan(rsi) or np.isnan(atr) or atr <= 0:
            return None
        self.last_atr = atr

        # 6. 信号
        rsi_os = self.params['rsi_oversold']
        rsi_ob = self.params['rsi_overbought']

        direction = 0
        if rsi < rsi_os:
            direction = 1  # 做多 (超卖反弹)
        elif rsi > rsi_ob:
            direction = -1  # 做空 (超买回落)
        else:
            return None

        # 7. 置信度: 反向越极端越高, 阈值处=0.5, 距阈值 20 越极端→0.9
        if direction == 1:
            # rsi=10 → conf=0.9, rsi=30 → conf=0.5
            conf = 0.5 + 0.4 * max(0.0, (rsi_os - rsi) / 20.0)
        else:
            # rsi=70 → conf=0.5, rsi=90 → conf=0.9
            conf = 0.5 + 0.4 * max(0.0, (rsi - rsi_ob) / 20.0)
        conf = min(0.9, max(0.5, conf))

        # 8. SL/TP
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
            factor_scores={'rsi': round(rsi, 2), 'atr': round(atr, 2)},
            confidence=round(conf, 3),
        )
