"""
regime.py — Market Regime Detector

Detects the current market regime from a single M15 bar plus a small window
of recent M15 history. Returns a multi-label dict of 8 booleans:

    TRENDING_UP, TRENDING_DOWN, RANGING, HIGH_VOL, LOW_VOL,
    NEWS_DAY, DXY_DRIVEN, SAFE_HAVEN

Inputs come from two sources:
  * in-memory M15 history (used to compute EMA / ADX / ATR / Bollinger)
  * SQLite at data/market_data.db (used to look up GVZ, VIX, DXY proxy,
    event calendar)

Missing data (e.g. VIX not pulled) is handled silently by returning False
for the dependent regime. No exceptions are raised on data gaps.

Conventions
-----------
* EMA / ADX / DI are computed on the M15 close series.
* ATR uses Wilder's smoothing (the same scheme ADX uses) on the M15 range.
* Bollinger width is (upper - lower) / middle over a 20-period window.
* Percentile ranks (30 / 50 / 100) are taken over the recent M15 ATR history
  with a 200-period lookback (or the available length if shorter).
* "Today" for the news / macro lookups is the ``date_str`` argument
  (format ``YYYY-MM-DD``); if it is not supplied we fall back to the bar's
  own timestamp.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (kept here so the detector is self-contained and easy to tune)
# ---------------------------------------------------------------------------

# EMA periods for trend detection
EMA_FAST: int = 50
EMA_SLOW: int = 200

# ADX configuration
ADX_PERIOD: int = 14
ADX_TREND_THRESHOLD: float = 25.0
ADX_RANGE_THRESHOLD: float = 20.0

# Bollinger / ATR windows
BB_PERIOD: int = 20
BB_STD: float = 2.0
ATR_PERIOD: int = 14
ATR_PERCENTILE_WINDOW: int = 200  # lookback for ATR percentile ranks

# Volatility thresholds
HIGH_VOL_ATR_PCTILE: float = 100.0   # ATR >= 100th percentile (i.e. highest)
LOW_VOL_ATR_PCTILE: float = 30.0
HIGH_VOL_GVZ: float = 20.0
LOW_VOL_GVZ: float = 12.0

# DXY correlation
DXY_CORR_LOOKBACK_DAYS: int = 20
DXY_CORR_THRESHOLD: float = 0.7

# Macro series names in macro_daily
GVZ_SERIES: str = "GVZCLS"
# VIX is not currently in the database; we still query it for forward
# compatibility and silently return False if absent.
VIX_SERIES: str = "VIXCLS"
# DXY itself is not in the database; we use the trade-weighted USD index
# (DTWEXBGS) as the proxy series. Field is named "DXY" only in the output
# flag — implementation is documented here.
DXY_SERIES: str = "DTWEXBGS"

# Event types that flag a news day
NEWS_EVENT_TYPES: tuple[str, ...] = ("FOMC", "NFP", "CPI")


# ---------------------------------------------------------------------------
# Pure technical-indicator helpers (numpy-vectorised, no Python loops)
# ---------------------------------------------------------------------------

def _ema(series: np.ndarray, period: int) -> np.ndarray:
    """Vectorised EMA using a pandas rolling ewm (alpha = 1/period).

    Returns an array the same length as ``series``; the first ``period-1``
    entries are NaN.
    """
    if len(series) == 0:
        return series.astype(float, copy=True)
    return pd.Series(series, dtype="float64").ewm(
        span=period, adjust=False, min_periods=period
    ).mean().to_numpy()


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    """Vectorised True Range."""
    prev_close = np.empty_like(close)
    prev_close[0] = np.nan
    prev_close[1:] = close[:-1]
    tr1 = high - low
    tr2 = np.abs(high - prev_close)
    tr3 = np.abs(low - prev_close)
    return np.nanmax(np.stack([tr1, tr2, tr3], axis=0), axis=0)


def _wilder_smooth(series: np.ndarray, period: int) -> np.ndarray:
    """Wilder's smoothing (the smoothing method used by ATR / ADX).

    Equivalent to an EMA with alpha = 1/period, but seeded with the SMA of
    the first ``period`` valid values. Returns an array the same length
    as ``series``; entries before the seed are NaN.
    """
    out = np.full_like(series, np.nan, dtype="float64")
    if len(series) < period:
        return out
    seed = np.nanmean(series[:period])
    out[period - 1] = seed
    for i in range(period, len(series)):
        out[i] = out[i - 1] + (series[i] - out[i - 1]) / period
    # NOTE: this inner loop is unavoidable for Wilder smoothing because
    # it is recursive by definition. All other calcs in this module stay
    # vectorised; only the smoothing seed loop uses Python.
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         period: int = ATR_PERIOD) -> np.ndarray:
    """ATR (Wilder) over ``high/low/close`` arrays."""
    tr = _true_range(high, low, close)
    return _wilder_smooth(tr, period)


def _adx(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         period: int = ADX_PERIOD) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ADX, +DI, -DI (Wilder) over ``high/low/close`` arrays.

    Returns three arrays the same length as the input; the first
    ``2 * period - 1`` entries of each are NaN.
    """
    n = len(close)
    if n < 2 * period:
        nan = np.full(n, np.nan, dtype="float64")
        return nan, nan, nan

    # Directional movement
    up_move = np.diff(high, prepend=high[0])
    down_move = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    tr = _true_range(high, low, close)

    # Wilder smoothing
    atr = _wilder_smooth(tr, period)
    smooth_plus = _wilder_smooth(plus_dm, period)
    smooth_minus = _wilder_smooth(minus_dm, period)

    # DI: replace any zero ATR with NaN to avoid div-by-zero
    safe_atr = np.where(atr == 0, np.nan, atr)
    plus_di = 100.0 * smooth_plus / safe_atr
    minus_di = 100.0 * smooth_minus / safe_atr

    dx_num = np.abs(plus_di - minus_di)
    dx_den = plus_di + minus_di
    dx = np.where(dx_den == 0, 0.0, 100.0 * dx_num / np.where(dx_den == 0, np.nan, dx_den))
    adx = _wilder_smooth(dx, period)

    return adx, plus_di, minus_di


def _bollinger(close: np.ndarray, period: int = BB_PERIOD,
               num_std: float = BB_STD) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Bollinger middle / upper / lower bands. Returns three arrays."""
    s = pd.Series(close, dtype="float64")
    mid = s.rolling(period, min_periods=period).mean().to_numpy()
    sd = s.rolling(period, min_periods=period).std(ddof=0).to_numpy()
    upper = mid + num_std * sd
    lower = mid - num_std * sd
    return mid, upper, lower


def _bb_width(close: np.ndarray, period: int = BB_PERIOD,
              num_std: float = BB_STD) -> np.ndarray:
    """Bollinger width as (upper - lower) / middle, vectorised."""
    mid, upper, lower = _bollinger(close, period, num_std)
    width = (upper - lower) / np.where(mid == 0, np.nan, mid)
    return width


# ---------------------------------------------------------------------------
# SQLite accessors
# ---------------------------------------------------------------------------

def _open(db_path: str) -> sqlite3.Connection:
    """Open a SQLite connection (read-only URI when possible)."""
    # ``mode=ro`` URIs are only honoured for real files; if the path is
    # relative, sqlite resolves it against CWD. Fall back to a plain
    # connect() on any error.
    try:
        uri = f"file:{db_path}?mode=ro"
        return sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError:
        return sqlite3.connect(db_path)


def _safe_scalar(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> float | None:
    """Run ``sql`` and return a single float column value, or None."""
    try:
        cur = conn.execute(sql, tuple(params))
        row = cur.fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])
    except Exception as exc:  # table missing, column missing, etc.
        logger.debug("regime: scalar query failed (%s): %s", sql, exc)
        return None


def _safe_dataframe(conn: sqlite3.Connection, sql: str,
                    params: Iterable = ()) -> pd.DataFrame:
    """Run ``sql`` and return a DataFrame, or empty on failure."""
    try:
        return pd.read_sql_query(sql, conn, params=tuple(params))
    except Exception as exc:
        logger.debug("regime: dataframe query failed (%s): %s", sql, exc)
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Main detector
# ---------------------------------------------------------------------------

@dataclass
class RegimeDetector:
    """Detect the current market regime from M15 bars + macro context.

    Parameters
    ----------
    atr_percentile_window:
        Number of recent M15 bars used to compute the ATR percentile
        ranks referenced by HIGH_VOL / LOW_VOL. ``200`` matches the
        spec; shorter windows are fine for live trading.
    bb_width_window:
        Window length for the rolling Bollinger-width percentile used by
        RANGING.
    """

    atr_percentile_window: int = ATR_PERCENTILE_WINDOW
    bb_width_window: int = 100

    # ----- public entry point --------------------------------------------

    def detect(
        self,
        bar: dict | None,
        history_bars: Sequence[dict],
        date_str: str | None = None,
        db_path: str = "data/market_data.db",
    ) -> dict[str, bool]:
        """Return the 8-key regime dictionary for the given bar.

        Parameters
        ----------
        bar:
            The current M15 bar (dict with ``time/open/high/low/close``).
            It is appended to ``history_bars`` for indicator calculation.
            May be ``None`` when only the historical context is needed.
        history_bars:
            Recent M15 bars (oldest first). Default-sized at 100, but
            anything >= ``max(EMA_SLOW, 2*ADX_PERIOD) + 1`` works.
        date_str:
            "YYYY-MM-DD" used to look up news / macro. If ``None`` we
            try to infer it from the bar's ``time`` field.
        db_path:
            Path to the SQLite database (default: ``data/market_data.db``).
        """
        flags: dict[str, bool] = {
            "TRENDING_UP": False,
            "TRENDING_DOWN": False,
            "RANGING": False,
            "HIGH_VOL": False,
            "LOW_VOL": False,
            "NEWS_DAY": False,
            "DXY_DRIVEN": False,
            "SAFE_HAVEN": False,
        }

        # 1. Build the price series and compute indicators
        closes, highs, lows, times = self._series(history_bars, bar)
        if closes is None or len(closes) < max(EMA_SLOW, 2 * ADX_PERIOD) + 1:
            logger.debug("regime: insufficient history (len=%s); returning all-False",
                         None if closes is None else len(closes))
            return flags

        ema_fast = _ema(closes, EMA_FAST)
        ema_slow = _ema(closes, EMA_SLOW)
        atr = _atr(highs, lows, closes, ATR_PERIOD)
        adx, plus_di, minus_di = _adx(highs, lows, closes, ADX_PERIOD)
        bb_w = _bb_width(closes, BB_PERIOD, BB_STD)

        cur_ema_fast = ema_fast[-1]
        cur_ema_slow = ema_slow[-1]
        cur_adx = adx[-1]
        cur_plus_di = plus_di[-1]
        cur_minus_di = minus_di[-1]
        cur_atr = atr[-1]
        cur_bb_w = bb_w[-1]

        # 2. Resolve "today" for the macro / event lookups
        today = self._resolve_date(date_str, bar, times)

        # 3. Trend regimes
        if self._valid(cur_ema_fast, cur_ema_slow, cur_adx,
                       cur_plus_di, cur_minus_di):
            if cur_ema_fast > cur_ema_slow and cur_adx > ADX_TREND_THRESHOLD \
                    and cur_plus_di > cur_minus_di:
                flags["TRENDING_UP"] = True
            if cur_ema_fast < cur_ema_slow and cur_adx > ADX_TREND_THRESHOLD \
                    and cur_minus_di > cur_plus_di:
                flags["TRENDING_DOWN"] = True

        # 4. RANGING
        if self._valid(cur_adx, cur_bb_w):
            bb_w_recent = bb_w[-self.bb_width_window:] if len(bb_w) >= self.bb_width_window else bb_w
            valid_bb = bb_w_recent[~np.isnan(bb_w_recent)]
            if valid_bb.size >= max(20, BB_PERIOD):
                bb_pctile = (np.searchsorted(np.sort(valid_bb), cur_bb_w) + 1) \
                            / (valid_bb.size + 1) * 100.0
            else:
                bb_pctile = np.nan
            if cur_adx < ADX_RANGE_THRESHOLD and self._valid(bb_pctile) \
                    and bb_pctile < 50.0:
                flags["RANGING"] = True

        # 5. Macro lookups (GVZ, VIX, events, DXY correlation)
        gvz = None
        vix = None
        is_news = False
        dxy_driven = False

        if today is not None:
            try:
                with _open(db_path) as conn:
                    gvz = _safe_scalar(
                        conn,
                        "SELECT value FROM macro_daily "
                        "WHERE series = ? AND date <= ? "
                        "ORDER BY date DESC LIMIT 1",
                        (GVZ_SERIES, today),
                    )
                    vix = _safe_scalar(
                        conn,
                        "SELECT value FROM macro_daily "
                        "WHERE series = ? AND date <= ? "
                        "ORDER BY date DESC LIMIT 1",
                        (VIX_SERIES, today),
                    )
                    ev_df = _safe_dataframe(
                        conn,
                        "SELECT 1 FROM events WHERE date = ? AND type IN "
                        "('FOMC','NFP','CPI') LIMIT 1",
                        (today,),
                    )
                    is_news = not ev_df.empty
                    dxy_driven = self._dxy_driven(conn, today, db_path, closes, times)
            except Exception as exc:
                # Last-resort: if SQLite is unreachable, keep flags False
                logger.warning("regime: DB lookup failed: %s", exc)

        # 6. Volatility regimes
        atr_recent = atr[-self.atr_percentile_window:] if len(atr) >= self.atr_percentile_window else atr
        valid_atr = atr_recent[~np.isnan(atr_recent)]
        if valid_atr.size >= 20 and self._valid(cur_atr):
            atr_pctile = (np.searchsorted(np.sort(valid_atr), cur_atr) + 1) \
                         / (valid_atr.size + 1) * 100.0
        else:
            atr_pctile = np.nan

        # HIGH_VOL: ATR at the very top of its recent range OR GVZ > 20
        if (self._valid(atr_pctile) and atr_pctile >= HIGH_VOL_ATR_PCTILE) \
                or (gvz is not None and gvz > HIGH_VOL_GVZ):
            flags["HIGH_VOL"] = True

        # LOW_VOL: ATR in the bottom 30% AND GVZ < 12
        if (self._valid(atr_pctile) and atr_pctile < LOW_VOL_ATR_PCTILE) \
                and (gvz is not None and gvz < LOW_VOL_GVZ):
            flags["LOW_VOL"] = True

        # 7. News / Safe-haven / DXY-driven
        if is_news:
            flags["NEWS_DAY"] = True
        if vix is not None and vix > 25.0:
            flags["SAFE_HAVEN"] = True
        if dxy_driven:
            flags["DXY_DRIVEN"] = True

        return flags

    # ----- internal helpers ----------------------------------------------

    @staticmethod
    def _series(history_bars: Sequence[dict],
                bar: dict | None) -> tuple[np.ndarray | None, ...]:
        """Convert bar dicts to numpy arrays.

        Tries to be robust to slightly different key casings and to
        missing fields: any unreadable bar is dropped.
        """
        rows: list[tuple] = []
        for b in (list(history_bars) + ([] if bar is None else [bar])):
            try:
                t = b.get("time") or b.get("timestamp") or b.get("t")
                o = b["open"]; h = b["high"]; l = b["low"]; c = b["close"]
            except (KeyError, TypeError):
                continue
            rows.append((t, float(o), float(h), float(l), float(c)))
        if not rows:
            empty = np.array([], dtype="float64")
            return None, empty, empty, empty, empty
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close"])
        return (
            df["close"].to_numpy(dtype="float64"),
            df["high"].to_numpy(dtype="float64"),
            df["low"].to_numpy(dtype="float64"),
            df["time"].tolist(),
        )

    @staticmethod
    def _resolve_date(date_str: str | None, bar: dict | None,
                      times: list) -> str | None:
        """Return a ``YYYY-MM-DD`` string, or None if it cannot be resolved."""
        if date_str:
            return str(date_str)[:10]
        # Try the current bar
        if isinstance(bar, dict):
            t = bar.get("time") or bar.get("timestamp") or bar.get("t")
            ds = _coerce_date(t)
            if ds is not None:
                return ds
        # Fall back to the most recent history time
        if times:
            return _coerce_date(times[-1])
        return None

    def _dxy_driven(self, conn: sqlite3.Connection, today: str,
                    db_path: str, closes: np.ndarray, times: list) -> bool:
        """Return True if the 20-day |corr(DXY, XAUUSD)| exceeds 0.7.

        Uses DTWEXBGS (trade-weighted USD) as the DXY proxy because the
        project does not currently store a DXY series.
        """
        # Pull ~30 days of DXY around ``today`` to be safe.
        dxy_df = _safe_dataframe(
            conn,
            "SELECT date, value FROM macro_daily "
            "WHERE series = ? AND date <= ? "
            "ORDER BY date DESC LIMIT 60",
            (DXY_SERIES, today),
        )
        if dxy_df.empty or len(dxy_df) < DXY_CORR_LOOKBACK_DAYS:
            return False

        dxy_df = dxy_df.sort_values("date").reset_index(drop=True)
        dxy_df = dxy_df.tail(DXY_CORR_LOOKBACK_DAYS)
        dxy_dates = set(dxy_df["date"].astype(str).tolist())

        # Build a daily XAUUSD close series aligned to those dates
        try:
            xau_df = pd.read_sql_query(
                "SELECT date(time) AS d, close FROM candles "
                "WHERE symbol_id = (SELECT id FROM symbols WHERE name LIKE 'XAUUSD%' LIMIT 1) "
                "AND timeframe = 'D1' AND date(time) <= ? "
                "ORDER BY time DESC LIMIT 60",
                conn,
                params=(today,),
            )
        except Exception as exc:
            logger.debug("regime: XAUUSD daily pull failed: %s", exc)
            return False

        if xau_df.empty:
            return False
        xau_df["d"] = xau_df["d"].astype(str)
        xau_df = xau_df[xau_df["d"].isin(dxy_dates)].sort_values("d").reset_index(drop=True)
        xau_df = xau_df.tail(DXY_CORR_LOOKBACK_DAYS)

        if len(xau_df) < DXY_CORR_LOOKBACK_DAYS or len(xau_df) < 5:
            return False

        dxy_vals = dxy_df["value"].to_numpy(dtype="float64")
        xau_vals = xau_df["close"].to_numpy(dtype="float64")
        if dxy_vals.size != xau_vals.size:
            # Align by the shorter length
            n = min(dxy_vals.size, xau_vals.size)
            dxy_vals = dxy_vals[-n:]
            xau_vals = xau_vals[-n:]

        # Pearson correlation
        try:
            corr = np.corrcoef(dxy_vals, xau_vals)[0, 1]
        except Exception:
            return False
        if np.isnan(corr):
            return False
        return abs(float(corr)) > DXY_CORR_THRESHOLD


# ---------------------------------------------------------------------------
# Small utilities
# ---------------------------------------------------------------------------

def _coerce_date(t: object) -> str | None:
    """Best-effort conversion of various time representations to ``YYYY-MM-DD``."""
    if t is None:
        return None
    # Numeric epoch
    if isinstance(t, (int, float)):
        try:
            ts = pd.to_datetime(t, unit="s" if float(t) < 1e12 else "ms",
                                errors="coerce", utc=True)
        except Exception:
            return None
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    # String
    if isinstance(t, str):
        ts = pd.to_datetime(t, errors="coerce", utc=True)
        if pd.isna(ts):
            return None
        return ts.strftime("%Y-%m-%d")
    # datetime / pd.Timestamp
    try:
        ts = pd.to_datetime(t, errors="coerce", utc=True)
    except Exception:
        return None
    if pd.isna(ts):
        return None
    return ts.strftime("%Y-%m-%d")


# Patch the helper onto the class so internal ``self._valid`` calls work.
def _valid(self, *vals: float | None) -> bool:
    """Return True iff every value is a finite number."""
    for v in vals:
        if v is None:
            return False
        try:
            f = float(v)
        except (TypeError, ValueError):
            return False
        if np.isnan(f) or np.isinf(f):
            return False
    return True


RegimeDetector._valid = _valid  # type: ignore[attr-defined]
RegimeDetector._coerce_date = staticmethod(_coerce_date)  # type: ignore[attr-defined]


__all__ = ["RegimeDetector", "NEWS_EVENT_TYPES", "GVZ_SERIES", "VIX_SERIES",
           "DXY_SERIES", "DXY_CORR_LOOKBACK_DAYS", "DXY_CORR_THRESHOLD"]
