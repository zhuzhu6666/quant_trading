"""
Multi-Factor M15 Strategy — 多因子投票策略

基于因子IC分析结果（M15周期）：
- di_spread IC=+0.021 → vote1: di_spread > 0 = bullish
- rsi_14 IC=+0.012  → vote2: rsi > 50 = bullish
- stoch_k IC=+0.012 → vote3: stoch_k > 50 = bullish

策略逻辑（投票制）：
1. 三个因子投票，至少2票同意才开仓
2. MACD_HIST反向过滤：MACD_HIST<0才允许LONG，>0才允许SHORT
   （小周期MACD_HIST为负IC，所以反向使用）
3. BB_WIDTH > 80分位 → 波动太大，跳过
4. SL=2.0×ATR, TP=3.0×ATR, CD=3
"""

import logging
from collections import deque
from datetime import datetime

import numpy as np

from strategy.base import BaseStrategy, Signal
from strategy.registry import strategy_registry

logger = logging.getLogger(__name__)


@strategy_registry.register(name='multi_factor_m15', timeframes=['M15'])
class MultiFactorM15Strategy(BaseStrategy):
    """
    M15多因子投票策略

    Params:
        di_thresh (float): DI spread阈值, default=0
        rsi_thresh (float): RSI阈值, default=50
        stoch_thresh (float): Stochastic阈值, default=50
        votes_needed (int): 最少需要的票数, default=2
        di_period (int): DI周期, default=14
        rsi_period (int): RSI周期, default=14
        stoch_period (int): Stochastic周期, default=14
        macd_fast (int): MACD快线周期, default=12
        macd_slow (int): MACD慢线周期, default=26
        macd_signal (int): MACD信号线周期, default=9
        bb_period (int): 布林带周期, default=20
        bb_std (float): 布林带标准差倍数, default=2.0
        atr_period (int): ATR周期, default=14
        sl_atr (float): 止损ATR倍数, default=2.0
        tp_atr (float): 止盈ATR倍数, default=3.0
        cooldown_bars (int): 交易后冷却bar数, default=3
        bb_percentile (int): BB宽度分位数阈值, default=80
    """
    params = {
        'di_thresh': 0,
        'rsi_thresh': 50,
        'stoch_thresh': 50,
        'votes_needed': 2,
        'di_period': 14,
        'rsi_period': 14,
        'stoch_period': 14,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'bb_period': 20,
        'bb_std': 2.0,
        'atr_period': 14,
        'sl_atr': 2.0,
        'tp_atr': 3.0,
        'cooldown_bars': 3,
        'bb_percentile': 80,
        # ── 事件 + 波动率过滤（默认关，可由 main.py 启用）──
        'enable_nfp_skip': False,
        'nfp_skip_days': 1,
        'enable_dual_event_skip': False,
        'enable_fomc_boost': False,
        'fomc_boost_mult': 1.5,
        'enable_gvz_gate': False,
        'gvz_drop_pct': -2.0,  # GVZ 日变化 < 此值则跳过
    }

    def __init__(self, name: str, symbol: str, timeframe: str = "M15"):
        super().__init__(name, symbol, timeframe)
        # 缓存最近100根bar用于指标计算
        self._bars = deque(maxlen=100)
        # BB宽度缓存，用于分位数计算
        self._bb_widths = deque(maxlen=20)
        p = self.params
        # 最小bar数：MACD(26+9+5=40) 最消耗数据
        self._min_bars = max(
            p['macd_slow'] + p['macd_signal'] + 5,
            p['di_period'] * 2 + 5,
            p['rsi_period'] * 2,
            p['stoch_period'] + 5,
            p['bb_period'] * 2,
            p['atr_period'] * 2 + 5,
        )
        # 事件缓存（启动时一次性加载）
        self._nfp_window: set[str] = set()
        self._fomc_dates: set[str] = set()
        self._cpi_dates: set[str] = set()
        self._gvz_series: dict[str, float] = {}
        self._load_news_cache()

    # ── 通用指标计算 ──────────────────────────────────────────

    @staticmethod
    def _vector_ema(values: np.ndarray, period: int) -> np.ndarray:
        """向量化EMA计算（标准EMA: multiplier = 2/(period+1)）"""
        n = len(values)
        result = np.full(n, np.nan)
        if n < period:
            return result
        multiplier = 2.0 / (period + 1.0)
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

    def _compute_di_spread(self, closes: np.ndarray, highs: np.ndarray,
                           lows: np.ndarray, period: int) -> float:
        """
        计算 DI Spread = +DI - -DI

        +DI 和 -DI 基于 Wilder's 平滑的 Directional Movement 指标
        需要至少 2×period 根bar才能稳定
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

        # Wilder's平滑: 初始SMA，然后递归
        tr_s = float(np.mean(tr[:period]))
        pdi_s = float(np.mean(plus_dm[:period]))
        ndi_s = float(np.mean(minus_dm[:period]))

        for i in range(period, len(tr)):
            tr_s = (tr_s * (period - 1) + tr[i]) / period
            pdi_s = (pdi_s * (period - 1) + plus_dm[i]) / period
            ndi_s = (ndi_s * (period - 1) + minus_dm[i]) / period

        # 转成百分比形式
        pdi = 100.0 * pdi_s / tr_s if tr_s > 1e-12 else 0.0
        ndi = 100.0 * ndi_s / tr_s if tr_s > 1e-12 else 0.0

        return pdi - ndi

    def _compute_stoch_k(self, closes: np.ndarray, highs: np.ndarray,
                         lows: np.ndarray, period: int) -> float:
        """计算Stochastic %K"""
        n = len(closes)
        if n < period:
            return np.nan
        recent_high = float(np.max(highs[-period:]))
        recent_low = float(np.min(lows[-period:]))
        close_current = closes[-1]
        if recent_high - recent_low < 1e-12:
            return 50.0
        return 100.0 * (close_current - recent_low) / (recent_high - recent_low)

    # ── 全部指标计算 ──────────────────────────────────────────

    def _calc_indicators(self) -> dict:
        """从缓存bar计算所有指标，返回dict或空dict"""
        bars = list(self._bars)
        if len(bars) < self._min_bars:
            return {}

        closes = np.array([b['close'] for b in bars])
        highs = np.array([b['high'] for b in bars])
        lows = np.array([b['low'] for b in bars])
        p = self.params

        # ── DI Spread ──
        di_spread = self._compute_di_spread(closes, highs, lows, p['di_period'])

        # ── RSI ──
        rsi = self._compute_rsi(closes, p['rsi_period'])

        # ── Stochastic %K ──
        stoch_k = self._compute_stoch_k(closes, highs, lows, p['stoch_period'])

        # ── MACD ──
        ema_fast = self._vector_ema(closes, p['macd_fast'])
        ema_slow = self._vector_ema(closes, p['macd_slow'])
        macd_line = ema_fast - ema_slow
        first_macd = p['macd_slow'] - 1
        valid_macd = macd_line[first_macd:]
        signal_line = self._vector_ema(valid_macd, p['macd_signal'])
        macd_hist = valid_macd - signal_line
        hist_cur = macd_hist[-1] if len(macd_hist) > 0 else np.nan

        # ── 布林带 → BB Width ──
        bb_n = p['bb_period']
        if len(closes) >= bb_n:
            recent = closes[-bb_n:]
            sma = float(np.mean(recent))
            std = float(np.std(recent, ddof=0))
            bb_width = (sma + p['bb_std'] * std) - (sma - p['bb_std'] * std)
        else:
            bb_width = np.nan

        # ── ATR ──
        atr = self._compute_atr(closes, highs, lows, p['atr_period'])

        return {
            'di_spread': di_spread,
            'rsi': rsi,
            'stoch_k': stoch_k,
            'macd_hist': hist_cur,
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

        # 写入指标快照（供风控层使用）
        self.last_indicators = ind
        self.last_atr = ind.get('atr')

        di_spread = ind['di_spread']
        rsi = ind['rsi']
        stoch_k = ind['stoch_k']
        macd_hist = ind['macd_hist']
        bb_width = ind['bb_width']
        atr = ind['atr']
        close = bar['close']
        p = self.params

        # 检查NaN
        if any(np.isnan(v) for v in (di_spread, rsi, stoch_k, macd_hist, bb_width, atr)):
            return None

        # 更新BB宽度缓存（用于分位数）
        self._bb_widths.append(bb_width)

        # ── 冷却期检查 ──
        if self._cooldown > 0:
            return None

        # ── 波动率过滤：BB_WIDTH > 80分位 → 跳过 ──
        if len(self._bb_widths) >= 20:
            bb_thresh = float(np.percentile(self._bb_widths, p['bb_percentile']))
            if bb_width >= bb_thresh:
                return None

        # ── 投票系统 ──
        votes_long = 0
        votes_short = 0

        # Vote 1: di_spread > 0 = bullish, < 0 = bearish
        if di_spread > p['di_thresh']:
            votes_long += 1
        elif di_spread < -p['di_thresh']:
            votes_short += 1

        # Vote 2: rsi > thresh = bullish, < thresh = bearish
        if rsi > p['rsi_thresh']:
            votes_long += 1
        elif rsi < p['rsi_thresh']:
            votes_short += 1

        # Vote 3: stoch_k > thresh = bullish, < thresh = bearish
        if stoch_k > p['stoch_thresh']:
            votes_long += 1
        elif stoch_k < p['stoch_thresh']:
            votes_short += 1

        # 决定方向：至少N票
        direction = 0
        if votes_long >= p['votes_needed']:
            direction = 1
        elif votes_short >= p['votes_needed']:
            direction = -1

        if direction == 0:
            return None

        # ── MACD_HIST反向过滤 ──
        # 小周期MACD_HIST为负IC(-0.025)
        # macd_hist < 0 才允许LONG, macd_hist > 0 才允许SHORT
        if direction == 1 and macd_hist > 0:
            return None
        if direction == -1 and macd_hist < 0:
            return None

        # ── 事件 / 波动率过滤 ──
        bar_date_str = datetime.utcfromtimestamp(bar.get('time', 0.0)).strftime("%Y-%m-%d")

        # NFP 前后 ±Nd 跳过
        if p.get('enable_nfp_skip', False) and bar_date_str in self._nfp_window:
            return None

        # FOMC+CPI 同周跳过（开仓日 ±3 天内有 FOMC 且有 CPI）
        if p.get('enable_dual_event_skip', False) and self._fomc_dates and self._cpi_dates:
            from datetime import datetime as _dt, timedelta
            bd = _dt.strptime(bar_date_str, "%Y-%m-%d").date()
            has_fomc_week = any(
                (bd - _dt.strptime(d, "%Y-%m-%d").date()).days in range(-3, 4)
                for d in self._fomc_dates
            )
            has_cpi_week = any(
                (bd - _dt.strptime(d, "%Y-%m-%d").date()).days in range(-3, 4)
                for d in self._cpi_dates
            )
            if has_fomc_week and has_cpi_week:
                return None

        # GVZ-gate：黄金波动率日变化 < 阈值（平静日跳过）
        if p.get('enable_gvz_gate', False) and self._gvz_series:
            try:
                from data.news_cache import daily_change_pct
                gvz_chg = daily_change_pct(self._gvz_series, bar_date_str)
                if gvz_chg is not None and gvz_chg < p.get('gvz_drop_pct', -2.0):
                    return None
            except Exception:
                pass

        # FOMC 决议日 boost（仓位倍数）
        position_size_mult = 1.0
        if p.get('enable_fomc_boost', False) and bar_date_str in self._fomc_dates:
            position_size_mult = float(p.get('fomc_boost_mult', 1.5))

        # 触发冷却
        self._cooldown = p['cooldown_bars']

        signal = Signal(
            strategy=self.name,
            symbol=self.symbol,
            direction=direction,
            strength=position_size_mult,  # 用 strength 字段传递仓位倍数
            sl_atr=float(p['sl_atr']),
            tp_atr=float(p['tp_atr']),
            atr=atr,
            price=close,
            timestamp=bar.get('time', 0.0),
            meta={
                'di_spread': round(di_spread, 2),
                'rsi': round(rsi, 1),
                'stoch_k': round(stoch_k, 1),
                'macd_hist': round(macd_hist, 2),
                'bb_width': round(bb_width, 2),
                'atr': round(atr, 2),
                'votes_long': votes_long,
                'votes_short': votes_short,
                'close': close,
            },
        )

        logger.info(
            f"[{self.name}] {'LONG' if direction == 1 else 'SHORT'} "
            f"di={di_spread:.1f} rsi={rsi:.1f} stoch={stoch_k:.1f} "
            f"votes({votes_long}/{votes_short}) "
            f"hist={macd_hist:.2f} bbw={bb_width:.2f} atr={atr:.2f} "
            f"sl={p['sl_atr']}x tp={p['tp_atr']}x"
        )
        return signal

    def on_init(self):
        """策略初始化"""
        self._bars.clear()
        self._bb_widths.clear()
        self._cooldown = 0
        self._bar_count = 0
        self._load_news_cache()
        logger.info(f"[{self.name}] Initialized, min_bars={self._min_bars}")

    def _load_news_cache(self):
        """从 SQLite 加载事件日历和 GVZ（缺失/异常时静默）"""
        try:
            from data.news_cache import (
                load_nfp_dates, load_event_dates, load_gvz_series,
                expand_to_window, daily_change_pct,
            )
            from data.store import DataStore  # 复用同库路径逻辑
            from pathlib import Path
            db_path = "data/market_data.db"
            if not Path(db_path).exists():
                return
            p = self.params
            if p.get('enable_nfp_skip', False):
                nfp = load_nfp_dates(db_path)
                self._nfp_window = expand_to_window(nfp, p.get('nfp_skip_days', 1))
            if p.get('enable_dual_event_skip', False) or p.get('enable_fomc_boost', False):
                self._fomc_dates = load_event_dates(db_path, 'FOMC')
                self._cpi_dates = load_event_dates(db_path, 'CPI')
            if p.get('enable_gvz_gate', False):
                self._gvz_series = load_gvz_series(db_path)
        except Exception as e:
            logger.debug(f"[{self.name}] news cache load skipped: {e}")
