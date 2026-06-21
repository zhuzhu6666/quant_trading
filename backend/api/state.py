"""GET /api/state — 完整状态快照 (同 WS /ws/state 推送的内容).

用于微信小程序在无法使用 WebSocket 时的 HTTP 轮询回退。
"""

from fastapi import APIRouter

from backend.core.auth import RequireUser
from backend.ws.endpoints import _read_state_snapshot

router = APIRouter(prefix="/api", tags=["state"])


@router.get("/state")
def get_full_state(_user: RequireUser) -> dict:
    """返回完整状态快照，同 WebSocket /ws/state 推送的内容。"""
    return _read_state_snapshot()
