#!/usr/bin/env python3
"""L1 feature IC snapshot: rank the open-quality model's 28 features.

Uses the REAL materialized `shadow_open_decision` sample features (no
recomputation, zero feature-definition drift) and labels every sample with a
price-path triple barrier walked on the M5 monthly bars:

    entry  = ask (long) / bid (short) recorded on the decision bar
    SL/TP  = 1.0 / 1.5 x ATR(14) at entry, time limit 24h (288 M5 bars)
    tie within one bar counts as SL first (conservative)
    expiry labels by the sign of the open PnL

Outputs per-feature univariate AUC (long-only, short-only, direction-pooled),
first/second-half stability, the same table on the execution-truth subset
(matured samples), and a price-path-vs-execution label agreement anchor.

Read-only against the databases; report goes to run_artifacts/.
"""

from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import duckdb

from backend.core.db import get_state_pg_conn
from backend.services.canonical_v2_reader import iter_training_sample_rows
from research.open_quality_lightgbm import FEATURE_NAMES, _features_from_sample

SYMBOL = "XAUUSD+"
TIMEFRAME = "M5"
ATR_PERIOD = 14
SL_ATR = 1.0
TP_ATR = 1.5
HORIZON_BARS = 288  # 24h of M5
SIGNED_FEATURES = {"action_score", "tactical_score", "macro_score", "alpha_score"}


def _load_bars(first_ts: float, last_ts: float) -> list[tuple[int, float, float, float, float]]:
    start_month = datetime.fromtimestamp(first_ts - 14 * 86400, tz=timezone.utc)
    end_month = datetime.fromtimestamp(last_ts, tz=timezone.utc)
    months: list[Path] = []
    for p in sorted((PROJECT_ROOT / "data" / "bars_monthly").glob("bars_*.duckdb")):
        y, m = int(p.stem.split("_")[1]), int(p.stem.split("_")[2])
        key = y * 100 + m
        if start_month.year * 100 + start_month.month <= key <= end_month.year * 100 + end_month.month:
            months.append(p)
    rows: list[tuple[int, float, float, float, float]] = []
    for path in months:
        con = duckdb.connect(str(path), read_only=True)
        try:
            rows.extend(
                (r[0], r[1], r[2], r[3], r[4])
                for r in con.execute(
                    "SELECT time, open, high, low, close FROM bars "
                    "WHERE symbol=? AND timeframe=? ORDER BY time",
                    [SYMBOL, TIMEFRAME],
                ).fetchall()
            )
        finally:
            con.close()
    rows.sort(key=lambda r: r[0])
    return rows


def _atr_series(bars: list[tuple[int, float, float, float, float]]) -> list[float]:
    out = [math.nan] * len(bars)
    prev_close: float | None = None
    atr: float | None = None
    for i, (_, o, h, l, c) in enumerate(bars):
        tr = h - l if prev_close is None else max(h - l, abs(h - prev_close), abs(l - prev_close))
        prev_close = c
        if atr is None:
            if i + 1 >= ATR_PERIOD:
                atr = tr  # seeded; Wilder smoothing continues
            continue
        atr = (atr * (ATR_PERIOD - 1) + tr) / ATR_PERIOD
        if i + 1 >= ATR_PERIOD:
            out[i] = atr
    return out


def _auc(values: list[float], labels: list[int]) -> float | None:
    """Mann-Whitney AUC: P(random positive ranks above random negative)."""
    pairs = [(v, l) for v, l in zip(values, labels) if v == v and l is not None]
    pos = [v for v, l in pairs if l == 1]
    neg = [v for v, l in pairs if l == 0]
    if not pos or not neg:
        return None
    all_v = sorted(set(pos + neg))
    rank = {v: i + 1 for i, v in enumerate(all_v)}
    # average ranks for ties
    from collections import defaultdict

    groups: dict[float, int] = defaultdict(int)
    for v in pos + neg:
        groups[v] += 1
    avg_rank: dict[float, float] = {}
    running = 0.0
    for v in all_v:
        count = groups[v]
        avg_rank[v] = running + (count + 1) / 2.0
        running += count
    r_pos = sum(avg_rank[v] for v in pos)
    u = r_pos - len(pos) * (len(pos) + 1) / 2.0
    return u / (len(pos) * len(neg))


def _path_label(
    bars: list[tuple[int, float, float, float, float]],
    start_idx: int,
    entry: float,
    direction: int,
    atr: float,
) -> tuple[int, str] | None:
    sl_dist = SL_ATR * atr
    tp_dist = TP_ATR * atr
    if sl_dist <= 0 or tp_dist <= 0:
        return None
    stop = entry - direction * sl_dist
    target = entry + direction * tp_dist
    end_idx = min(len(bars), start_idx + HORIZON_BARS)
    for i in range(start_idx, end_idx):
        _, _, high, low, _ = bars[i]
        if direction == 1:
            hit_sl = low <= stop
            hit_tp = high >= target
        else:
            hit_sl = high >= stop
            hit_tp = low <= target
        if hit_sl:
            return 0, "sl"
        if hit_tp:
            return 1, "tp"
    exit_close = bars[end_idx - 1][4]
    pnl = (exit_close - entry) * direction
    return (1 if pnl > 0 else 0), "expiry"


def main() -> int:
    conn = get_state_pg_conn()
    try:
        samples = iter_training_sample_rows(conn, sample_type="shadow_open_decision", limit=0)
    finally:
        conn.close()
    if not samples:
        print("no shadow_open_decision samples", file=sys.stderr)
        return 1

    items: list[dict[str, Any]] = []
    skipped = {"no_direction": 0, "no_entry_price": 0, "no_bars": 0, "zero_atr": 0}
    for row in samples:
        raw = row["features_json"]
        features_obj = raw if isinstance(raw, dict) else json.loads(raw or "{}")
        action = features_obj.get("action") or {}
        direction = int(action.get("direction") or 0)
        if direction not in (1, -1):
            skipped["no_direction"] += 1
            continue
        entry = float((action.get("ask") if direction == 1 else action.get("bid")) or 0.0)
        if entry <= 0:
            skipped["no_entry_price"] += 1
            continue
        label_raw = row.get("label_json")
        truth = None
        if label_raw:
            label = label_raw if isinstance(label_raw, dict) else json.loads(label_raw)
            target = label.get("open_target_v2") if isinstance(label.get("open_target_v2"), dict) else {}
            fin = str(target.get("financial_label") or "").lower()
            if fin in {"profit", "loss"}:
                truth = 1 if fin == "profit" else 0
        items.append(
            {
                "event_ts": float(row["event_ts"]),
                "direction": direction,
                "entry": entry,
                "truth": truth,
                "features": _features_from_sample({"features_json": json.dumps(features_obj, ensure_ascii=False)}),
            }
        )
    items.sort(key=lambda it: it["event_ts"])
    if not items:
        print("no usable samples after filters", file=sys.stderr)
        return 1

    bars = _load_bars(items[0]["event_ts"], items[-1]["event_ts"])
    if len(bars) < ATR_PERIOD + 10:
        print("insufficient bars", file=sys.stderr)
        return 1
    atr = _atr_series(bars)
    bar_times = [b[0] for b in bars]
    import bisect

    labeled = 0
    label_sources = {"sl": 0, "tp": 0, "expiry": 0}
    for it in items:
        idx = bisect.bisect_left(bar_times, int(it["event_ts"]))
        if idx >= len(bars):
            skipped["no_bars"] += 1
            continue
        a = atr[idx]
        if a is None or not (a > 0):
            skipped["zero_atr"] += 1
            continue
        result = _path_label(bars, idx, it["entry"], it["direction"], a)
        if result is None:
            skipped["zero_atr"] += 1
            continue
        it["path_label"], it["path_source"] = result
        label_sources[it["path_source"]] += 1
        labeled += 1

    labeled_items = [it for it in items if "path_label" in it]
    truth_items = [it for it in labeled_items if it["truth"] is not None]
    agreement = (
        round(sum(1 for it in truth_items if it["path_label"] == it["truth"]) / len(truth_items), 4)
        if truth_items
        else None
    )

    median_ts = labeled_items[len(labeled_items) // 2]["event_ts"]
    first_half = [it for it in labeled_items if it["event_ts"] <= median_ts]
    second_half = [it for it in labeled_items if it["event_ts"] > median_ts]

    def _feature_table(dataset: list[dict[str, Any]], label_key: str) -> list[dict[str, Any]]:
        table = []
        for name in FEATURE_NAMES:
            values = [it["features"].get(name, 0.0) for it in dataset]
            labels = [int(it[label_key]) for it in dataset]
            dirs = [it["direction"] for it in dataset]
            long_vals = [v for v, d in zip(values, dirs) if d == 1]
            long_lab = [l for v, d, l in zip(values, dirs, labels) if d == 1]
            short_vals = [v for v, d in zip(values, dirs) if d == -1]
            short_lab = [l for v, d, l in zip(values, dirs, labels) if d == -1]
            if name in SIGNED_FEATURES:
                pooled_vals = [v * d for v, d in zip(values, dirs)]
            else:
                pooled_vals = values
            auc_long = _auc(long_vals, long_lab)
            auc_short = _auc(short_vals, short_lab)
            auc_pooled = _auc(pooled_vals, labels)
            table.append(
                {
                    "feature": name,
                    "auc_long": round(auc_long, 4) if auc_long is not None else None,
                    "auc_short": round(auc_short, 4) if auc_short is not None else None,
                    "auc_pooled": round(auc_pooled, 4) if auc_pooled is not None else None,
                }
            )
        return table

    path_table = _feature_table(labeled_items, "path_label")
    for row in path_table:
        auc = row["auc_pooled"]
        row["informativeness"] = round(abs(auc - 0.5), 4) if auc is not None else None
    path_table.sort(key=lambda r: (r["informativeness"] if r["informativeness"] is not None else 0.0), reverse=True)

    def _stability(name: str) -> dict[str, Any]:
        def pooled(dataset: list[dict[str, Any]]) -> float | None:
            vals = [
                (it["features"].get(name, 0.0) * it["direction"]) if name in SIGNED_FEATURES
                else it["features"].get(name, 0.0)
                for it in dataset
            ]
            labs = [int(it["path_label"]) for it in dataset]
            return _auc(vals, labs)

        a, b = pooled(first_half), pooled(second_half)
        return {
            "first_half_auc": round(a, 4) if a is not None else None,
            "second_half_auc": round(b, 4) if b is not None else None,
            "flip": (a is not None and b is not None and (a - 0.5) * (b - 0.5) < 0),
        }
    stability = {row["feature"]: _stability(row["feature"]) for row in path_table}

    truth_table = _feature_table(truth_items, "truth") if truth_items else []
    truth_by_name = {r["feature"]: r["auc_pooled"] for r in truth_table}

    report = {
        "schema_version": "feature_ic_snapshot.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "barriers": {
            "atr_period": ATR_PERIOD, "sl_atr": SL_ATR, "tp_atr": TP_ATR,
            "horizon_bars": HORIZON_BARS, "tie_rule": "sl_first",
        },
        "samples": {
            "total": len(samples),
            "labeled": labeled,
            "skipped": skipped,
            "label_sources": label_sources,
            "truth_anchor_n": len(truth_items),
            "path_vs_execution_label_agreement": agreement,
        },
        "feature_ranking_by_informativeness": path_table,
        "stability": stability,
        "execution_truth_auc_pooled": truth_by_name,
        "caveats": [
            "Price-path labels approximate execution outcomes (no spread/slip at history).",
            "Uniform ATR barriers; real trades use event-scaled SL/TP.",
            "Features are production-materialized at decision time (no recomputation drift).",
            "Univariate AUC; interactions are not captured. This screens features, it does not prove the model.",
        ],
    }
    out_dir = PROJECT_ROOT / "run_artifacts" / "feature_ic_snapshot"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"snapshot_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nwritten: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
