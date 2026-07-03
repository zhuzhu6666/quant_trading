from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from config.runtime_config import RuntimeConfig, shared as shared_runtime_config


VALID_SYSTEM_MODES = {"backtest", "paper", "live"}


@dataclass(frozen=True)
class ExecutionSemantics:
    system_mode: str
    ctrader_send_orders: bool
    factor_dry_run: bool
    effective_send_orders: bool
    blocking_reason: str = ""

    @property
    def valid(self) -> bool:
        return not self.blocking_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "system_mode": self.system_mode,
            "ctrader_send_orders": self.ctrader_send_orders,
            "factor_dry_run": self.factor_dry_run,
            "effective_send_orders": self.effective_send_orders,
            "blocking_reason": self.blocking_reason,
        }


def system_mode_from_settings(settings: dict[str, Any] | None) -> str:
    system = settings.get("system") if isinstance(settings, dict) else {}
    if not isinstance(system, dict):
        return "backtest"
    return str(system.get("mode") or "backtest").strip().lower() or "backtest"


def evaluate_execution_semantics(
    settings: dict[str, Any] | None,
    runtime_config: Any | None = None,
) -> ExecutionSemantics:
    cfg = runtime_config or RuntimeConfig.from_yaml(settings or {})
    mode = system_mode_from_settings(settings or {})
    ctrader_send_orders = bool(getattr(cfg, "ctrader_send_orders", False))
    factor_dry_run = bool(getattr(cfg, "factor_dry_run", False))

    blocking_reason = ""
    if mode not in VALID_SYSTEM_MODES:
        blocking_reason = f"invalid_system_mode:{mode}"
    elif mode != "live" and ctrader_send_orders:
        blocking_reason = "ctrader_send_orders_requires_system_mode_live"

    effective = bool(not blocking_reason and mode == "live" and ctrader_send_orders and not factor_dry_run)
    return ExecutionSemantics(
        system_mode=mode,
        ctrader_send_orders=ctrader_send_orders,
        factor_dry_run=factor_dry_run,
        effective_send_orders=effective,
        blocking_reason=blocking_reason,
    )


def validate_execution_semantics(settings: dict[str, Any] | None, runtime_config: Any | None = None) -> ExecutionSemantics:
    semantics = evaluate_execution_semantics(settings, runtime_config)
    if semantics.blocking_reason:
        raise ValueError(f"invalid_execution_semantics: {semantics.blocking_reason}")
    return semantics


def current_execution_semantics(settings_text: str | None = None) -> ExecutionSemantics:
    if settings_text is None:
        from backend.services.config_service import SETTINGS_PATH

        if SETTINGS_PATH.exists():
            settings_text = SETTINGS_PATH.read_text(encoding="utf-8")
        else:
            settings_text = ""
    try:
        parsed = yaml.safe_load(settings_text) or {}
    except Exception:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return evaluate_execution_semantics(parsed, shared_runtime_config())


def effective_send_orders(settings: dict[str, Any] | None = None, runtime_config: Any | None = None) -> bool:
    return evaluate_execution_semantics(settings or {}, runtime_config).effective_send_orders


def opens_effective_send_orders(before: ExecutionSemantics, after: ExecutionSemantics) -> bool:
    return bool(not before.effective_send_orders and after.effective_send_orders)
