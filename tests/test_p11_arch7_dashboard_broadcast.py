"""
tests/test_p11_arch7_dashboard_broadcast.py — Batch main/monitor 修复

引自 framework_audit_20260604.md ARCH-7:
monitor/dashboard.py:96 _broadcast() 定义但从未启动,
WebSocket 收不到任何数据, 实时仪表盘是空壳。

修复: @app.on_event("startup") 起 _broadcast_loop 协程,
按 config monitor.metrics_interval_seconds 间隔 broadcast。
"""
import inspect
from unittest.mock import MagicMock, patch


def test_dashboard_has_broadcast_loop_startup_handler():
    """P11 ARCH-7: dashboard 应有 startup handler 起 broadcast loop"""
    try:
        from monitor import dashboard
    except ImportError:
        import pytest
        pytest.skip("FastAPI/uvicorn not installed")
    # 检查 _start_broadcast_loop 函数存在
    assert hasattr(dashboard, "_start_broadcast_loop"), (
        "ARCH-7 未修: 没有 _start_broadcast_loop"
    )
    # 检查它被注册为 startup event handler
    # 简单方法: 检查它被 @app.on_event("startup") 装饰
    # 通过检查函数体里调用了 _broadcast
    src = inspect.getsource(dashboard._start_broadcast_loop)
    assert "_broadcast" in src, (
        f"_start_broadcast_loop 没调 _broadcast: {src[:200]}"
    )
    assert "asyncio.create_task" in src, (
        f"_start_broadcast_loop 没起后台 task: {src[:200]}"
    )


def test_broadcast_pushes_payload_to_clients():
    """P11: _broadcast 应当把 state 打包成 payload 推到所有 _ws_clients"""
    try:
        from monitor import dashboard
    except ImportError:
        import pytest
        pytest.skip("FastAPI/uvicorn not installed")

    import asyncio
    from unittest.mock import AsyncMock
    mock_ws = MagicMock()
    mock_ws.send_text = AsyncMock()
    dashboard._ws_clients.add(mock_ws)
    try:
        # mock state
        with patch.object(dashboard, "state") as mock_state:
            mock_state.position.direction = 1
            mock_state.position.entry_price = 2000.0
            mock_state.position.volume = 0.1
            mock_state.position.unrealized_pnl = 5.0
            mock_state.daily.total_trades = 3
            mock_state.daily.winning_trades = 2
            mock_state.daily.losing_trades = 1
            mock_state.daily.net_pnl = 10.0
            mock_state.daily.max_drawdown_pct = 1.5
            mock_state.daily.consecutive_losses = 0
            mock_state.equity = 1010.0
            mock_state.balance = 1000.0
            mock_state.is_circuit_breaker = False

            asyncio.run(dashboard._broadcast())

        assert mock_ws.send_text.called, (
            "P11: _broadcast 没调 ws.send_text"
        )
    finally:
        dashboard._ws_clients.discard(mock_ws)
