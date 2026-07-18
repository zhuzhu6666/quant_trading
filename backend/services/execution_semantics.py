from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml

from config.runtime_config import RuntimeConfig, shared as shared_runtime_config
from execution.broker_config import (
    BrokerConnectionConfig,
    shared_broker_connection_config,
)


VALID_SYSTEM_MODES = {"backtest", "paper", "live"}
LIVE_ACCOUNT_MAX_DAILY_LOSS_PCT = 5.0
LIVE_ACCOUNT_MAX_DAILY_TRADES = 20


@dataclass(frozen=True)
class ExecutionSemantics:
    system_mode: str
    ctrader_send_orders: bool
    factor_dry_run: bool
    effective_send_orders: bool
    blocking_reason: str = ""
    broker_environment: str = "unknown"
    effective_broker_host: str = ""
    effective_broker_account_id: int = 0
    broker_config_hash: str = ""

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
            "broker_environment": self.broker_environment,
            "effective_broker_host": self.effective_broker_host,
            "effective_broker_account_id": self.effective_broker_account_id,
            "broker_config_hash": self.broker_config_hash,
        }


def system_mode_from_settings(settings: dict[str, Any] | None) -> str:
    system = settings.get("system") if isinstance(settings, dict) else {}
    if not isinstance(system, dict):
        return "backtest"
    return str(system.get("mode") or "backtest").strip().lower() or "backtest"


def evaluate_execution_semantics(
    settings: dict[str, Any] | None,
    runtime_config: Any | None = None,
    *,
    broker_config: BrokerConnectionConfig | None = None,
) -> ExecutionSemantics:
    cfg = runtime_config or RuntimeConfig.from_yaml(settings or {})
    effective_broker = broker_config or BrokerConnectionConfig.from_sources(settings or {})
    mode = system_mode_from_settings(settings or {})
    ctrader_send_orders = bool(getattr(cfg, "ctrader_send_orders", False))
    factor_dry_run = bool(getattr(cfg, "factor_dry_run", False))

    blocking_reason = ""
    if mode not in VALID_SYSTEM_MODES:
        blocking_reason = f"invalid_system_mode:{mode}"
    elif mode != "live" and ctrader_send_orders:
        blocking_reason = "ctrader_send_orders_requires_system_mode_live"
    elif mode == "live" and ctrader_send_orders and not factor_dry_run:
        if not effective_broker.is_demo:
            daily_loss = float(getattr(cfg, "risk_max_daily_loss_pct", 5.0) or 5.0)
            daily_trades = max(
                int(getattr(cfg, "risk_max_daily_trades", 20) or 20),
                int(getattr(cfg, "demo_learning_max_daily_trades", 20) or 20),
            )
            if daily_loss > LIVE_ACCOUNT_MAX_DAILY_LOSS_PCT:
                blocking_reason = "demo_daily_loss_limit_requires_demo_ctrader_host"
            elif daily_trades > LIVE_ACCOUNT_MAX_DAILY_TRADES:
                blocking_reason = "demo_daily_trade_limit_requires_demo_ctrader_host"

    effective = bool(not blocking_reason and mode == "live" and ctrader_send_orders and not factor_dry_run)
    return ExecutionSemantics(
        system_mode=mode,
        ctrader_send_orders=ctrader_send_orders,
        factor_dry_run=factor_dry_run,
        effective_send_orders=effective,
        blocking_reason=blocking_reason,
        broker_environment=effective_broker.environment,
        effective_broker_host=effective_broker.host,
        effective_broker_account_id=effective_broker.account_id,
        broker_config_hash=effective_broker.config_hash,
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
    return evaluate_execution_semantics(
        parsed,
        shared_runtime_config(),
        broker_config=shared_broker_connection_config(),
    )


def effective_send_orders(settings: dict[str, Any] | None = None, runtime_config: Any | None = None) -> bool:
    return evaluate_execution_semantics(settings or {}, runtime_config).effective_send_orders


def opens_effective_send_orders(before: ExecutionSemantics, after: ExecutionSemantics) -> bool:
    return bool(not before.effective_send_orders and after.effective_send_orders)
