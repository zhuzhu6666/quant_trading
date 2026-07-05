"""Factor Takeover v4 API — weight history + attribution stats.

Endpoints:
  GET /api/v4/weights      — last 50 entries from factor_weight_history.jsonl
  GET /api/v4/stats        — attribution summary per-factor + pipeline health
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from backend.core.auth import RequireUser
from backend.core.db import get_state_pg_conn

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v4", tags=["factor-v4"])

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "charts" / "factor_weight_history.jsonl"
_ATTR_SNAPSHOT = Path(__file__).resolve().parent.parent.parent / "data" / "charts" / "factor_attribution.json"


def _state_conn():
    return get_state_pg_conn(read_only=True)


def _state_sql(sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s")


@router.get("/weights")
def get_weight_history(_user: RequireUser) -> list[dict]:
    """Return latest effective weights: config baseline + state store weight_history merged."""
    weights: dict[str, float] = {}

    # 1. Load latest AWE weight per factor from state store
    try:
        conn = _state_conn()
        try:
            rows = conn.execute(
                "SELECT factor, new_weight FROM weight_history "
                "WHERE id IN (SELECT MAX(id) FROM weight_history GROUP BY factor)"
            ).fetchall()
            for r in rows:
                weights[r["factor"]] = float(r["new_weight"])
        finally:
            conn.close()
    except Exception:
        # 降级: JSONL 文件
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
                pass  # JSONL fallback file read failed — harmless, will use config baseline

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


@router.get("/catalog")
def get_factor_catalog(
    _user: RequireUser,
    snapshot: str | None = Query(default=None),
) -> dict[str, Any]:
    """Unified factor governance catalog for frontend/readiness inspection."""
    from backend.services.factor_catalog import build_factor_catalog, latest_factor_catalog_snapshot

    if str(snapshot or "").strip().lower() == "latest":
        latest = latest_factor_catalog_snapshot()
        return {
            "schema_version": "factor_catalog.v3",
            "snapshot_mode": "latest",
            **latest,
        }

    items = build_factor_catalog()
    return {
        "schema_version": "factor_catalog.v3",
        "snapshot_mode": "live",
        "count": len(items),
        "items": items,
    }


@router.get("/stats")
def get_attribution_stats(_user: RequireUser) -> dict:
    """Return attribution stats per-factor from state store (primary) or JSON file (fallback).

    Data written by AttributionEngine._save_stats_snapshot() after every close.
    Returns empty dict when no trades have been attributed yet.
    """
    # ── Primary: state store attribution_snapshot ──
    raw: dict[str, Any] = {}
    try:
        conn = _state_conn()
        try:
            rows = conn.execute(
                "SELECT factor, data_json FROM attribution_snapshot"
            ).fetchall()
            for r in rows:
                try:
                    raw[r["factor"]] = json.loads(r["data_json"])
                except (json.JSONDecodeError, TypeError):
                    continue
        finally:
            conn.close()
    except Exception as e:
        logger.warning("Failed to read attribution_snapshot from state store: %s", e)

    # ── Fallback: JSON file ──
    if not raw and _ATTR_SNAPSHOT.exists():
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
    system_stats: dict[str, Any] = {}
    for name, s in raw.items():
        if name == "__SYSTEM__":
            system_stats = s
            continue
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
    total_trades = system_stats.get("n_trades", 0)
    total_wins = sum(s["wins"] for s in per_factor.values())
    total_voted = sum(s["n_voted"] for s in per_factor.values())
    # ── 真实 PnL 汇总 (从 __SYSTEM__ 行读取, 非跨因子求和) ──
    total_gross = system_stats.get("total_gross", 0.0) or 0.0
    total_swap = system_stats.get("total_swap", 0.0) or 0.0
    total_comm = system_stats.get("total_commission", 0.0) or 0.0
    total_net = system_stats.get("total_net_pnl", 0.0) or 0.0
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
        "last_updated": time.time(),
        # ── 真实 PnL 汇总 ──
        "total_gross": round(total_gross, 2),
        "total_swap": round(total_swap, 2),
        "total_commission": round(total_comm, 2),
        "total_net_pnl": round(total_net, 2),
    }

    return {"status": "ok", "per_factor": per_factor, "summary": summary}


@router.get("/recent-ticks")
def get_recent_ticks(_user: RequireUser, n: int = 30) -> list[dict]:
    """返回最近 N 个 v4 信号记录 (从主状态库 decision_log 读取)。"""
    entries: list[dict] = []
    try:
        conn = _state_conn()
        try:
            rows = conn.execute(
                _state_sql("SELECT meta, ts FROM decision_log WHERE strategy='factor_v4' "
                "AND decision_type='signal' ORDER BY id DESC LIMIT ?"),
                (n,)
            ).fetchall()
            for r in reversed(rows):
                try:
                    entry = json.loads(r["meta"])
                    entry["ts"] = r["ts"]
                    entries.append(entry)
                except json.JSONDecodeError:
                    continue
        finally:
            conn.close()
    except Exception:
        pass
    return entries
