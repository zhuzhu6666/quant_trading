"""Release-time safety feature flags.

These flags are intentionally outside RuntimeConfig and its autonomous overlay.
They are resolved once per process and require a deployment/restart to change.
"""
from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Mapping


SAFETY_PLANE_MODES = frozenset({"off", "shadow", "enforce"})


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "enabled"}


@dataclass(frozen=True)
class StaticFeatureFlags:
    live_safety_plane_v2_mode: str = "off"
    live_generation_controller_v2_enabled: bool = False
    ctrader_execution_outcome_v2_enabled: bool = False

    @classmethod
    def from_sources(
        cls,
        settings: dict[str, Any] | None,
        environ: Mapping[str, str] | None = None,
    ) -> "StaticFeatureFlags":
        env = os.environ if environ is None else environ
        features = (settings or {}).get("features") if isinstance(settings, dict) else {}
        features = features if isinstance(features, dict) else {}
        safety_mode = str(
            env.get(
                "QUANT_LIVE_SAFETY_PLANE_V2_MODE",
                features.get("live_safety_plane_v2_mode", "off"),
            )
            or "off"
        ).strip().lower()
        if safety_mode not in SAFETY_PLANE_MODES:
            raise ValueError(
                "invalid_live_safety_plane_v2_mode: "
                f"{safety_mode!r}; expected one of {sorted(SAFETY_PLANE_MODES)}"
            )
        return cls(
            live_safety_plane_v2_mode=safety_mode,
            live_generation_controller_v2_enabled=_as_bool(
                env.get(
                    "QUANT_LIVE_GENERATION_CONTROLLER_V2_ENABLED",
                    features.get("live_generation_controller_v2_enabled", False),
                )
            ),
            ctrader_execution_outcome_v2_enabled=_as_bool(
                env.get(
                    "QUANT_CTRADER_EXECUTION_OUTCOME_V2_ENABLED",
                    features.get("ctrader_execution_outcome_v2_enabled", False),
                )
            ),
        )


_LOCK = threading.Lock()
_SHARED: StaticFeatureFlags | None = None


def shared_static_feature_flags() -> StaticFeatureFlags:
    global _SHARED
    with _LOCK:
        if _SHARED is None:
            from config import load_config

            _SHARED = StaticFeatureFlags.from_sources(load_config())
        return _SHARED


def reset_static_feature_flags_for_tests() -> None:
    global _SHARED
    with _LOCK:
        _SHARED = None
