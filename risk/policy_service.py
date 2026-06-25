"""Unified risk policy service.

Phase B entry point for high-impact live decisions.  The service wraps the
existing RiskGovernor and adds action-specific checks so callers can record a
single auditable risk verdict instead of scattering pre-trade branches.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from risk.governor import GovernorState, RiskGovernor


@dataclass
class RiskVerdict:
    allowed: bool
    reason: str = "ok"
    severity: str = "info"
    max_size: float = 0.0
    required_mode: str = "live"
    audit_payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class RiskPolicyService:
    """Single facade for risk decisions.

    Supported actions start with ``open_trade`` and now include conservative
    non-execution gates for policy/model governance.
    """

    _instance: "RiskPolicyService | None" = None

    def __init__(self, governor: RiskGovernor | None = None):
        self.governor = governor or RiskGovernor.shared()

    @classmethod
    def shared(cls) -> "RiskPolicyService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None

    def evaluate(self, action: str, context: dict[str, Any] | None = None) -> RiskVerdict:
        context = context or {}
        if action == "open_trade":
            return self._evaluate_open_trade(context)
        if action == "close_position":
            return self._evaluate_close_position(context)
        if action == "update_weight":
            return self._evaluate_governor_action(action, context, "allow_weight_update")
        if action == "promote_factor":
            return self._evaluate_governor_action(action, context, "allow_promotion")
        if action == "register_factor":
            return self._evaluate_governor_action(action, context, "allow_new_factor")
        if action == "start_shadow_model":
            return self._evaluate_model_stage(action, context, required_mode="shadow")
        if action == "start_canary_model":
            return self._evaluate_model_stage(action, context, required_mode="canary")
        return RiskVerdict(
            allowed=False,
            reason="unsupported_action",
            severity="error",
            audit_payload={"action": action},
        )

    def _evaluate_open_trade(self, context: dict[str, Any]) -> RiskVerdict:
        state = self._build_governor_state(context)
        gov_verdict = self.governor.allow_trade(state)
        if not gov_verdict.allowed:
            return RiskVerdict(
                allowed=False,
                reason=gov_verdict.reason or "governor_block",
                severity="error",
                audit_payload={
                    "action": "open_trade",
                    "source": "RiskGovernor",
                    "suggestion": gov_verdict.suggestion,
                    "state": state.extra,
                },
            )

        risk_snapshot = context.get("risk_snapshot") or {}
        var_cfg = context.get("var") or {}
        if bool(var_cfg.get("enabled", False)):
            var_data = risk_snapshot.get("var") or {}
            var_pct = float(var_data.get("var_pct", 0.0) or 0.0)
            threshold_pct = float(var_cfg.get("threshold_pct", 0.0) or 0.0)
            if threshold_pct > 0 and var_pct > threshold_pct:
                return RiskVerdict(
                    allowed=False,
                    reason=f"var_gate: VaR={var_pct:.1f}% > {threshold_pct:.1f}%",
                    severity="error",
                    audit_payload={
                        "action": "open_trade",
                        "source": "var_gate",
                        "var_pct": var_pct,
                        "threshold_pct": threshold_pct,
                    },
                )

        open_position_count = int(context.get("open_position_count", 0) or 0)
        max_position_count = int(context.get("max_position_count", 0) or 0)
        if max_position_count > 0 and open_position_count >= max_position_count:
            return RiskVerdict(
                allowed=False,
                reason=f"仓位上限: {open_position_count}/{max_position_count}",
                severity="error",
                audit_payload={
                    "action": "open_trade",
                    "source": "position_count",
                    "open_position_count": open_position_count,
                    "max_position_count": max_position_count,
                },
            )

        requested_api_volume = float(context.get("requested_api_volume", 0.0) or 0.0)
        total_api_volume = float(context.get("total_api_volume", 0.0) or 0.0)
        max_api_volume = float(context.get("max_position_api_volume", 0.0) or 0.0)
        remaining_api_volume = max(0.0, max_api_volume - total_api_volume) if max_api_volume > 0 else 0.0
        if max_api_volume > 0 and total_api_volume + requested_api_volume > max_api_volume:
            return RiskVerdict(
                allowed=False,
                reason=f"API量上限: {total_api_volume:.0f}+{requested_api_volume:.0f}>{max_api_volume:.0f}",
                severity="error",
                max_size=remaining_api_volume,
                audit_payload={
                    "action": "open_trade",
                    "source": "api_volume",
                    "total_api_volume": total_api_volume,
                    "requested_api_volume": requested_api_volume,
                    "max_position_api_volume": max_api_volume,
                },
            )

        if bool(context.get("pyramid_enabled", True)) and open_position_count > 0:
            max_entry_score = float(context.get("max_abs_entry_score", 0.0) or 0.0)
            signal_score = abs(float(context.get("signal_score", 0.0) or 0.0))
            if max_entry_score > 0 and max_entry_score >= signal_score:
                return RiskVerdict(
                    allowed=False,
                    reason=f"金字塔: 需超 {max_entry_score:.4f}",
                    severity="warn",
                    audit_payload={
                        "action": "open_trade",
                        "source": "pyramid",
                        "max_abs_entry_score": max_entry_score,
                        "signal_score": signal_score,
                    },
                )

        return RiskVerdict(
            allowed=True,
            reason="ok",
            max_size=remaining_api_volume,
            audit_payload={
                "action": "open_trade",
                "source": "risk_policy",
                "open_position_count": open_position_count,
                "total_api_volume": total_api_volume,
                "requested_api_volume": requested_api_volume,
                "max_position_count": max_position_count,
                "max_position_api_volume": max_api_volume,
            },
        )

    def _build_governor_state(self, context: dict[str, Any]) -> GovernorState:
        account = context.get("account") or {}
        session = context.get("session") or {}
        session_pnl = float(session.get("pnl", 0.0) or 0.0)
        start_balance = float(session.get("start_balance", 0.0) or 0.0)
        daily_loss_pct = float(session.get("daily_loss_pct", 0.0) or 0.0)
        if daily_loss_pct <= 0 and session_pnl < 0 and start_balance > 0:
            daily_loss_pct = abs(session_pnl) / start_balance * 100.0
        extra = {
            "open_position_count": int(context.get("open_position_count", 0) or 0),
            "requested_api_volume": float(context.get("requested_api_volume", 0.0) or 0.0),
            "total_api_volume": float(context.get("total_api_volume", 0.0) or 0.0),
        }
        return GovernorState(
            balance=float(account.get("balance", 0.0) or 0.0),
            equity=float(account.get("equity", 0.0) or 0.0),
            drawdown_pct=float(session.get("drawdown_pct", 0.0) or 0.0),
            open_positions=int(context.get("open_position_count", 0) or 0),
            consecutive_losses=int(session.get("consecutive_losses", 0) or 0),
            daily_trades=int(session.get("trades", 0) or 0),
            daily_loss_pct=daily_loss_pct,
            circuit_broken=bool(session.get("circuit_breaker", False)),
            data_lag_seconds=float(context.get("data_lag_seconds", 0.0) or 0.0),
            loop_running=bool(context.get("loop_running", True)),
            bridge_connected=bool(context.get("bridge_connected", True)),
            extra=extra,
        )

    def _evaluate_close_position(self, context: dict[str, Any]) -> RiskVerdict:
        return RiskVerdict(
            allowed=True,
            reason="risk_reducing_action",
            required_mode=str(context.get("mode") or "live"),
            audit_payload={
                "action": "close_position",
                "source": "risk_policy",
                "position_id": context.get("position_id", ""),
                "close_reason": context.get("close_reason", ""),
            },
        )

    def _evaluate_governor_action(
        self,
        action: str,
        context: dict[str, Any],
        method_name: str,
    ) -> RiskVerdict:
        state = self._build_governor_state(context)
        method = getattr(self.governor, method_name)
        gov_verdict = method(state)
        if not gov_verdict.allowed:
            return RiskVerdict(
                allowed=False,
                reason=gov_verdict.reason or "governor_block",
                severity="error",
                required_mode=str(context.get("required_mode") or "governed"),
                audit_payload={
                    "action": action,
                    "source": "RiskGovernor",
                    "suggestion": gov_verdict.suggestion,
                    "state": state.extra,
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode=str(context.get("required_mode") or "governed"),
            audit_payload={
                "action": action,
                "source": "risk_policy",
                "state": state.extra,
            },
        )

    def _evaluate_model_stage(
        self,
        action: str,
        context: dict[str, Any],
        *,
        required_mode: str,
    ) -> RiskVerdict:
        capabilities = context.get("capabilities") or {}
        live_trading = bool(context.get("live_trading", False) or capabilities.get("live_trading", False))
        if live_trading:
            return RiskVerdict(
                allowed=False,
                reason="live_trading_capability_not_allowed",
                severity="error",
                required_mode=required_mode,
                audit_payload={
                    "action": action,
                    "source": "model_guardrail",
                    "candidate_id": context.get("candidate_id", ""),
                    "capabilities": capabilities,
                },
            )
        allowed_statuses = set(context.get("allowed_statuses") or [])
        candidate_status = str(context.get("candidate_status") or "")
        if allowed_statuses and candidate_status and candidate_status not in allowed_statuses:
            return RiskVerdict(
                allowed=False,
                reason="candidate_status_not_allowed",
                severity="error",
                required_mode=required_mode,
                audit_payload={
                    "action": action,
                    "source": "model_stage",
                    "candidate_id": context.get("candidate_id", ""),
                    "candidate_status": candidate_status,
                    "allowed_statuses": sorted(allowed_statuses),
                },
            )
        return RiskVerdict(
            allowed=True,
            reason="ok",
            required_mode=required_mode,
            audit_payload={
                "action": action,
                "source": "risk_policy",
                "candidate_id": context.get("candidate_id", ""),
                "candidate_status": candidate_status,
                "capabilities": capabilities,
            },
        )
