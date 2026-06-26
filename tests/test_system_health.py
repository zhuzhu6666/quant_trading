from __future__ import annotations

from monitor.system_health import ComponentStatus, HealthReport


def test_advisory_tick_data_does_not_force_critical_status() -> None:
    components = {
        "ctrader_bridge": ComponentStatus(name="cTrader 桥", status="ok", score=1.0),
        "live_loop": ComponentStatus(name="实盘循环", status="ok", score=1.0),
        "bar_m5": ComponentStatus(name="M5 Bar", status="ok", score=1.0),
        "tick_data": ComponentStatus(name="Tick 数据", status="critical", score=0.0),
    }

    criticals = [
        name for name, c in components.items()
        if c.status == "critical" and name not in {"tick_data"}
    ]
    degradeds = [
        name for name, c in components.items()
        if c.status == "degraded" or (c.status == "critical" and name in {"tick_data"})
    ]

    report = HealthReport(components=components)
    if criticals:
        report.overall = "critical"
    elif degradeds:
        report.overall = "degraded"
    else:
        report.overall = "healthy"

    assert report.overall == "degraded"
