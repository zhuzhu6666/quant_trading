"""DataQualityGate — 检测同步数据中的 gap / duplicate / outlier。

设计:
- DataQualityGate.check(symbol, timeframe, df) 返回 DataQualityReport
- gap: 时间戳间隔大于 2*bar_duration 视为缺口
- duplicate: 完全相同 (ts, open, high, low, close) 视为重复
- outlier: close 偏离前一根 close > 5*ATR(14) 视为异常
- 触发条件:bad_rows / total_rows > 0.1 (10% 阈值) → 写 quality log + emit metric
- 与 SyncHealth 联动:连续多次 quality failure 累加 consecutive_failures
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


# 1 bar 时长(分钟) by timeframe
BAR_MINUTES = {
    "M5": 5,
    "M15": 15,
    "M30": 30,
    "H1": 60,
    "H4": 240,
    "D1": 1440,
}


@dataclass
class DataQualityReport:
    symbol: str
    timeframe: str
    total_rows: int = 0
    n_gaps: int = 0
    n_duplicates: int = 0
    n_outliers: int = 0
    bad_ratio: float = 0.0
    passed: bool = True
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class DataQualityGate:
    """数据质量门:扫一个 DataFrame,标出 gap/duplicate/outlier。

    用法::

        gate = DataQualityGate()
        report = gate.check("XAUUSD+", "M15", df)
        if not report.passed:
            ...

    阈值:bad_ratio > 0.1 → passed=False
    """

    def __init__(self, bad_ratio_threshold: float = 0.1, log_path: Optional[str] = None) -> None:
        self.bad_ratio_threshold = float(bad_ratio_threshold)
        self._log_path = log_path or "data/charts/data_quality.jsonl"
        Path(self._log_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def check(self, symbol: str, timeframe: str, df: pd.DataFrame) -> DataQualityReport:
        report = DataQualityReport(symbol=symbol, timeframe=timeframe)
        if df is None or df.empty:
            report.notes.append("empty dataframe")
            return report

        report.total_rows = int(len(df))
        # ---- gap ----
        if "ts" in df.columns and len(df) > 1:
            ts = pd.to_datetime(df["ts"], utc=True, errors="coerce").dropna().sort_values()
            if len(ts) > 1:
                deltas = ts.diff().dt.total_seconds().dropna()
                expected = BAR_MINUTES.get(timeframe, 15) * 60
                report.n_gaps = int((deltas > expected * 2.0).sum())
        # ---- duplicate ----
        key_cols = [c for c in ("ts", "open", "high", "low", "close") if c in df.columns]
        if key_cols:
            report.n_duplicates = int(df.duplicated(subset=key_cols).sum())
        # ---- outlier ----
        if "close" in df.columns and len(df) > 15:
            try:
                close = pd.to_numeric(df["close"], errors="coerce")
                prev = close.shift(1)
                tr = (df["high"].astype(float) - df["low"].astype(float)).abs() if "high" in df.columns and "low" in df.columns else (close - prev).abs()
                atr14 = tr.rolling(14).mean()
                dev = (close - prev).abs()
                valid = atr14 > 0
                report.n_outliers = int(((dev > 5.0 * atr14) & valid).sum())
            except Exception:  # noqa: BLE001
                logger.debug("outlier check failed", exc_info=True)

        total_bad = report.n_gaps + report.n_duplicates + report.n_outliers
        report.bad_ratio = total_bad / report.total_rows if report.total_rows else 0.0
        report.passed = report.bad_ratio <= self.bad_ratio_threshold

        if not report.passed:
            self._log_failure(report)
        return report

    def _log_failure(self, report: DataQualityReport) -> None:
        record = {
            "ts": time.time(),
            "ts_iso": _iso_now(),
            "report": report.to_dict(),
        }
        with self._lock:
            try:
                with open(self._log_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record, ensure_ascii=False) + "\n")
            except OSError:
                logger.exception("DataQualityGate._log_failure failed")
        # 联动 metric
        try:
            from backend.runtime.runtime_state import RuntimeState

            RuntimeState.shared().emit_metric(
                "data_quality_failed",
                {
                    "symbol": report.symbol,
                    "timeframe": report.timeframe,
                    "value": report.bad_ratio,
                },
            )
        except Exception:  # noqa: BLE001
            pass


def _iso_now() -> str:
    from datetime import datetime, timezone

    return datetime.now(tz=timezone.utc).isoformat()
