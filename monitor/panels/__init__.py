"""monitor/panels — 监控面板组件包。

导出 SystemOverviewPanel 供 dashboard 使用。
"""

from __future__ import annotations

from monitor.panels.overview import SystemOverviewPanel  # noqa: F401

__all__ = [
    "SystemOverviewPanel",
]
