"""Explicit broker reconciliation contracts used by live safety paths.

This module is deliberately independent from PostgreSQL and the live service
state cache.  It only normalizes fresh broker observations; callers decide how
to publish those observations into compatibility state.
"""
from __future__ import annotations

import time
import uuid
from typing import Any, Mapping

from execution.base import PositionReconcileResult


def reconcile_value(value: Any, field: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(field, default)
    return getattr(value, field, default)


def fresh_observation_timestamp(value: Any, *, max_age_sec: float = 15.0) -> bool:
    """Return true only for a known, recent broker observation timestamp."""

    try:
        observed_at = float(value or 0.0)
    except (TypeError, ValueError):
        return False
    if observed_at <= 0:
        return False
    age = time.time() - observed_at
    return -1.0 <= age <= float(max_age_sec)


def explicit_position_reconcile(bridge: Any) -> Any:
    """Request a fresh position snapshot without cache fallback."""

    if bridge is None or not bool(getattr(bridge, "is_connected", False)):
        return {
            "status": "failed",
            "success": False,
            "fresh": False,
            "authoritative": False,
            "positions": (),
            "observed_at": 0.0,
            "generated_at": time.time(),
            "reconcile_id": f"positions_unavailable_{uuid.uuid4()}",
            "error_code": "broker_not_ready",
        }
    if not hasattr(bridge, "reconcile_positions"):
        return {
            "status": "failed",
            "success": False,
            "fresh": False,
            "authoritative": False,
            "positions": (),
            "observed_at": 0.0,
            "generated_at": time.time(),
            "reconcile_id": f"positions_contract_missing_{uuid.uuid4()}",
            "error_code": "explicit_position_reconcile_missing",
        }
    try:
        result = bridge.reconcile_positions(force=True, allow_cache_fallback=False)
    except TypeError:
        result = bridge.reconcile_positions(force=True)
    except Exception as exc:
        return {
            "status": "failed",
            "success": False,
            "fresh": False,
            "authoritative": False,
            "positions": (),
            "observed_at": 0.0,
            "generated_at": time.time(),
            "reconcile_id": f"positions_exception_{uuid.uuid4()}",
            "error_code": "position_reconcile_exception",
            "error_message": f"{type(exc).__name__}: {exc}",
        }
    status = str(reconcile_value(result, "status", "failed") or "failed")
    observed_at = float(reconcile_value(result, "observed_at", 0.0) or 0.0)
    reconcile_id = str(reconcile_value(result, "reconcile_id", "") or "")
    if (
        status != "fresh"
        or not reconcile_id
        or not fresh_observation_timestamp(observed_at)
    ):
        return {
            "status": "failed",
            "success": False,
            "fresh": False,
            "authoritative": False,
            "positions": tuple(reconcile_value(result, "positions", ()) or ()),
            "observed_at": observed_at,
            "generated_at": float(reconcile_value(result, "generated_at", time.time()) or time.time()),
            "reconcile_id": str(
                reconcile_value(result, "reconcile_id", "")
                or f"positions_stale_{uuid.uuid4()}"
            ),
            "error_code": (
                str(reconcile_value(result, "error_code", "") or "")
                or (
                    "position_reconcile_identity_missing"
                    if not reconcile_id
                    else
                    "position_reconcile_timestamp_unknown"
                    if observed_at <= 0
                    else "position_reconcile_stale"
                )
            ),
            "error_message": str(reconcile_value(result, "error_message", "") or ""),
        }
    return result


def explicit_account_reconcile(
    bridge: Any,
    *,
    positions_reconcile: Any = None,
) -> Any:
    """Request a fresh account snapshot without cache fallback."""

    if bridge is None or not bool(getattr(bridge, "is_connected", False)):
        return None
    if not hasattr(bridge, "reconcile_account"):
        return None
    try:
        kwargs = {"force": True, "allow_cache_fallback": False}
        if isinstance(positions_reconcile, PositionReconcileResult):
            kwargs["confirmed_empty_positions"] = positions_reconcile
        result = bridge.reconcile_account(**kwargs)
    except TypeError:
        # Compatibility for non-cTrader test/adapter bridges that have not
        # adopted the additive empty-position evidence argument.
        try:
            result = bridge.reconcile_account(
                force=True,
                allow_cache_fallback=False,
            )
        except TypeError:
            result = bridge.reconcile_account(force=True)
    except Exception:
        return None
    if str(reconcile_value(result, "status", "failed") or "failed") != "fresh":
        return None
    if not str(reconcile_value(result, "reconcile_id", "") or ""):
        return None
    if not fresh_observation_timestamp(reconcile_value(result, "observed_at", 0.0)):
        return None
    return result


def verify_position_protection_projection(
    reconcile_result: Any,
    *,
    position_id: int,
    expected_stop_loss: float = 0.0,
    expected_take_profit: float = 0.0,
    precision: int = 2,
) -> dict[str, Any]:
    """Verify an amend from a fresh broker position projection.

    An accepted RPC is not proof that SL/TP changed at the broker.  This
    helper deliberately consumes only the explicit reconcile contract and is
    independent from PostgreSQL, audit ledgers, and the process-local position
    cache.  Callers may therefore fail closed for *new* risk without blocking
    or retrying the already-submitted risk-reducing mutation.
    """

    pid = int(position_id or 0)
    status = str(reconcile_value(reconcile_result, "status", "failed") or "failed")
    reconcile_id = str(reconcile_value(reconcile_result, "reconcile_id", "") or "")
    observed_at = float(reconcile_value(reconcile_result, "observed_at", 0.0) or 0.0)
    base = {
        "schema_version": "position_protection_projection.v1",
        "ok": False,
        "position_id": pid,
        "reconcile_id": reconcile_id,
        "reconcile_status": status,
        "observed_at": observed_at,
        "expected_stop_loss": float(expected_stop_loss or 0.0),
        "expected_take_profit": float(expected_take_profit or 0.0),
    }
    if status != "fresh" or not fresh_observation_timestamp(observed_at):
        return {
            **base,
            "reason": str(
                reconcile_value(reconcile_result, "error_code", "")
                or "position_reconcile_failed"
            ),
        }
    if pid <= 0:
        return {**base, "reason": "position_id_required"}

    current: Any | None = None
    for item in tuple(reconcile_value(reconcile_result, "positions", ()) or ()):
        raw_pid = (
            item.get("position_id", item.get("ticket", 0))
            if isinstance(item, Mapping)
            else getattr(item, "position_id", getattr(item, "ticket", 0))
        )
        try:
            matches = int(raw_pid or 0) == pid
        except (TypeError, ValueError):
            matches = False
        if matches:
            current = item
            break
    if current is None:
        return {**base, "reason": "position_missing_after_amend"}

    def _number(*names: str) -> float:
        for name in names:
            raw = (
                current.get(name)
                if isinstance(current, Mapping)
                else getattr(current, name, None)
            )
            if raw is None:
                continue
            try:
                return float(raw or 0.0)
            except (TypeError, ValueError):
                continue
        return 0.0

    actual_sl = _number("sl", "stop_loss", "stopLoss")
    actual_tp = _number("tp", "take_profit", "takeProfit")
    digits = max(0, min(12, int(precision or 0)))
    tolerance = max(1e-9, 0.5 * (10.0 ** (-digits)))
    expected_sl = float(expected_stop_loss or 0.0)
    expected_tp = float(expected_take_profit or 0.0)
    verifiable = expected_sl > 0.0 or expected_tp > 0.0
    sl_matches = expected_sl <= 0.0 or abs(actual_sl - expected_sl) <= tolerance
    tp_matches = expected_tp <= 0.0 or abs(actual_tp - expected_tp) <= tolerance
    payload = {
        **base,
        "actual_stop_loss": actual_sl,
        "actual_take_profit": actual_tp,
        "precision": digits,
        "tolerance": tolerance,
        "sl_matches": sl_matches,
        "tp_matches": tp_matches,
    }
    if not verifiable:
        return {**payload, "reason": "no_verifiable_protection_fields"}
    if not sl_matches:
        return {**payload, "reason": "stop_loss_mismatch"}
    if not tp_matches:
        return {**payload, "reason": "take_profit_mismatch"}
    return {**payload, "ok": True, "reason": "fresh_projection_matches"}
