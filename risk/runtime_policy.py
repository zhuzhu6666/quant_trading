"""Shared runtime risk policy snapshots.

These helpers keep live execution, RiskPolicyService, and RiskGovernor on the
same risk-limit and runtime-health vocabulary.  They are inputs to the existing
RiskPolicyService boundary, not an additional authorization layer.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


def _safe_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except Exception:
        return int(default)


def _pct_value(value: Any, default_pct: float) -> float:
    pct = _safe_float(value, default_pct)
    if 0.0 < pct <= 1.0:
        return pct * 100.0
    return pct


@dataclass(frozen=True)
class RiskLimitSnapshot:
    schema_version: str = "risk_limit_snapshot.v1"
    source: str = "runtime_config"
    max_drawdown_pct: float = 15.0
    max_consecutive_losses: int = 8
    max_daily_loss_pct: float = 5.0
    max_daily_trades: int = 20
    data_lag_max_seconds: float = 3600.0
    loss_cooldown_after_losses: int = 2
    loss_cooldown_bars: int = 3
    block_on_disk_critical: bool = True
    require_l2_depth: bool = False
    var_threshold_pct: float = 2.0
    cvar_threshold_pct: float = 2.0
    circuit_breaker_bypass: bool = False

    @classmethod
    def from_runtime_config(cls, cfg: Any | None = None) -> "RiskLimitSnapshot":
        if cfg is None:
            try:
                from config.runtime_config import shared as runtime_config

                cfg = runtime_config()
            except Exception:
                cfg = None
        legacy_var_threshold = getattr(cfg, "var_cvar_threshold", 0.02) if cfg is not None else 0.02
        return cls(
            max_drawdown_pct=_safe_float(getattr(cfg, "risk_max_drawdown_pct", 15.0), 15.0),
            max_consecutive_losses=_safe_int(getattr(cfg, "risk_max_consecutive_losses", 8), 8),
            max_daily_loss_pct=_safe_float(getattr(cfg, "risk_max_daily_loss_pct", 5.0), 5.0),
            max_daily_trades=_safe_int(getattr(cfg, "risk_max_daily_trades", 20), 20),
            data_lag_max_seconds=_safe_float(getattr(cfg, "risk_data_lag_max_seconds", 3600.0), 3600.0),
            loss_cooldown_after_losses=_safe_int(getattr(cfg, "risk_loss_cooldown_after_losses", 2), 2),
            loss_cooldown_bars=_safe_int(getattr(cfg, "risk_loss_cooldown_bars", 3), 3),
            block_on_disk_critical=bool(getattr(cfg, "risk_block_on_disk_critical", True)),
            require_l2_depth=bool(getattr(cfg, "risk_require_l2_depth", False)),
            var_threshold_pct=_pct_value(getattr(cfg, "risk_var_threshold_pct", legacy_var_threshold), 2.0),
            cvar_threshold_pct=_pct_value(getattr(cfg, "risk_cvar_threshold_pct", legacy_var_threshold), 2.0),
            circuit_breaker_bypass=bool(getattr(cfg, "risk_circuit_breaker_bypass", False)),
        )

    @classmethod
    def from_context(cls, context: dict[str, Any] | None = None) -> "RiskLimitSnapshot":
        context = context or {}
        base = cls.from_runtime_config()
        raw = context.get("risk_limits") or context.get("risk_limit_snapshot") or {}
        if not isinstance(raw, dict):
            raw = {}
        var_cfg = context.get("var") if isinstance(context.get("var"), dict) else {}
        return cls(
            source=str(raw.get("source") or base.source),
            max_drawdown_pct=_safe_float(raw.get("max_drawdown_pct"), base.max_drawdown_pct),
            max_consecutive_losses=_safe_int(raw.get("max_consecutive_losses"), base.max_consecutive_losses),
            max_daily_loss_pct=_safe_float(raw.get("max_daily_loss_pct"), base.max_daily_loss_pct),
            max_daily_trades=_safe_int(raw.get("max_daily_trades"), base.max_daily_trades),
            data_lag_max_seconds=_safe_float(raw.get("data_lag_max_seconds"), base.data_lag_max_seconds),
            loss_cooldown_after_losses=_safe_int(
                raw.get("loss_cooldown_after_losses"),
                base.loss_cooldown_after_losses,
            ),
            loss_cooldown_bars=_safe_int(raw.get("loss_cooldown_bars"), base.loss_cooldown_bars),
            block_on_disk_critical=bool(raw.get("block_on_disk_critical", base.block_on_disk_critical)),
            require_l2_depth=bool(raw.get("require_l2_depth", base.require_l2_depth)),
            var_threshold_pct=_pct_value(
                var_cfg.get("threshold_pct", raw.get("var_threshold_pct", base.var_threshold_pct)),
                base.var_threshold_pct,
            ),
            cvar_threshold_pct=_pct_value(
                var_cfg.get("cvar_threshold_pct", raw.get("cvar_threshold_pct", base.cvar_threshold_pct)),
                base.cvar_threshold_pct,
            ),
            circuit_breaker_bypass=bool(raw.get("circuit_breaker_bypass", base.circuit_breaker_bypass)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuntimeHealthSnapshot:
    schema_version: str = "runtime_health_snapshot.v1"
    source: str = "risk_policy_context"
    loop_running: bool | None = None
    bridge_connected: bool | None = None
    data_lag_seconds: float = 0.0
    disk_space_status: str = ""
    l2_depth_status: str = ""
    raw: dict[str, Any] | None = None

    @classmethod
    def from_context(cls, context: dict[str, Any] | None = None) -> "RuntimeHealthSnapshot":
        context = context or {}
        runtime_health = context.get("runtime_health") or {}
        if not isinstance(runtime_health, dict):
            runtime_health = {}
        system_health = runtime_health.get("system_health") or {}
        if not isinstance(system_health, dict):
            system_health = {}
        component_status = system_health.get("component_status") or {}
        if not isinstance(component_status, dict):
            component_status = {}
        return cls(
            loop_running=context.get("loop_running") if "loop_running" in context else None,
            bridge_connected=context.get("bridge_connected") if "bridge_connected" in context else None,
            data_lag_seconds=_safe_float(context.get("data_lag_seconds", 0.0), 0.0),
            disk_space_status=str(component_status.get("disk_space") or ""),
            l2_depth_status=str(component_status.get("l2_depth") or ""),
            raw=runtime_health,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
