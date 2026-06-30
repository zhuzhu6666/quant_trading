"""GET /api/state — 完整状态快照 (同 WS /ws/state 推送的内容).

用于微信小程序在无法使用 WebSocket 时的 HTTP 轮询回退。
"""

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter

from backend.core.auth import RequireUser
from backend.ws.endpoints import _read_state_snapshot

router = APIRouter(prefix="/api", tags=["state"])


def _json_safe(value):
    """Convert runtime/numpy values to FastAPI-serializable Python objects."""
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if hasattr(value, "tolist"):
        try:
            return _json_safe(value.tolist())
        except Exception:
            pass
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except Exception:
            pass
    return value


@router.get("/state")
def get_full_state(_user: RequireUser) -> dict:
    """返回完整状态快照，同 WebSocket /ws/state 推送的内容。"""
    return _json_safe(_read_state_snapshot())
