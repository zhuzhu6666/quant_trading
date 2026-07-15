"""Metrics — 进程内 Prometheus 指标单例。

设计:
- 不引入 prometheus_client 之外的依赖(prometheus_client 是 stdlib 级别的小包)
- 通过 backend.runtime.runtime_state 的 metrics_hook 注入到本模块
- 提供 factor_count / loop_status / data_sync_last_bar_age_seconds / factor_health_score / factor_lifecycle_events_total 最小集
- 其他模块通过 Metrics.shared() 拿单例,再 set_gauge / inc_counter

Phase 1.3 末: 与 backend/api/metrics.py 配合暴露 /metrics 端点。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Tuple

try:
    from prometheus_client import (  # type: ignore
        CollectorRegistry,
        Counter,
        Gauge,
        Histogram,
        generate_latest,
    )
    from prometheus_client.exposition import CONTENT_TYPE_LATEST  # type: ignore

    _PROM_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PROM_AVAILABLE = False
    CollectorRegistry = None  # type: ignore
    Counter = None  # type: ignore
    Gauge = None  # type: ignore
    Histogram = None  # type: ignore
    generate_latest = None  # type: ignore
    CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"

logger = logging.getLogger(__name__)


def metrics_backend_status() -> dict[str, Any]:
    """Stable readiness contract for the active metrics implementation."""
    backend = "prometheus" if _PROM_AVAILABLE else "fallback"
    return {
        "ok": bool(_PROM_AVAILABLE),
        "status": "healthy" if _PROM_AVAILABLE else "degraded",
        "metrics_backend": backend,
        "prometheus_available": bool(_PROM_AVAILABLE),
    }


class Metrics:
    """Prometheus 指标单例。

    所有指标都注册到自己的 CollectorRegistry(避免污染默认全局 registry,
    方便测试 reset)。
    """

    _instance: Optional["Metrics"] = None
    _lock = threading.Lock()

    def __init__(self) -> None:
        self._enabled: bool = True
        self._fallback_samples: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}
        if not _PROM_AVAILABLE:
            logger.warning("prometheus_client not installed, using lightweight text metrics fallback")
            return
        self._registry = CollectorRegistry()
        # 最小指标集
        self.factor_count = Gauge(
            "factor_count",
            "Number of factors by source",
            ["source"],
            registry=self._registry,
        )
        self.loop_status = Gauge(
            "loop_status",
            "Whether a runtime loop is currently running (1=running, 0=stopped)",
            ["kind"],
            registry=self._registry,
        )
        self.data_sync_last_bar_age_seconds = Gauge(
            "data_sync_last_bar_age_seconds",
            "Seconds since the most recent bar was pulled from broker",
            ["symbol", "timeframe"],
            registry=self._registry,
        )
        self.factor_health_score = Gauge(
            "factor_health_score",
            "Latest factor health score (0-100)",
            ["factor", "status"],
            registry=self._registry,
        )
        self.factor_lifecycle_events_total = Counter(
            "factor_lifecycle_events_total",
            "Count of factor lifecycle events",
            ["event", "source"],
            registry=self._registry,
        )
        # Phase 2 预占位
        self.canary_rollback_total = Counter(
            "canary_rollback_total",
            "Count of canary rollbacks",
            registry=self._registry,
        )
        self.risk_rebalance_events_total = Counter(
            "risk_rebalance_events_total",
            "Count of risk rebalances triggered by factor set change",
            registry=self._registry,
        )
        self.gp_elite_added_total = Counter(
            "gp_elite_added_total",
            "Count of GP elite individuals added to archive",
            registry=self._registry,
        )

    @classmethod
    def shared(cls) -> "Metrics":
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """仅供测试使用。"""
        with cls._lock:
            cls._instance = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def render(self) -> Tuple[bytes, str]:
        """返回 (body, content_type) 给 /metrics 端点。"""
        if not _PROM_AVAILABLE:
            lines = ["# metrics fallback (prometheus_client not installed)"]
            for (name, labels), value in sorted(self._fallback_samples.items()):
                if labels:
                    label_text = ",".join(f'{k}="{v}"' for k, v in labels)
                    lines.append(f"{name}{{{label_text}}} {float(value)}")
                else:
                    lines.append(f"{name} {float(value)}")
            return ("\n".join(lines).encode("utf-8") + b"\n", CONTENT_TYPE_LATEST)
        return generate_latest(self._registry), CONTENT_TYPE_LATEST  # type: ignore

    def _fallback_set(self, name: str, labels: dict[str, Any], value: float) -> None:
        key = (name, tuple((str(k), str(v)) for k, v in labels.items()))
        self._fallback_samples[key] = float(value)

    def _fallback_inc(self, name: str, labels: dict[str, Any], value: float = 1.0) -> None:
        key = (name, tuple((str(k), str(v)) for k, v in labels.items()))
        self._fallback_samples[key] = float(self._fallback_samples.get(key, 0.0)) + float(value)

    # ----- 便捷 API -----
    def emit(self, name: str, fields: Dict[str, Any]) -> None:
        """RuntimeState.emit_metric 的回调入口。

        支持的 name:
        - factor_count: {source: str, value: float}
        - loop_status: {kind: str, value: float}
        - data_sync_last_bar_age_seconds: {symbol, timeframe, value}
        - factor_health_score: {factor, status, value}
        - factor_lifecycle_events_total: {event, source, value=1(默认)}
        """
        if not _PROM_AVAILABLE:
            if name == "factor_count":
                self._fallback_set(name, {"source": fields["source"]}, float(fields.get("value", 1)))
            elif name == "loop_status":
                self._fallback_set(name, {"kind": fields["kind"]}, float(fields.get("value", 1)))
            elif name == "data_sync_last_bar_age_seconds":
                self._fallback_set(name, {"symbol": fields["symbol"], "timeframe": fields["timeframe"]}, float(fields["value"]))
            elif name == "factor_health_score":
                self._fallback_set(name, {"factor": fields["factor"], "status": fields["status"]}, float(fields["value"]))
            elif name == "factor_lifecycle_events_total":
                self._fallback_inc(name, {"event": fields["event"], "source": fields.get("source", "unknown")}, float(fields.get("value", 1)))
            elif name in {"canary_rollback_total", "risk_rebalance_events_total", "gp_elite_added_total"}:
                self._fallback_inc(name, {}, float(fields.get("value", 1)))
            else:
                logger.debug("Metrics.emit unknown name=%s", name)
            return
        try:
            if name == "factor_count":
                self.factor_count.labels(source=str(fields["source"])).set(float(fields.get("value", 1)))
            elif name == "loop_status":
                self.loop_status.labels(kind=str(fields["kind"])).set(float(fields.get("value", 1)))
            elif name == "data_sync_last_bar_age_seconds":
                self.data_sync_last_bar_age_seconds.labels(
                    symbol=str(fields["symbol"]), timeframe=str(fields["timeframe"])
                ).set(float(fields["value"]))
            elif name == "factor_health_score":
                self.factor_health_score.labels(
                    factor=str(fields["factor"]), status=str(fields["status"])
                ).set(float(fields["value"]))
            elif name == "factor_lifecycle_events_total":
                self.factor_lifecycle_events_total.labels(
                    event=str(fields["event"]), source=str(fields.get("source", "unknown"))
                ).inc(float(fields.get("value", 1)))
            elif name == "canary_rollback_total":
                self.canary_rollback_total.inc(float(fields.get("value", 1)))
            elif name == "risk_rebalance_events_total":
                self.risk_rebalance_events_total.inc(float(fields.get("value", 1)))
            elif name == "gp_elite_added_total":
                self.gp_elite_added_total.inc(float(fields.get("value", 1)))
            else:
                logger.debug("Metrics.emit unknown name=%s", name)
        except Exception:  # noqa: BLE001
            logger.exception("Metrics.emit failed for name=%s fields=%r", name, fields)


# 让 RuntimeState.emit_metric 自动接到 Metrics
def install_into_runtime_state() -> None:
    """把 Metrics.shared() 注册为 RuntimeState 的 metrics_hook。

    应该在 FastAPI lifespan 启动时调用一次。
    """
    from backend.runtime.runtime_state import RuntimeState

    state = RuntimeState.shared()
    metrics = Metrics.shared()
    if not metrics.enabled:
        logger.info("Metrics disabled, skipping install")
        return
    state.set_metrics_hook(metrics.emit)
    logger.info("Metrics installed into RuntimeState")
