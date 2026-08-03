"""Shared live/replay factor-component wiring.

This module contains only deterministic configuration projection.  It owns no
runtime state and performs no broker, database, or registry mutation, so the
same projection can be bound into live execution and causal parity replay.
"""

from __future__ import annotations

from typing import Any, Mapping

from loguru import logger


def merge_portfolio_configs(
    signal_config: Mapping[str, Any] | None,
    weight_config: Mapping[str, Any] | None,
    tactical_alpha: float,
    signal_threshold: float,
    macro_direction_cap: float = 0.15,
) -> dict[str, Any]:
    """Project admitted factor config into ``PortfolioCompositor`` input.

    Missing weights are explicit zero.  A discovered factor therefore cannot
    enter scoring through the historical implicit-weight fallback.  If runtime
    admission itself is unavailable, alpha factors fail closed while explicitly
    exempt non-alpha/context factors remain observable.
    """

    signals = dict(signal_config or {})
    weights = dict(weight_config or {})
    try:
        from alpha.runtime_factor_selection import (
            runtime_factor_enabled,
            select_runtime_factors,
        )

        selection = select_runtime_factors(signals)
        selected_names = set(
            selection.selected_factor_ids if selection is not None else signals
        )
        discovered_names = (selected_names - set(signals)) | {
            name
            for name in selected_names
            if isinstance(signals.get(name), dict)
            and signals.get(name, {}).get("source") == "discovered"
        }
    except Exception as exc:
        logger.warning(
            "[factor_wiring] runtime factor admission unavailable; alpha fail-closed: {}",
            exc,
        )
        selected_names = {
            name
            for name, raw_cfg in signals.items()
            if isinstance(raw_cfg, dict)
            and (
                str(raw_cfg.get("role") or "alpha").lower() != "alpha"
                or bool(raw_cfg.get("health_gate_exempt", False))
            )
        }
        discovered_names = set()

    merged: dict[str, Any] = {}
    for name in selected_names:
        raw_signal = signals.get(name, {})
        factor = dict(raw_signal) if isinstance(raw_signal, dict) else {}
        raw_weight = weights.get(name, 0.0)
        weight = (
            raw_weight
            if isinstance(raw_weight, (int, float))
            else (raw_weight or {}).get("weight", 0.0)
        )
        merged[name] = {
            "weight": weight,
            "tags": factor.get(
                "tags",
                ["GP发现"] if name in discovered_names else [],
            ),
            "mode": factor.get("mode", "rank_mapping"),
            "role": factor.get("role", "alpha"),
            "enabled": runtime_factor_enabled(factor),
            "source": factor.get(
                "source",
                "discovered" if name in discovered_names else "builtin",
            ),
        }
    merged["_tactical_alpha"] = tactical_alpha
    merged["_macro_direction_cap"] = macro_direction_cap
    merged["_signal_threshold"] = signal_threshold
    return merged


__all__ = ["merge_portfolio_configs"]
