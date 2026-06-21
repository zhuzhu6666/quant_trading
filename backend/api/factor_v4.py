"""Factor Takeover v4 API — weight history + attribution stats + ML retrain.

Endpoints:
  GET /api/v4/weights      — last 50 entries from factor_weight_history.jsonl
  GET /api/v4/stats        — attribution summary per-factor + pipeline health
  POST /api/v4/ml/retrain  — manually trigger ML direction predictor retrain
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from collections import Counter
from pathlib import Path

from fastapi import APIRouter

from backend.core.auth import RequireUser
from backend.services.live_service import _scheduled_ml_retrain

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v4", tags=["factor-v4"])

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "charts" / "factor_weight_history.jsonl"
_TRADE_LOG = Path(__file__).resolve().parent.parent.parent / "logs" / "live_trades.jsonl"
_ATTR_SNAPSHOT = Path(__file__).resolve().parent.parent.parent / "data" / "charts" / "factor_attribution.json"


@router.get("/weights")
def get_weight_history(_user: RequireUser) -> list[dict]:
    """Return latest effective weights: config baseline + AWE adaptations merged."""
    weights: dict[str, float] = {}

    # 1. Load AWE weight history (latest adaptation per factor)
    if DATA_FILE.exists():
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        name = entry.get("factor") or entry.get("name")
                        new_w = entry.get("new")
                        if name and new_w is not None:
                            weights[name] = float(new_w)
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    # 2. Fill in config baseline for factors AWE hasn't adapted yet
    try:
        from config.runtime_config import shared as _rc
        cfg = _rc()
        for name, w in (cfg.factor_portfolio_weights or {}).items():
            if name not in weights:
                if isinstance(w, dict):
                    w = w.get("weight", 1.0)
                weights[name] = float(w)
    except Exception:
        pass

    # 3. Filter out test/dev entries not in config (factor_0, test, f1, etc.)
    try:
        from config.runtime_config import shared as _rc2
        cfg2 = _rc2()
        valid = set(cfg2.factor_portfolio_weights.keys()) | set(cfg2.factor_signal_config.keys())
        weights = {k: v for k, v in weights.items() if k in valid}
    except Exception:
        pass

    return [{"factor": k, "new": v} for k, v in
            sorted(weights.items(), key=lambda x: -x[1])]


@router.get("/stats")
def get_attribution_stats(_user: RequireUser) -> dict:
    """Return attribution stats per-factor from snapshot file + summary.

    Data written by AttributionEngine._save_stats_snapshot() after every close.
    Returns empty dict when no trades have been attributed yet.
    """
    if not _ATTR_SNAPSHOT.exists():
        return {"status": "no_data", "per_factor": {}, "summary": {}}

    try:
        with open(_ATTR_SNAPSHOT, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read attribution snapshot: %s", e)
        return {"status": "error", "detail": str(e)}

    if not raw:
        return {"status": "no_data", "per_factor": {}, "summary": {}}

    # ── Build per-factor summary (trim recent_mcs to avoid bloat) ──
    per_factor = {}
    for name, s in raw.items():
        per_factor[name] = {
            "n_trades": s.get("n_trades", 0),
            "n_voted": s.get("n_voted", 0),
            "wins": s.get("wins", 0),
            "win_rate": round(
                s["wins"] / s["n_voted"], 4
            ) if s.get("n_voted", 0) > 0 else 0.0,
            "total_mc": s.get("total_mc", 0.0),
            "avg_mc": s.get("avg_mc", 0.0),
            "composite_sharpe_score": s.get("composite_sharpe_score"),
            "ir_short": s.get("ir_short"),
            "ir_mid": s.get("ir_mid"),
            "ir_long": s.get("ir_long"),
            "recent_mcs_sample": s.get("recent_mcs", [])[-10:],  # last 10 only
        }

    # ── Summary stats ──
    n_factors = len(per_factor)
    total_trades = sum(s["n_trades"] for s in per_factor.values())
    total_wins = sum(s["wins"] for s in per_factor.values())
    total_voted = sum(s["n_voted"] for s in per_factor.values())
    avg_sharpe_values = [
        s["composite_sharpe_score"] for s in per_factor.values()
        if s.get("composite_sharpe_score") is not None
        and not (isinstance(s["composite_sharpe_score"], float) and math.isnan(s["composite_sharpe_score"]))
    ]
    avg_sharpe = round(sum(avg_sharpe_values) / len(avg_sharpe_values), 4) if avg_sharpe_values else None

    # Top/bottom factors by avg_mc
    sorted_by_mc = sorted(
        per_factor.items(), key=lambda x: abs(x[1].get("avg_mc", 0)), reverse=True
    )
    top_factors = [
        {"name": n, "avg_mc": v["avg_mc"], "win_rate": v["win_rate"]}
        for n, v in sorted_by_mc[:10]
    ]

    summary = {
        "n_factors_attributed": n_factors,
        "total_trades": total_trades,
        "total_voted": total_voted,
        "total_wins": total_wins,
        "overall_win_rate": round(total_wins / total_voted, 4) if total_voted > 0 else 0.0,
        "avg_sharpe_across_factors": avg_sharpe,
        "top_contributors": top_factors,
        "last_updated": _ATTR_SNAPSHOT.stat().st_mtime,
    }

    return {"status": "ok", "per_factor": per_factor, "summary": summary}


@router.post("/ml/retrain")
async def trigger_ml_retrain(_user: RequireUser) -> dict:
    """手动触发 ML 方向预测器重训（后台运行，立即返回）。"""
    loop = asyncio.get_event_loop()
    loop.run_in_executor(None, _scheduled_ml_retrain)
    return {"status": "started"}


@router.get("/recent-ticks")
def get_recent_ticks(_user: RequireUser, n: int = 30) -> list[dict]:
    """返回最近 N 个 tick 的因子管道数据（从 live_trades.jsonl 读取）。"""
    if not _TRADE_LOG.exists():
        return []
    try:
        with open(_TRADE_LOG, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-n:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries[::-1]  # 最新在前
    except OSError:
        return []
