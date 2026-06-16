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
    """Return the last 50 entries from factor_weight_history.jsonl."""
    if not DATA_FILE.exists():
        return []
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            lines = f.readlines()
        entries = []
        for line in lines[-50:]:
            line = line.strip()
            if line:
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return entries
    except OSError:
        return []


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
