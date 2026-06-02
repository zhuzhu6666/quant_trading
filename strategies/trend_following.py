"""
Trend Following Strategy — M15 EMA Triple Alignment + ADX

纯趋势跟随策略:
  多头信号: EMA20 > EMA50 > EMA200 (三线多头排列) AND ADX(14) > 25
  空头信号: EMA20 < EMA50 < EMA200 (三线空头排列) AND ADX(14) > 25

持仓期间行为:
  - 每根 bar 末重新判断方向; 若方向反 → 立刻平仓 (close on signal_flip)
  - 不立即反向开仓, 等下一根 bar 的策略信号 (避免未来函数 & 反手滑点)

止损/止盈:
  - SL = 2.0 × ATR(14)
  - TP = 3.0 × ATR(14)
  - R:R = 1.5
  - Cooldown = 5 bars (平仓后)

Phase 7 Signal 扩展字段:
  - factor_scores: {"ema20_slope": ..., "adx": ..., "atr": ...}
  - regime: None (由 router 注入, 不在本策略内计算)
  - confidence: 0.7 (固定)

指标全部用 numpy vectorized 内部计算, 无外部 TA 库依赖.
"""

import logging
from collections import deque
from typing import Optional

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='trend_following', timeframes=['M15'])
class TrendFollowingStrategy(BaseStrategy):
    """
    M15 趋势跟随 — EMA 三线 + ADX 过滤

    Params:
        ema_fast (int):   快线 EMA 周期, default=20
        ema_mid (int):    中线 EMA 周期, default=50
        ema_slow (int):   慢线 EMA 周期, default=200
        adx_period (int): ADX 周期, default=14
        adx_thresh (float): ADX 阈值, default=25
        atr_period (int): ATR 周期, default=14
        sl_atr (float):   止损 ATR 倍数, default=2.0
        tp_atr (float):   止盈 ATR 倍数, default=3.0
        cooldown_bars (int): 平仓后冷却 bar 数, default=5
        confidence (float): 信号固定置信度, default=0.7
    """
    params = {
        'ema_fast': 20,
        'ema_mid': 50,
        'ema_slow': 200,
        'adx_period': 14,
        'adx_thresh': 25.0,
        'atr_period': 14,
        'sl_atr': 2.0,
        'tp_atr': 3.0,
        'cooldown_bars': 5,
        'confidence': 0.7,
    }

    def __init__(self, name: str, symbol: str, timeframe: str = "M15"):
        super().__init__(name, symbol, timeframe)
        # Bar 缓存: 需要装下 ema_slow=200 + ADX warmup (2*14=28) + ATR warmup
        # 取 max(200, 28) + 安全余量 → 250
        p = self.params
        cache_size = max(p['ema_slow'] * 2, p['adx_period'] * 4, p['atr_period'] * 3, 300)
        self._bars = deque(maxlen=cache_size)
        self._min_bars = max(
            p['ema_slow'] + 5,         # EMA200 + warmup
            p['adx_period'] * 2 + 5,   # ADX 需 2×period
            p['atr_period'] * 2 + 5,   # ATR 需 2×period
        )
        # 可选注入: 执行引擎引用 — 用于在 on_bar 内直接平仓
        # 测试代码可在 run() 之前赋值: strategy.engine = paper_trader.engine
        self.engine: Optional[object] = None

    # ── 指标计算 (vectorized) ──────────────────────────────────

    @staticmethod
    def _vector_ema(values: np.ndarray, period: int) -> np.ndarray:
        """
        标准 EMA: multiplier = 2 / (period + 1)
        使用循环实现 (Wilder's 风格在内部其实也是循环),
        但对外暴露可一次性产出完整 series (与 rolling 思路一致).
        """
        n = len(values)
        out = np.full(n, np.nan, dtype=np.float64)
        if n < period:
            return out
        k = 2.0 / (period + 1.0)
        # 初始 = SMA(period)
        out[period - 1] = float(np.mean(values[:period]))
        for i in range(period, n):
            out[i] = (values[i] - out[i - 1]) * k + out[i - 1]
        return out

    @staticmethod
    def _compute_atr(closes: np.ndarray, highs: np.ndarray,
                     lows: np.ndarray, period: int) -> float:
        """Wilder's 平滑 ATR (返回最新一根的标量值)"""
        n = len(closes)
        if n < period + 1:
            return float('nan')
        tr = np.empty(n - 1, dtype=np.float64)
        for i in range(1, n):
            tr[i - 1] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
        atr = float(np.mean(tr[:period]))
        for i in range(period, len(tr)):
            atr = (atr * (period - 1) + tr[i]) / period
        return float(atr)

    @staticmethod
    def _compute_adx(closes: np.ndarray, highs: np.ndarray,
                     lows: np.ndarray, period: int) -> float:
        """
        Wilder's ADX(period).
        需要至少 2*period 根 bar (period 初始化 + period 平滑 DX).
        """
        n = len(closes)
        if n < period * 2:
            return float('nan')

        tr = np.empty(n - 1, dtype=np.float64)
        plus_dm = np.empty(n - 1, dtype=np.float64)
        minus_dm = np.empty(n - 1, dtype=np.float64)
        for i in range(1, n):
            up = highs[i] - highs[i - 1]
            dn = lows[i - 1] - lows[i]
            tr[i - 1] = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            )
            plus_dm[i - 1] = up if (up > dn and up > 0) else 0.0
            minus_dm[i - 1] = dn if (dn > up and dn > 0) else 0.0

        # Wilder 平滑 TR / +DM / -DM
        tr_s = float(np.mean(tr[:period]))
        pdi_s = float(np.mean(plus_dm[:period]))
        ndi_s = float(np.mean(minus_dm[:period]))

        # 计算 DX 序列
        dx_arr = np.empty(len(tr) - period, dtype=np.float64)
        idx = 0
        for i in range(period, len(tr)):
            tr_s = (tr_s * (period - 1) + tr[i]) / period
            pdi_s = (pdi_s * (period - 1) + plus_dm[i]) / period
            ndi_s = (ndi_s * (period - 1) + minus_dm[i]) / period
            pdi_v = 100.0 * pdi_s / tr_s if tr_s > 1e-12 else 0.0
            ndi_v = 100.0 * ndi_s / tr_s if tr_s > 1e-12 else 0.0
            s = pdi_v + ndi_v
            dx_arr[idx] = 100.0 * abs(pdi_v - ndi_v) / s if s > 1e-12 else 0.0
            idx += 1

        if len(dx_arr) < period:
            return float('nan')
        # ADX = DX 的 Wilder 平滑
        adx = float(np.mean(dx_arr[:period]))
        for i in range(period, len(dx_arr)):
            adx = (adx * (period - 1) + dx_arr[i]) / period
        return float(adx)

    def _calc_indicators(self) -> dict:
        bars = list(self._bars)
        if len(bars) < self._min_bars:
            return {}

        closes = np.array([b['close'] for b in bars], dtype=np.float64)
        highs = np.array([b['high'] for b in bars], dtype=np.float64)
        lows = np.array([b['low'] for b in bars], dtype=np.float64)
        p = self.params

        # 三条 EMA
        ema_fast = self._vector_ema(closes, p['ema_fast'])
        ema_mid = self._vector_ema(closes, p['ema_mid'])
        ema_slow = self._vector_ema(closes, p['ema_slow'])

        ema_fast_v = float(ema_fast[-1])
        ema_mid_v = float(ema_mid[-1])
        ema_slow_v = float(ema_slow[-1])
        ema_slow_prev = float(ema_slow[-2]) if len(ema_slow) >= 2 else ema_slow_v

        # ADX
        adx = self._compute_adx(closes, highs, lows, p['adx_period'])

        # ATR
        atr = self._compute_atr(closes, highs, lows, p['atr_period'])

        # EMA20 斜率: (ema20[t] - ema20[t-1]) / atr  (用 ATR 标准化, 跨价格可比)
        ema_fast_prev = float(ema_fast[-2]) if len(ema_fast) >= 2 else ema_fast_v
        if atr > 1e-12 and not np.isnan(atr):
            ema20_slope = (ema_fast_v - ema_fast_prev) / atr
        else:
            ema20_slope = 0.0

        return {
            'ema20': ema_fast_v,
            'ema50': ema_mid_v,
            'ema200': ema_slow_v,
            'ema200_prev': ema_slow_prev,
            'adx': adx,
            'atr': atr,
            'ema20_slope': float(ema20_slope),
        }

    def _alignment(self, ind: dict) -> int:
        """
        返回方向:
           1  = 多头排列 (EMA20 > EMA50 > EMA200)
          -1  = 空头排列 (EMA20 < EMA50 < EMA200)
           0  = 无明确排列 (震荡 / 缠绕)
        """
        e20, e50, e200 = ind['ema20'], ind['ema50'], ind['ema200']
        if e20 > e50 > e200:
            return 1
        if e20 < e50 < e200:
            return -1
        return 0

    def _position_direction(self) -> int:
        """
        读取当前持仓方向. 优先用 core.state 单例 (paper_engine 同步过),
        回退到 self.engine.position (若注入).
        """
        try:
            from core.state import state as _state
            if _state.position and _state.position.direction != 0 and _state.position.volume > 0:
                # 仅匹配本策略对应的 symbol
                if _state.position.symbol == self.symbol:
                    return int(_state.position.direction)
        except Exception:
            pass
        if self.engine is not None:
            pos = getattr(self.engine, 'position', None)
            if pos and pos.direction != 0 and pos.volume > 0:
                if pos.symbol == self.symbol:
                    return int(pos.direction)
        return 0

    # ── 主入口 ────────────────────────────────────────────────

    def on_bar(self, bar: dict) -> Optional[Signal]:
        self._bars.append(bar)
        self._bar_count += 1
        if self._cooldown > 0:
            self._cooldown -= 1

        ind = self._calc_indicators()
        if not ind:
            return None

        # 指标快照 (供风控/熔断)
        self.last_indicators = ind
        self.last_atr = ind.get('atr')

        atr = ind['atr']
        adx = ind['adx']
        # 任何 NaN 直接跳过
        if not (np.isfinite(atr) and np.isfinite(adx) and
                np.isfinite(ind['ema20']) and np.isfinite(ind['ema50']) and
                np.isfinite(ind['ema200'])):
            return None

        close = bar['close']
        p = self.params

        desired = self._alignment(ind)            # 期望方向
        adx_ok = adx > p['adx_thresh']
        pos_dir = self._position_direction()      # 当前持仓方向

        # ── 持仓中: 每根 bar 末重新判断 ──
        if pos_dir != 0:
            # 方向反 → 立刻平仓 (close on signal_flip), 不开新仓
            if desired == -pos_dir:
                self._close_position(bar, reason='signal_flip')
                self._cooldown = p['cooldown_bars']
                return None
            # 方向同 → 继续持有, 不做事
            return None

        # ── 空仓: 冷却期 + 条件判断 → 开仓 ──
        if self._cooldown > 0:
            return None
        if desired == 0 or not adx_ok:
            return None

        signal = Signal(
            strategy=self.name,
            symbol=self.symbol,
            direction=desired,
            strength=1.0,
            sl_atr=float(p['sl_atr']),
            tp_atr=float(p['tp_atr']),
            atr=float(atr),
            price=float(close),
            timestamp=float(bar.get('time', 0.0)),
            meta={
                'ema20': round(ind['ema20'], 4),
                'ema50': round(ind['ema50'], 4),
                'ema200': round(ind['ema200'], 4),
                'adx': round(adx, 2),
                'atr': round(atr, 4),
                'alignment': 'LONG' if desired == 1 else 'SHORT',
                'close': float(close),
            },
            # ── Phase 7 扩展字段 ──
            factor_scores={
                'ema20_slope': round(ind['ema20_slope'], 4),
                'adx': round(adx, 2),
                'atr': round(atr, 4),
            },
            regime=None,                    # 由 router 注入
            confidence=float(p['confidence']),
        )
        self._cooldown = p['cooldown_bars']

        logger.info(
            f"[{self.name}] {'LONG' if desired == 1 else 'SHORT'} "
            f"close={close:.2f} ema20={ind['ema20']:.2f} ema50={ind['ema50']:.2f} "
            f"ema200={ind['ema200']:.2f} adx={adx:.1f} atr={atr:.2f} "
            f"sl={p['sl_atr']}x tp={p['tp_atr']}x"
        )
        return signal

    # ── 内部工具 ──────────────────────────────────────────────

    def _close_position(self, bar: dict, reason: str = 'signal_flip') -> None:
        """
        持仓方向反转时, 主动调用 engine._close() 在本 bar close 价平仓.
        若没有 engine 引用, 只更新本地冷却 + 记日志 (不强制, 留作退化路径).
        """
        if self.engine is None:
            logger.debug(
                f"[{self.name}] direction flip but engine not wired; "
                f"skipping manual close (SL/TP will catch it next bar)."
            )
            return
        try:
            pos = getattr(self.engine, 'position', None)
            if pos is None or pos.direction == 0:
                return
            self.engine._close(
                fill_price=bar['close'],
                reason=reason,
                bar_time=bar.get('time', 0.0),
            )
        except Exception as e:
            logger.warning(f"[{self.name}] manual close failed: {e}")

    # ── 生命周期 ──────────────────────────────────────────────

    def on_init(self):
        self._bars.clear()
        self._cooldown = 0
        self._bar_count = 0
        self.last_indicators = {}
        self.last_atr = None
        logger.info(f"[{self.name}] Initialized, min_bars={self._min_bars}")
