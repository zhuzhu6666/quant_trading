"""test_data_quality_gate — gap / duplicate / outlier 检测。"""
from __future__ import annotations

import pandas as pd
import pytest

from data.live_sync.quality_gate import DataQualityGate


@pytest.fixture
def gate() -> DataQualityGate:
    return DataQualityGate(bad_ratio_threshold=0.1)


def _df_clean(n: int = 100) -> pd.DataFrame:
    """构造一根毛病的正常 DataFrame,所有列单调。"""
    import numpy as np
    from datetime import datetime, timedelta, timezone

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = []
    for i in range(n):
        rows.append({
            "ts": base + timedelta(minutes=15 * i),
            "open": 100 + i * 0.01,
            "high": 100 + i * 0.01 + 0.5,
            "low": 100 + i * 0.01 - 0.5,
            "close": 100 + i * 0.01 + 0.1,
            "volume": 1000,
        })
    return pd.DataFrame(rows)


def test_clean_dataframe_passes(gate: DataQualityGate) -> None:
    report = gate.check("XAUUSD+", "M15", _df_clean(100))
    assert report.passed is True
    assert report.n_gaps == 0
    assert report.n_duplicates == 0
    assert report.n_outliers == 0


def test_gap_detected(gate: DataQualityGate) -> None:
    df = _df_clean(100)
    # 跳过 5 根:在 idx=50 把后续时间戳 +75min
    from datetime import timedelta
    df.loc[50:, "ts"] = df.loc[50:, "ts"] + timedelta(minutes=75)
    report = gate.check("XAUUSD+", "M15", df)
    # 至少 1 个 gap
    assert report.n_gaps >= 1


def test_duplicate_detected(gate: DataQualityGate) -> None:
    df = _df_clean(100)
    # 复制最后一行
    df = pd.concat([df, df.tail(1)], ignore_index=True)
    report = gate.check("XAUUSD+", "M15", df)
    assert report.n_duplicates >= 1


def test_empty_dataframe_returns_passed(gate: DataQualityGate) -> None:
    report = gate.check("XAUUSD+", "M15", pd.DataFrame())
    assert report.total_rows == 0
    assert report.passed is True


def test_bad_ratio_triggers_passed_false(gate: DataQualityGate) -> None:
    """20% bad → passed=False。"""
    df = _df_clean(10)
    # 加 3 个完全重复行,让 bad_ratio = 3/(10+3) ≈ 0.23
    df = pd.concat([df, df.head(3)], ignore_index=True)
    report = gate.check("XAUUSD+", "M15", df)
    assert report.passed is False
