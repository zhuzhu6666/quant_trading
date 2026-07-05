"""Cadence and history-sampling policy for factors.

The normalizer must not treat a weekly COT value repeated on every M5 bar as
new evidence.  This small helper lives in alpha so backend catalog/reporting and
runtime normalization can share the same classification without backend imports.
"""
from __future__ import annotations

from typing import Any


LOW_FREQUENCY_DAILY_FACTORS = {
    "dxy_corr_20",
    "slv_gld_ratio",
    "real_yield_chg",
    "real_yield_pct_rank",
    "gld_tonnes_chg_5d",
    "gld_tonnes_chg_20d",
    "gld_tonnes_pct_20d",
    "gld_tonnes_zscore_60d",
    "slv_tonnes_chg_20d",
    "silver_gold_holdings_ratio",
}


def infer_factor_cadence(name: str, cfg: dict[str, Any] | None = None) -> tuple[str, str]:
    """Return ``(cadence, history_sample_policy)`` for a factor.

    Explicit config wins.  Unknown factors are assumed to be bar-level alpha so
    discovered GP factors remain eligible without manual configuration.
    """
    cfg = cfg if isinstance(cfg, dict) else {}
    cadence = str(cfg.get("cadence") or "").strip().lower()
    policy = str(cfg.get("history_sample_policy") or "").strip().lower()
    if cadence and policy:
        return cadence, policy

    if name in {"hours_to_fomc", "hours_to_nfp"} or name.startswith("evt_"):
        return cadence or "event", policy or "event_window"
    if name.startswith("cot_"):
        return cadence or "weekly", policy or "on_value_change"
    if name.startswith("cb_"):
        return cadence or "monthly", policy or "on_value_change"
    if name in LOW_FREQUENCY_DAILY_FACTORS:
        return cadence or "daily", policy or "on_value_change"
    return cadence or "bar", policy or "every_bar"
