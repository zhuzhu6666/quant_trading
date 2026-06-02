"""
Gold Momentum Strategy — H1 Momentum with ADX/RSI/SMA

策略逻辑:
- 时间框架: H1
- 入场: price > SMA(20) AND ADX(14)>25 AND RSI(14)>50 → LONG
        price < SMA(20) AND ADX(14)>25 AND RSI(14)<50 → SHORT
- 止损: 1.5×ATR(14)
- 止盈: 4.5×ATR(14)
- 冷却: 20根bar
"""

import logging
from collections import deque

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='gold_momentum', timeframes=['H1'])
class GoldMomentumStrategy(BaseStrategy):
    """
    黄金动量策略 — H1 Momentum

    Params:
        lookback_sma (int): SMA周期, default=20
        adx_period (int): ADX周期, default=14
        adx_thresh (int): ADX阈值, default=25
        rsi_period (int): RSI周期, default=14
        rsi_thresh (int): RSI中位线, default=50
        atr_period (int): ATR周期, default=14
        sl_atr (float): 止损ATR倍数, default=1.5
        tp_atr (float): 止盈ATR倍数, default=4.5
        cooldown_bars (int): 交易后冷却bar数, default=20
    """
    params = {
        'lookback_sma': 20,
        'adx_period': 14,
        'adx_thresh': 25,
        'rsi_period': 14,
        'rsi_thresh': 50,
        'atr_period': 14,
        'sl_atr': 1.5,
        'tp_atr': 4.5,
        'cooldown_bars': 20,
    }

    def __init__(self, name: str, symbol: str, timeframe: str = "H1"):
        super().__init__(name, symbol, timeframe)
        # 缓存最近50根bar用于指标计算
        self._bars = deque(maxlen=50)
        # 预热所需的最少bar数（SMA20 + ADX/RSI/ATR都需要足够数据）
        p = self.params
        self._min_bars = max(
            p['lookback_sma'],
            p['atr_period'] * 2 + 5,   # ADX需要2×period才能有稳定值
            p['rsi_period'] * 2,
        )

    # ── 核心指标计算 ──────────────────────────────────────────

    def _compute_sma(self, closes: np.ndarray, period: int) -> float:
        if len(closes) < period:
            return np.nan
        return float(np.mean(closes[-period:]))

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
        # Wilder's smoothing: initial = SMA, then EMA-like
        atr = float(np.mean(tr[:period]))
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
        return atr

    def _compute_rsi(self, closes: np.ndarray, period: int) -> float:
        """Wilder's smoothed RSI"""
        n = len(closes)
        if n < period + 1:
            return np.nan
        deltas = np.diff(closes)
        gains = np.maximum(deltas, 0)
        losses = np.maximum(-deltas, 0)

        avg_gain = float(np.mean(gains[:period]))
        avg_loss = float(np.mean(losses[:period]))

        for i in range(period, len(gains)):
            avg_gain = (avg_gain * (period - 1) + gains[i]) / period
            avg_loss = (avg_loss * (period - 1) + losses[i]) / period

        if avg_loss < 1e-12:
            return 100.0 if avg_gain > 1e-12 else 50.0
        rs = avg_gain / avg_loss
        return float(100.0 - 100.0 / (1.0 + rs))

    def _compute_adx(self, closes: np.ndarray, highs: np.ndarray,
                     lows: np.ndarray, period: int) -> float:
        """
        Wilder's ADX(14)

        ADX需要2×period根bar才能稳定: period根for平滑初始化, period根for DX平滑
        """
        n = len(closes)
        if n < period * 2:
            return np.nan

        # 计算TR, +DM, -DM
        tr = np.zeros(n - 1)
        plus_dm = np.zeros(n - 1)
        minus_dm = np.zeros(n - 1)
        for i in range(1, n):
            tr[i - 1] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]
            plus_dm[i - 1] = up_move if up_move > down_move and up_move > 0 else 0.0
            minus_dm[i - 1] = down_move if down_move > up_move and down_move > 0 else 0.0

        # Wilder's平滑: TR_s, +DM_s, -DM_s
        tr_s = float(np.mean(tr[:period]))
        pdi_s = float(np.mean(plus_dm[:period]))
        ndi_s = float(np.mean(minus_dm[:period]))

        # 收集DX值
        dx_list = []
        p = period
        for i in range(p, len(tr)):
            tr_s = (tr_s * (p - 1) + tr[i]) / p
            pdi_s = (pdi_s * (p - 1) + plus_dm[i]) / p
            ndi_s = (ndi_s * (p - 1) + minus_dm[i]) / p

            pdi_val = 100.0 * pdi_s / tr_s if tr_s > 1e-12 else 0.0
            ndi_val = 100.0 * ndi_s / tr_s if tr_s > 1e-12 else 0.0
            di_sum = pdi_val + ndi_val
            dx = 100.0 * abs(pdi_val - ndi_val) / di_sum if di_sum > 1e-12 else 0.0
            dx_list.append(dx)

        # ADX = DX的Wilder平滑 (初始=SMA)
        if len(dx_list) < p:
            return np.nan
        adx = float(np.mean(dx_list[:p]))
        for i in range(p, len(dx_list)):
            adx = (adx * (p - 1) + dx_list[i]) / p
        return adx

    def _calc_indicators(self) -> dict:
        """从缓存bar计算所有指标，返回dict或空dict"""
        bars = list(self._bars)
        if len(bars) < self._min_bars:
            return {}

        closes = np.array([b['close'] for b in bars])
        highs = np.array([b['high'] for b in bars])
        lows = np.array([b['low'] for b in bars])
        p = self.params

        sma = self._compute_sma(closes, p['lookback_sma'])
        atr = self._compute_atr(closes, highs, lows, p['atr_period'])
        rsi = self._compute_rsi(closes, p['rsi_period'])
        adx = self._compute_adx(closes, highs, lows, p['adx_period'])

        return {
            'sma': sma,
            'atr': atr,
            'rsi': rsi,
            'adx': adx,
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

        close = bar['close']
        sma = ind['sma']
        atr = ind['atr']
        rsi = ind['rsi']
        adx = ind['adx']

        # 检查NaN
        if any(np.isnan(v) for v in (sma, atr, rsi, adx)):
            return None

        p = self.params

        # 冷却期检查
        if self._cooldown > 0:
            return None

        # 方向判断
        direction = 0
        if close > sma and adx > p['adx_thresh'] and rsi > p['rsi_thresh']:
            direction = 1  # LONG
        elif close < sma and adx > p['adx_thresh'] and rsi < p['rsi_thresh']:
            direction = -1  # SHORT

        if direction == 0:
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
                'sma': round(sma, 1),
                'atr': round(atr, 2),
                'rsi': round(rsi, 1),
                'adx': round(adx, 1),
                'close': close,
            },
        )

        logger.info(
            f"[{self.name}] {'LONG' if direction == 1 else 'SHORT'} "
            f"close={close:.2f} sma={sma:.1f} adx={adx:.1f} rsi={rsi:.1f} "
            f"atr={atr:.2f} sl={p['sl_atr']}x tp={p['tp_atr']}x"
        )
        return signal

    def on_init(self):
        """策略初始化"""
        self._bars.clear()
        self._cooldown = 0
        self._bar_count = 0
        logger.info(f"[{self.name}] Initialized, min_bars={self._min_bars}")
