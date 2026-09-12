"""GET /api/system/db-health — 数据库健康状态（大小/行数/最新数据时间）

健康快照的计算与缓存由 backend.services.db_health_service 拥有；本模块只保留
HTTP 路由与 app 生命周期注册。
"""
import time

from fastapi import APIRouter

from backend.core.auth import RequireUser
from backend.services import db_health_service
from backend.services.api_fact_views import db_health_fact_payload

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/db-health")
def db_health(_user: RequireUser) -> dict:
    """返回所有数据库的健康状态 (后台自动刷新, 永远 <1ms)。

    后台线程每 55s 更新缓存, 请求端永远直接读缓存。
    """
    result = db_health_service.snapshot_or_compute()
    return db_health_fact_payload(result, now=time.time())


# 注册到 FastAPI app 的 startup 事件
# (在 backend/app.py 中调用: db_health.register_startup(app))
def register_startup(app):
    app.add_event_handler(
        "startup",
        lambda: db_health_service.start_background_refresh(initial_delay_sec=3.0),
    )
    app.add_event_handler(
        "shutdown",
        lambda: db_health_service.stop_background_refresh(timeout_sec=30.0),
    )
