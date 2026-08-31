"""Canonical risk snapshot and frozen forward-risk projection."""
from __future__ import annotations

from backend.core.hash import canonical_hash
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from backend.risk.concentration import ConcentrationChecker
from backend.risk.kelly import KellyCriterion
from backend.risk.stress_test import StressTest, _direction
from backend.risk.var import VaRCalculator


SNAPSHOT_KEY = "risk_metrics_snapshot.v2"
FORWARD_VAR_INPUT_SCHEMA = "forward_var_input.v1"
_INTERNAL_FORWARD_VAR_INPUT = "_forward_var_input"




@dataclass(frozen=True)
class FrozenForwardVarInput:
    status: str
    reason: str
    symbol: str
    timeframe: str
    as_of: float
    source_window_start: str
    source_window_end: str
    sample_count: int
    returns: tuple[float, ...]
    input_fingerprint: str
    schema_version: str = FORWARD_VAR_INPUT_SCHEMA

    def to_dict(self, *, include_returns: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload["returns"] = list(self.returns) if include_returns else []
        return payload

    def compact_dict(self) -> dict[str, Any]:
        payload = self.to_dict(include_returns=False)
        payload.pop("returns", None)
        return payload

    @classmethod
    def from_mapping(
        cls,
        payload: Mapping[str, Any] | None,
    ) -> "FrozenForwardVarInput":
        raw = dict(payload or {})
        if not raw:
            return freeze_closed_bar_returns(
                [],
                symbol="",
                timeframe="",
                as_of=0.0,
            )
        try:
            returns = tuple(float(value) for value in (raw.get("returns") or []))
            as_of = float(raw.get("as_of") or 0.0)
            sample_count = int(raw.get("sample_count") or len(returns))
        except (TypeError, ValueError, OverflowError):
            return freeze_closed_bar_returns(
                [],
                symbol=str(raw.get("symbol") or ""),
                timeframe=str(raw.get("timeframe") or ""),
                as_of=0.0,
                invalid_reason="invalid_frozen_forward_var_input",
            )
        return cls(
            status=str(raw.get("status") or "unknown"),
            reason=str(raw.get("reason") or ""),
            symbol=str(raw.get("symbol") or ""),
            timeframe=str(raw.get("timeframe") or ""),
            as_of=as_of,
            source_window_start=str(raw.get("source_window_start") or ""),
            source_window_end=str(raw.get("source_window_end") or ""),
            sample_count=sample_count,
            returns=returns,
            input_fingerprint=str(raw.get("input_fingerprint") or ""),
            schema_version=str(
                raw.get("schema_version") or FORWARD_VAR_INPUT_SCHEMA
            ),
        )


def freeze_closed_bar_returns(
    closes: Sequence[Any],
    *,
    timestamps: Sequence[Any] | None = None,
    symbol: str,
    timeframe: str,
    as_of: float,
    lookback: int = 500,
    invalid_reason: str = "",
) -> FrozenForwardVarInput:
    raw_closes = list(closes or [])
    raw_timestamps = list(timestamps or [])
    if invalid_reason:
        status = "error"
        reason = invalid_reason
        values: list[float] = []
    elif not raw_closes:
        status = "unknown"
        reason = "missing_closed_bar_prices"
        values = []
    else:
        try:
            values = [float(value) for value in raw_closes]
        except (TypeError, ValueError, OverflowError):
            values = []
            status = "error"
            reason = "invalid_closed_bar_prices"
        else:
            if (
                any(not math.isfinite(value) or value <= 0 for value in values)
                or (raw_timestamps and len(raw_timestamps) != len(values))
            ):
                values = []
                status = "error"
                reason = "invalid_closed_bar_prices"
            else:
                status = "known"
                reason = ""

    max_prices = max(2, int(lookback) + 1)
    values = values[-max_prices:]
    if raw_timestamps:
        raw_timestamps = (
            raw_timestamps[-len(values):]
            if values
            else []
        )
    returns = tuple(
        (values[index] - values[index - 1]) / values[index - 1]
        for index in range(1, len(values))
    )
    source_start = str(raw_timestamps[0]) if raw_timestamps else ""
    source_end = str(raw_timestamps[-1]) if raw_timestamps else ""
    fingerprint_input = {
        "schema_version": FORWARD_VAR_INPUT_SCHEMA,
        "status": status,
        "reason": reason,
        "symbol": str(symbol or ""),
        "timeframe": str(timeframe or ""),
        "as_of": float(as_of or 0.0),
        "source_window_start": source_start,
        "source_window_end": source_end,
        "returns": returns,
    }
    return FrozenForwardVarInput(
        status=status,
        reason=reason,
        symbol=str(symbol or ""),
        timeframe=str(timeframe or ""),
        as_of=float(as_of or 0.0),
        source_window_start=source_start,
        source_window_end=source_end,
        sample_count=len(returns),
        returns=returns,
        input_fingerprint=canonical_hash(fingerprint_input),
    )


def _symbol_key(value: Any) -> str:
    return str(value or "").strip().upper().rstrip("+")


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _contract_size(
    symbol: str,
    contract_sizes: Mapping[str, Any] | None,
) -> float | None:
    target = _symbol_key(symbol)
    for name, raw in dict(contract_sizes or {}).items():
        if _symbol_key(name) != target:
            continue
        value = _number(raw)
        return value if value is not None and value > 0 else None
    return None


def _position_notional(
    position: Mapping[str, Any],
    *,
    contract_sizes: Mapping[str, Any] | None,
) -> float | None:
    explicit = _number(position.get("notional_usd"))
    if explicit is not None:
        return explicit if explicit >= 0 else None
    price = _number(
        position.get("current_price")
        or position.get("price_current")
    )
    lots = _number(position.get("volume_lots"))
    if lots is not None:
        contract = _number(position.get("contract_size"))
        if contract is None:
            contract = _contract_size(
                str(position.get("symbol") or ""),
                contract_sizes,
            )
        if price is None or contract is None or lots < 0:
            return None
        return price * lots * contract
    api_volume = _number(
        position.get("api_volume")
        if position.get("api_volume") is not None
        else position.get("volume")
    )
    contract = _contract_size(
        str(position.get("symbol") or ""),
        contract_sizes,
    )
    if (
        price is None
        or api_volume is None
        or api_volume < 0
        or contract is None
    ):
        return None
    return price * api_volume / 10_000.0 * contract


def _unavailable_forward_var(
    *,
    confidence: float,
    status: str,
    reason: str,
    frozen_input: FrozenForwardVarInput,
    current_equity: float | None,
) -> dict[str, Any]:
    payload = VaRCalculator(confidence=confidence)._empty(
        status,
        reason,
        sample_count=frozen_input.sample_count,
        current_equity=current_equity,
        timeframe=frozen_input.timeframe,
    )
    payload["input_fingerprint"] = frozen_input.input_fingerprint
    return payload


def calculate_forward_var(
    *,
    frozen_input: FrozenForwardVarInput,
    positions: Sequence[Mapping[str, Any]] | None,
    account: Mapping[str, Any] | None,
    candidate: Mapping[str, Any] | None = None,
    contract_sizes: Mapping[str, Any] | None = None,
    confidence: float = 0.95,
    lookback: int = 500,
) -> dict[str, Any]:
    equity = _number((account or {}).get("equity"))
    if frozen_input.schema_version != FORWARD_VAR_INPUT_SCHEMA:
        return _unavailable_forward_var(
            confidence=confidence,
            status="unknown",
            reason="forward_var_input_contract_missing",
            frozen_input=frozen_input,
            current_equity=equity,
        )
    if frozen_input.status != "known":
        return _unavailable_forward_var(
            confidence=confidence,
            status=frozen_input.status,
            reason=frozen_input.reason or "forward_var_input_unavailable",
            frozen_input=frozen_input,
            current_equity=equity,
        )
    if positions is None:
        return _unavailable_forward_var(
            confidence=confidence,
            status="unknown",
            reason="positions_missing",
            frozen_input=frozen_input,
            current_equity=equity,
        )
    if equity is None or equity <= 0:
        return _unavailable_forward_var(
            confidence=confidence,
            status="unknown",
            reason="account_equity_missing",
            frozen_input=frozen_input,
            current_equity=equity,
        )

    current_net_notional = 0.0
    for position in positions:
        symbol = str(position.get("symbol") or frozen_input.symbol)
        if _symbol_key(symbol) != _symbol_key(frozen_input.symbol):
            return _unavailable_forward_var(
                confidence=confidence,
                status="unknown",
                reason="position_return_distribution_mismatch",
                frozen_input=frozen_input,
                current_equity=equity,
            )
        direction = _direction(position)
        notional = _position_notional(
            position,
            contract_sizes=contract_sizes,
        )
        if direction == 0 or notional is None:
            return _unavailable_forward_var(
                confidence=confidence,
                status="unknown",
                reason="invalid_position_input",
                frozen_input=frozen_input,
                current_equity=equity,
            )
        current_net_notional += direction * notional

    candidate_notional = 0.0
    candidate_signed_notional = 0.0
    candidate_direction = 0
    if candidate is not None:
        candidate_symbol = str(candidate.get("symbol") or frozen_input.symbol)
        candidate_direction = _direction(candidate)
        candidate_price = _number(candidate.get("current_price"))
        candidate_volume = _number(candidate.get("requested_api_volume"))
        candidate_contract = _number(candidate.get("contract_size"))
        if candidate_contract is None:
            candidate_contract = _contract_size(
                candidate_symbol,
                contract_sizes,
            )
        if (
            _symbol_key(candidate_symbol) != _symbol_key(frozen_input.symbol)
            or candidate_direction == 0
            or candidate_price is None
            or candidate_price <= 0
            or candidate_volume is None
            or candidate_volume < 0
            or candidate_contract is None
            or candidate_contract <= 0
        ):
            return _unavailable_forward_var(
                confidence=confidence,
                status="unknown",
                reason="invalid_candidate_notional_input",
                frozen_input=frozen_input,
                current_equity=equity,
            )
        candidate_notional = (
            candidate_price
            * candidate_volume
            / 10_000.0
            * candidate_contract
        )
        candidate_signed_notional = candidate_direction * candidate_notional

    forward_net_notional = current_net_notional + candidate_signed_notional
    result = VaRCalculator(confidence=confidence).calculate_forward(
        list(frozen_input.returns),
        net_notional_usd=forward_net_notional,
        current_equity=equity,
        lookback=lookback,
        timeframe=frozen_input.timeframe,
    )
    result.update(
        {
            "symbol": frozen_input.symbol,
            "source_window_start": frozen_input.source_window_start,
            "source_window_end": frozen_input.source_window_end,
            "current_net_notional_usd": round(current_net_notional, 8),
            "candidate_direction": candidate_direction,
            "candidate_notional_usd": round(candidate_notional, 8),
            "candidate_signed_notional_usd": round(
                candidate_signed_notional,
                8,
            ),
            "forward_net_notional_usd": round(forward_net_notional, 8),
            "input_fingerprint": canonical_hash(
                {
                    "frozen_input": frozen_input.input_fingerprint,
                    "positions": list(positions),
                    "candidate": dict(candidate or {}),
                    "account_equity": equity,
                    "confidence": confidence,
                    "lookback": lookback,
                }
            ),
        }
    )
    return result


@dataclass(frozen=True)
class RiskMetricsSnapshot:
    status: str
    as_of: float
    source_window_start: str
    source_window_end: str
    sample_count: int
    distinct_position_count: int
    method: str
    alpha: float
    horizon: str
    var_usd: float | None
    cvar_usd: float | None
    var_fraction: float | None
    cvar_fraction: float | None
    var_pct: float | None
    cvar_pct: float | None
    kelly_fraction_raw: float | None
    kelly_fraction_bounded: float | None
    stress_loss_pct: float | None
    concentration_pct: float | None
    input_fingerprint: str
    account_reconcile_id: str
    positions_reconcile_id: str
    blockers: tuple[str, ...]
    components: dict[str, Any]
    schema_version: str = SNAPSHOT_KEY

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["blockers"] = list(self.blockers)
        return payload


def build_risk_metrics_snapshot(
    *,
    forward_var_input: FrozenForwardVarInput,
    clean_trade_pnls: list[float],
    positions: list[dict[str, Any]] | None,
    account: dict[str, Any] | None,
    account_reconcile_id: str,
    positions_reconcile_id: str,
    as_of: float,
    kelly_min_closed_trades: int = 20,
    kelly_multiplier: float = 0.5,
    kelly_max_fraction: float = 0.25,
    var_confidence: float = 0.95,
    var_lookback: int = 500,
) -> RiskMetricsSnapshot:
    var = calculate_forward_var(
        frozen_input=forward_var_input,
        positions=positions,
        account=account,
        confidence=var_confidence,
        lookback=var_lookback,
    )
    var_shadow_99 = calculate_forward_var(
        frozen_input=forward_var_input,
        positions=positions,
        account=account,
        confidence=0.99,
        lookback=var_lookback,
    )
    stress = StressTest().run(positions, account)
    weights = None
    if positions is not None and all(
        "notional_usd" in item for item in positions
    ):
        weights = {}
        for index, item in enumerate(positions):
            asset = str(
                item.get("symbol")
                or item.get("asset")
                or item.get("position_id")
                or index
            )
            weights[asset] = (
                weights.get(asset, 0.0) + float(item["notional_usd"])
            )
    concentration = ConcentrationChecker().check(weights)
    if concentration.get("status") == "known":
        concentration["applicable"] = len(weights or {}) > 1

    wins = [pnl for pnl in clean_trade_pnls if pnl > 0]
    losses = [-pnl for pnl in clean_trade_pnls if pnl < 0]
    closed_trades = len(wins) + len(losses)
    if (
        closed_trades >= max(1, int(kelly_min_closed_trades))
        and wins
        and losses
    ):
        kelly = KellyCriterion().calculate(
            len(wins) / (len(wins) + len(losses)),
            sum(wins) / len(wins),
            sum(losses) / len(losses),
        )
    else:
        kelly = KellyCriterion().get_status()
    kelly["closed_trades"] = closed_trades
    kelly["sample_count"] = closed_trades
    raw_kelly = kelly.get("kelly_fraction")
    bounded_kelly = (
        min(
            float(raw_kelly) * float(kelly_multiplier),
            float(kelly_max_fraction),
        )
        if raw_kelly is not None
        else None
    )

    blockers = [
        f"{name}:{component.get('status')}"
        for name, component in (
            ("var", var),
            ("kelly", kelly),
            ("stress", stress),
            ("concentration", concentration),
        )
        if component.get("status") != "known"
    ]
    if not account_reconcile_id:
        blockers.append("account_reconcile_missing")
    if not positions_reconcile_id:
        blockers.append("positions_reconcile_missing")
    component_statuses = {
        str(component.get("status") or "unknown")
        for component in (var, kelly, stress, concentration)
    }
    status = (
        "error"
        if "error" in component_statuses
        else "unknown"
        if "unknown" in component_statuses
        or not account_reconcile_id
        or not positions_reconcile_id
        else "warming_up"
        if blockers
        else "known"
    )
    fingerprint_input = {
        "forward_var_input": forward_var_input.input_fingerprint,
        "clean_trade_pnls": clean_trade_pnls,
        "positions": positions,
        "account_equity": (account or {}).get("equity"),
        "account_reconcile_id": account_reconcile_id,
        "positions_reconcile_id": positions_reconcile_id,
        "kelly": {
            "min_closed_trades": kelly_min_closed_trades,
            "multiplier": kelly_multiplier,
            "max_fraction": kelly_max_fraction,
        },
        "var": {
            "confidence": var_confidence,
            "shadow_confidence": 0.99,
            "lookback": var_lookback,
        },
    }
    return RiskMetricsSnapshot(
        status=status,
        as_of=float(as_of),
        source_window_start=forward_var_input.source_window_start,
        source_window_end=forward_var_input.source_window_end,
        sample_count=int(var.get("sample_count") or 0),
        distinct_position_count=len(positions or []),
        method=str(var.get("method") or "historical"),
        alpha=float(var.get("alpha") or var_confidence),
        horizon=str(var.get("horizon") or "one_closed_bar"),
        var_usd=var.get("var_usd"),
        cvar_usd=var.get("cvar_usd"),
        var_fraction=var.get("var_fraction"),
        cvar_fraction=var.get("cvar_fraction"),
        var_pct=var.get("var_pct"),
        cvar_pct=var.get("cvar_pct"),
        kelly_fraction_raw=raw_kelly,
        kelly_fraction_bounded=bounded_kelly,
        stress_loss_pct=stress.get("stress_loss_pct"),
        concentration_pct=concentration.get("concentration_pct"),
        input_fingerprint=canonical_hash(fingerprint_input),
        account_reconcile_id=str(account_reconcile_id or ""),
        positions_reconcile_id=str(positions_reconcile_id or ""),
        blockers=tuple(blockers),
        components={
            "var": var,
            "var_shadow_99": var_shadow_99,
            "forward_var_input": forward_var_input.compact_dict(),
            "kelly": kelly,
            "stress": stress,
            "concentration": concentration,
        },
    )


def project_candidate_risk_snapshot(
    risk_snapshot: Mapping[str, Any] | None,
    *,
    positions: Sequence[Mapping[str, Any]] | None,
    account: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
    contract_sizes: Mapping[str, Any] | None,
    var_confidence: float = 0.95,
    var_lookback: int = 500,
) -> dict[str, Any]:
    projected = dict(risk_snapshot or {})
    raw_input = projected.pop(_INTERNAL_FORWARD_VAR_INPUT, None)
    frozen_input = FrozenForwardVarInput.from_mapping(
        raw_input if isinstance(raw_input, Mapping) else None
    )
    current_var = dict(projected.get("var") or {})
    candidate_var = calculate_forward_var(
        frozen_input=frozen_input,
        positions=positions,
        account=account,
        candidate=candidate,
        contract_sizes=contract_sizes,
        confidence=var_confidence,
        lookback=var_lookback,
    )
    candidate_shadow_99 = calculate_forward_var(
        frozen_input=frozen_input,
        positions=positions,
        account=account,
        candidate=candidate,
        contract_sizes=contract_sizes,
        confidence=0.99,
        lookback=var_lookback,
    )
    projected.update(
        {
            "current_var": current_var,
            "var": candidate_var,
            "var_shadow_99": candidate_shadow_99,
            "candidate_forward_risk": {
                "schema_version": "candidate_forward_risk.v1",
                "status": candidate_var.get("status") or "unknown",
                "input_fingerprint": candidate_var.get("input_fingerprint")
                or frozen_input.input_fingerprint,
                "current_net_notional_usd": candidate_var.get(
                    "current_net_notional_usd"
                ),
                "candidate_notional_usd": candidate_var.get(
                    "candidate_notional_usd"
                ),
                "forward_net_notional_usd": candidate_var.get(
                    "forward_net_notional_usd"
                ),
            },
        }
    )
    return projected


def attach_internal_forward_var_input(
    snapshot: Mapping[str, Any],
    frozen_input: FrozenForwardVarInput,
) -> dict[str, Any]:
    return {
        **dict(snapshot),
        _INTERNAL_FORWARD_VAR_INPUT: frozen_input.to_dict(),
    }
