"""
MACD-BB Strategy — MACD Histogram with Bollinger Band Width Filter

基于因子IC分析结果：
- MACD_HIST IC=+0.111（20步持有期）→ 主方向因子
- BB_WIDTH IC=-0.060 → 波动率过滤（宽带→看跌，窄带→看涨）

策略逻辑：
1. 方向：MACD_HIST>0 & 递增 → LONG；MACD_HIST<0 & 递减 → SHORT
2. 波动率过滤：BB_WIDTH > 80分位 → 只允许SHORT；BB_WIDTH < 20分位 → 只允许LONG
3. SL=2.0×ATR, TP=5.0×ATR（比之前宽，减少误止损）
4. CD=5（因子IC够强不需要长冷却）
"""

import logging
from collections import deque

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='macd_bb', timeframes=['H1'])
class MACDBBStrategy(BaseStrategy):
    """
    MACD-BB Strategy — H1 MACD Histogram with BB Width Filter

    Params:
        macd_fast (int): MACD快线周期, default=12
        macd_slow (int): MACD慢线周期, default=26
        macd_signal (int): MACD信号线周期, default=9
        bb_period (int): 布林带周期, default=20
        bb_std (float): 布林带标准差倍数, default=2.0
        atr_period (int): ATR周期, default=14
        sl_atr (float): 止损ATR倍数, default=2.0
        tp_atr (float): 止盈ATR倍数, default=5.0
        cooldown_bars (int): 交易后冷却bar数, default=5
        bb_percentile_high (int): BB宽度上限分位数, default=80
        bb_percentile_low (int): BB宽度下限分位数, default=20
    """
    params = {
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'bb_period': 20,
        'bb_std': 2.0,
        'atr_period': 14,
        'sl_atr': 2.0,
        'tp_atr': 5.0,
        'cooldown_bars': 5,
        'bb_percentile_high': 80,
        'bb_percentile_low': 20,
    }

    def __init__(self, name: str, symbol: str, timeframe: str = "H1"):
        super().__init__(name, symbol, timeframe)
        # 缓存最近100根bar用于指标计算
        self._bars = deque(maxlen=100)
        # BB宽度缓存，用于分位数计算
        self._bb_widths = deque(maxlen=20)
        p = self.params
        # 最小bar数：MACD(26+9+5=40) / BB(20*2=40) / ATR(14*2+5=33)
        self._min_bars = max(
            p['macd_slow'] + p['macd_signal'] + 5,
            p['bb_period'] * 2,
            p['atr_period'] * 2 + 5,
        )

    # ── 指标计算 ──────────────────────────────────────────

    @staticmethod
    def _vector_ema(values: np.ndarray, period: int) -> np.ndarray:
        """向量化EMA计算（标准EMA: multiplier = 2/(period+1)）"""
        n = len(values)
        result = np.full(n, np.nan)
        if n < period:
            return result
        multiplier = 2.0 / (period + 1.0)
        # SMA初始化
        result[period - 1] = float(np.mean(values[:period]))
        for i in range(period, n):
            result[i] = (values[i] - result[i - 1]) * multiplier + result[i - 1]
        return result

    def _compute_atr(self, closes: np.ndarray, highs: np.ndarray,
                     lows: np.ndarray, period: int) -> float:
        """Wilder's smoothed ATR"""
        n = len(closes)
        if n < period + 1:
            return np.nan
        tr = np.zeros(n - 1)
        for i in range(1, n):
            tr[i - 1] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        # Wilder's smoothing: initial = SMA, then recursive
        atr = float(np.mean(tr[:period]))
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
        return atr

    def _calc_indicators(self) -> dict:
        """从缓存bar计算所有指标，返回dict或空dict"""
        bars = list(self._bars)
        if len(bars) < self._min_bars:
            return {}

        closes = np.array([b['close'] for b in bars])
        highs = np.array([b['high'] for b in bars])
        lows = np.array([b['low'] for b in bars])
        p = self.params

        # ── MACD ──────────────────────────────────────────
        # ema12, ema26 → macd_line → ema9 → hist
        ema_fast = self._vector_ema(closes, p['macd_fast'])
        ema_slow = self._vector_ema(closes, p['macd_slow'])
        macd_line = ema_fast - ema_slow

        # 取macd_line有效段计算信号线
        first_macd = p['macd_slow'] - 1  # 第一个有效macd_line索引
        valid_macd = macd_line[first_macd:]  # 无NaN段
        signal_line = self._vector_ema(valid_macd, p['macd_signal'])
        hist = valid_macd - signal_line

        hist_cur = hist[-1] if len(hist) > 0 else np.nan
        hist_prev = hist[-2] if len(hist) > 1 else np.nan

        # ── 布林带 ────────────────────────────────────────
        bb_n = p['bb_period']
        if len(closes) >= bb_n:
            recent = closes[-bb_n:]
            sma = float(np.mean(recent))
            std = float(np.std(recent, ddof=0))
            bb_top = sma + p['bb_std'] * std
            bb_bot = sma - p['bb_std'] * std
            bb_width = bb_top - bb_bot
        else:
            sma, bb_top, bb_bot, bb_width = np.nan, np.nan, np.nan, np.nan

        # ── ATR ──────────────────────────────────────────
        atr = self._compute_atr(closes, highs, lows, p['atr_period'])

        return {
            'macd_hist': hist_cur,
            'macd_hist_prev': hist_prev,
            'sma': sma,
            'bb_top': bb_top,
            'bb_bot': bb_bot,
            'bb_width': bb_width,
            'atr': atr,
        }

    # ── 主入口 ────────────────────────────────────────────────

    def on_bar(self, bar: dict) -> Signal | None:
        self._bars.append(bar)
        self._bar_count += 1

        # 冷却计数
        if self._cooldown > 0:
            self._cooldown -= 1

        ind = self._calc_indicators()
        if not ind:
            return None

        hist_cur = ind['macd_hist']
        hist_prev = ind['macd_hist_prev']
        bb_width = ind['bb_width']
        atr = ind['atr']
        close = bar['close']
        p = self.params

        # 检查NaN
        if any(np.isnan(v) for v in (hist_cur, hist_prev, bb_width, atr)):
            return None

        # 更新BB宽度缓存（用于分位数）
        self._bb_widths.append(bb_width)

        # ── 冷却期检查 ──
        if self._cooldown > 0:
            return None

        # ── 波动率过滤（BB宽度分位数） ──
        if len(self._bb_widths) >= 20:
            bb_high = float(np.percentile(self._bb_widths, p['bb_percentile_high']))
            bb_low = float(np.percentile(self._bb_widths, p['bb_percentile_low']))
        else:
            bb_high = np.inf
            bb_low = -np.inf

        # BB_WIDTH > 80分位 → 只允许SHORT
        vola_only_short = bb_width >= bb_high
        # BB_WIDTH < 20分位 → 只允许LONG
        vola_only_long = bb_width <= bb_low

        # ── 方向判断 ──
        direction = 0
        hist_rising = hist_cur > hist_prev  # histogram递增

        # MACD_HIST > 0 且 递增 → LONG
        if hist_cur > 0 and hist_rising:
            direction = 1
        # MACD_HIST < 0 且 递减 → SHORT
        elif hist_cur < 0 and not hist_rising:
            direction = -1

        if direction == 0:
            return None

        # ── 波动率过滤 ──
        if direction == 1 and vola_only_short:
            return None
        if direction == -1 and vola_only_long:
            return None

        # 触发冷却
        self._cooldown = p['cooldown_bars']

        signal = Signal(
            strategy=self.name,
            symbol=self.symbol,
            direction=direction,
            strength=1.0,
            sl_atr=float(p['sl_atr']),
            tp_atr=float(p['tp_atr']),
            atr=atr,
            price=close,
            timestamp=bar.get('time', 0.0),
            meta={
                'macd_hist': round(hist_cur, 2),
                'hist_rising': hist_rising,
                'bb_width': round(bb_width, 2),
                'atr': round(atr, 2),
                'close': close,
            },
        )

        logger.info(
            f"[{self.name}] {'LONG' if direction == 1 else 'SHORT'} "
            f"hist={hist_cur:.2f}({'↑' if hist_rising else '↓'}) "
            f"bbw={bb_width:.2f} atr={atr:.2f} "
            f"sl={p['sl_atr']}x tp={p['tp_atr']}x"
        )
        return signal

    def on_init(self):
        """策略初始化"""
        self._bars.clear()
        self._bb_widths.clear()
        self._cooldown = 0
        self._bar_count = 0
        logger.info(f"[{self.name}] Initialized, min_bars={self._min_bars}")
