"""Factor Takeover v4 API — weight history + attribution stats + ML retrain.

Endpoints:
  GET /api/v4/weights      — last 50 entries from factor_weight_history.jsonl
  GET /api/v4/stats        — attribution summary (stub, returns {} for now)
  POST /api/v4/ml/retrain  — manually trigger ML direction predictor retrain
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fastapi import APIRouter

from backend.core.auth import RequireUser
from backend.services.live_service import _scheduled_ml_retrain

router = APIRouter(prefix="/api/v4", tags=["factor-v4"])

DATA_FILE = Path(__file__).resolve().parent.parent.parent / "data" / "charts" / "factor_weight_history.jsonl"
_TRADE_LOG = Path(__file__).resolve().parent.parent.parent / "logs" / "live_trades.jsonl"


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
    """Stub: returns empty dict for now. Will be filled when attribution is
    wired into live state."""
    return {}


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
