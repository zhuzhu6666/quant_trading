"""Config service — read/edit config/settings.yaml."""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import yaml

from backend.core.paths import CONFIG_DIR
from backend.services.execution_semantics import (
    evaluate_execution_semantics,
    opens_effective_send_orders,
    validate_execution_semantics,
)
from backend.services.mutation_audit import confirm_header_valid, record_api_mutation
from config.runtime_config import RuntimeConfig, replace as replace_runtime_config


SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
BACKUP_SUFFIX = ".bak"
TEMP_SUFFIX = ".tmp"

_POSITIVE_FLOAT_FIELDS = {
    "risk_sl_atr",
    "risk_tp_atr",
    "risk_max_drawdown_pct",
    "risk_max_daily_loss_pct",
    "risk_data_lag_max_seconds",
    "risk_var_threshold_pct",
    "risk_cvar_threshold_pct",
    "strategy_sl_atr",
    "strategy_tp_atr",
    "max_position_volume",
    "max_position_api_volume",
    "kelly_max_pct",
    "kelly_risk_per_trade_pct",
    "dynamic_sizing_max_api_volume",
    "dynamic_sizing_api_units_per_display_unit",
}
_POSITIVE_INT_FIELDS = {
    "demo_learning_max_daily_trades",
    "max_position_count",
    "risk_max_consecutive_losses",
    "risk_max_daily_trades",
    "sync_interval_sec",
    "l2_write_batch_size",
    "var_window",
}
_NON_NEGATIVE_INT_FIELDS = {
    "risk_cooldown_bars",
    "risk_loss_cooldown_after_losses",
    "risk_loss_cooldown_bars",
    "risk_supervisor_reentry_cooldown_bars",
    "risk_max_holding_bars",
    "sync_recovery_max_attempts",
    "cross_asset_covariance_window",
    "cross_asset_update_interval",
    "algo_duration_minutes",
}
_RATIO_FIELDS = {
    "factor_tactical_alpha",
    "factor_signal_threshold",
    "kelly_fraction",
    "kelly_max_pct",
}


def _numeric_value(runtime: dict[str, Any], field: str) -> float | int:
    value = runtime.get(field)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"invalid_runtime_value: {field} must be numeric")
    if not isinstance(value, (int, float)):
        raise ValueError(f"invalid_runtime_value: {field} must be numeric")
    return value


def _validate_runtime_bounds(runtime: dict[str, Any]) -> None:
    """Validate high-impact live risk controls after RuntimeConfig merge."""
    for field in sorted(_POSITIVE_FLOAT_FIELDS):
        value = float(_numeric_value(runtime, field))
        if value <= 0:
            raise ValueError(f"invalid_runtime_value: {field} must be > 0")

    for field in sorted(_POSITIVE_INT_FIELDS):
        value = _numeric_value(runtime, field)
        if int(value) != value or int(value) <= 0:
            raise ValueError(f"invalid_runtime_value: {field} must be a positive integer")

    for field in sorted(_NON_NEGATIVE_INT_FIELDS):
        value = _numeric_value(runtime, field)
        if int(value) != value or int(value) < 0:
            raise ValueError(f"invalid_runtime_value: {field} must be an integer >= 0")

    for field in sorted(_RATIO_FIELDS):
        value = float(_numeric_value(runtime, field))
        if not 0 < value <= 1:
            raise ValueError(f"invalid_runtime_value: {field} must be > 0 and <= 1")


def _validate_parsed_runtime_config(parsed: Any) -> None:
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValueError("settings_yaml_must_be_mapping")
    runtime = RuntimeConfig.from_yaml(parsed).to_dict()
    _validate_runtime_bounds(runtime)
    validate_execution_semantics(parsed, RuntimeConfig.from_dict(runtime))


def get_config() -> dict:
    """Read settings.yaml and return both the raw yaml text and the parsed dict."""
    if not SETTINGS_PATH.exists():
        return {"yaml": "", "parsed": {}, "path": str(SETTINGS_PATH), "exists": False}
    text = SETTINGS_PATH.read_text(encoding="utf-8")
    try:
        parsed = yaml.safe_load(text) or {}
    except yaml.YAMLError as e:
        return {"yaml": text, "parsed": {}, "path": str(SETTINGS_PATH), "exists": True, "parse_error": str(e)}
    return {"yaml": text, "parsed": parsed, "path": str(SETTINGS_PATH), "exists": True}


def config_runtime_drift(
    parsed: dict[str, Any] | None = None,
    *,
    include_overlay: bool | None = None,
) -> dict[str, Any]:
    """Compare the effective YAML+overlay authority with the in-memory singleton."""
    from config.runtime_config import config_from_overlay, shared as shared_runtime_config

    if parsed is None:
        parsed = get_config().get("parsed") or {}
    if not isinstance(parsed, dict):
        parsed = {}
    disk_runtime = RuntimeConfig.from_yaml(parsed)
    expected_runtime = disk_runtime
    overlay_status: dict[str, Any] = {"ok": False, "status": "not_checked", "overlay": {}}
    if include_overlay is None:
        include_overlay = not (os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"))
    if include_overlay:
        try:
            from backend.services.runtime_config_overlay import RuntimeConfigOverlayService

            overlay_status = RuntimeConfigOverlayService().latest()
            if overlay_status.get("ok"):
                expected_runtime = config_from_overlay(overlay_status.get("overlay") or {})
        except Exception as exc:
            overlay_status = {
                "ok": False,
                "status": "unavailable",
                "error": f"{type(exc).__name__}: {exc}",
                "overlay": {},
            }
    memory_runtime = shared_runtime_config()
    disk_semantics = evaluate_execution_semantics(parsed, disk_runtime)
    expected_semantics = evaluate_execution_semantics(parsed, expected_runtime)
    memory_semantics = evaluate_execution_semantics(parsed, memory_runtime)
    disk_dict = disk_runtime.to_dict()
    expected_dict = expected_runtime.to_dict()
    memory_dict = memory_runtime.to_dict()
    changed_keys = sorted(k for k in expected_dict if expected_dict.get(k) != memory_dict.get(k))
    overlay_changed_keys = sorted(k for k in disk_dict if disk_dict.get(k) != expected_dict.get(k))
    semantic_drift = expected_semantics.to_dict() != memory_semantics.to_dict()
    return {
        "drift": bool(changed_keys or semantic_drift),
        "changed_keys": changed_keys[:200],
        "changed_key_count": len(changed_keys),
        "semantic_drift": semantic_drift,
        "authority": "yaml_plus_runtime_overlay" if overlay_status.get("ok") else "yaml",
        "overlay_status": str(overlay_status.get("status") or ""),
        "overlay_changed_keys": overlay_changed_keys[:200],
        "overlay_changed_key_count": len(overlay_changed_keys),
        "disk_execution_semantics": disk_semantics.to_dict(),
        "expected_execution_semantics": expected_semantics.to_dict(),
        "memory_execution_semantics": memory_semantics.to_dict(),
    }


def _parsed_from_text(yaml_text: str) -> dict[str, Any]:
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        line_no = (line.line + 1) if line else None
        col_no = (line.column + 1) if line else None
        raise ValueError(f"yaml_parse_error: line={line_no} col={col_no} msg={e}")
    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValueError("settings_yaml_must_be_mapping")
    return parsed


def put_config(
    yaml_text: str,
    *,
    x_confirm: str | None = None,
    user: str = "",
    endpoint: str = "/api/config",
    audit: bool = True,
) -> dict:
    """Validate + atomically write settings.yaml. Returns changes summary."""
    parsed = _parsed_from_text(yaml_text)
    _validate_parsed_runtime_config(parsed)

    old = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    if not isinstance(old, dict):
        old = {}
    before_runtime = RuntimeConfig.from_yaml(old)
    before_semantics = evaluate_execution_semantics(old, before_runtime)
    after_runtime = RuntimeConfig.from_yaml(parsed)
    after_semantics = validate_execution_semantics(parsed, after_runtime)
    requires_confirm = opens_effective_send_orders(before_semantics, after_semantics)
    if audit and requires_confirm and not confirm_header_valid(x_confirm, "enable-send-orders"):
        record_api_mutation(
            user=user,
            endpoint=endpoint,
            action="put_config",
            status="blocked",
            before=before_semantics.to_dict(),
            after=after_semantics.to_dict(),
            result={"reason": "missing_x_confirm"},
            reason="missing_x_confirm",
            required_confirm="enable-send-orders",
            confirm_ok=False,
        )
        raise PermissionError("missing_x_confirm: enable-send-orders")
    changes = _diff(old or {}, parsed or {})

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup_path = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.name}{BACKUP_SUFFIX}")
    tmp_path = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.name}{TEMP_SUFFIX}")

    if SETTINGS_PATH.exists():
        shutil.copy2(SETTINGS_PATH, backup_path)

    tmp_path.write_text(yaml_text, encoding="utf-8")
    tmp_path.replace(SETTINGS_PATH)

    drift = config_runtime_drift(parsed)
    requires_restart = bool(drift.get("drift"))
    if audit:
        record_api_mutation(
            user=user,
            endpoint=endpoint,
            action="put_config",
            status="applied",
            before=before_semantics.to_dict(),
            after=after_semantics.to_dict(),
            result={
                "requires_confirm": requires_confirm,
                "requires_restart": requires_restart,
                "change_count": len(changes),
            },
            required_confirm="enable-send-orders" if requires_confirm else "",
            confirm_ok=bool(not requires_confirm or confirm_header_valid(x_confirm, "enable-send-orders")),
        )

    return {
        "ok": True,
        "changes": changes,
        "path": str(SETTINGS_PATH),
        "backup_path": str(backup_path) if backup_path.exists() else None,
        "requires_restart": requires_restart,
        "execution_semantics": after_semantics.to_dict(),
        "config_runtime_drift": drift,
    }


def patch_runtime_config(
    runtime_patch: dict[str, Any],
    *,
    x_confirm: str | None = None,
    user: str = "",
    endpoint: str = "/api/config/runtime",
) -> dict:
    """Patch only the runtime config section with schema validation."""
    if not isinstance(runtime_patch, dict) or not runtime_patch:
        raise ValueError("runtime_patch_must_be_non_empty_object")

    allowed = {k for k in RuntimeConfig.__dataclass_fields__ if k != "extra"}
    unknown = sorted(k for k in runtime_patch if k not in allowed)
    if unknown:
        raise ValueError(f"unknown_runtime_keys: {', '.join(unknown)}")

    current = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    if not isinstance(current, dict):
        current = {}

    before_runtime = RuntimeConfig.from_yaml(current)
    before_semantics = evaluate_execution_semantics(current, before_runtime)
    merged_runtime = RuntimeConfig.from_yaml(current).to_dict()
    merged_runtime.update(runtime_patch)
    validated_runtime = RuntimeConfig.from_dict(merged_runtime)
    _validate_runtime_bounds(validated_runtime.to_dict())
    after_semantics = validate_execution_semantics(current, validated_runtime)

    requires_confirm = opens_effective_send_orders(before_semantics, after_semantics)
    if requires_confirm and not confirm_header_valid(x_confirm, "enable-send-orders"):
        record_api_mutation(
            user=user,
            endpoint=endpoint,
            action="patch_runtime_config",
            status="blocked",
            before=before_semantics.to_dict(),
            after=after_semantics.to_dict(),
            result={"updated_keys": sorted(runtime_patch.keys())},
            reason="missing_x_confirm",
            required_confirm="enable-send-orders",
            confirm_ok=False,
        )
        raise PermissionError("missing_x_confirm: enable-send-orders")

    current["runtime"] = validated_runtime.to_dict()
    ctrader_cfg = current.get("ctrader")
    if not isinstance(ctrader_cfg, dict):
        ctrader_cfg = {}
        current["ctrader"] = ctrader_cfg
    if "ctrader_send_orders" in runtime_patch:
        ctrader_cfg["send_orders"] = bool(validated_runtime.ctrader_send_orders)

    yaml_text = yaml.safe_dump(current, sort_keys=False, allow_unicode=True)
    result = put_config(yaml_text, x_confirm=x_confirm, user=user, endpoint=endpoint, audit=False)
    replace_runtime_config(validated_runtime)
    result["config_runtime_drift"] = config_runtime_drift(current)
    result["requires_restart"] = bool(result["config_runtime_drift"].get("drift"))
    result["runtime"] = validated_runtime.to_dict()
    result["updated_keys"] = sorted(runtime_patch.keys())
    record_api_mutation(
        user=user,
        endpoint=endpoint,
        action="patch_runtime_config",
        status="applied",
        before=before_semantics.to_dict(),
        after=after_semantics.to_dict(),
        result={"updated_keys": result["updated_keys"], "requires_confirm": requires_confirm},
        required_confirm="enable-send-orders" if requires_confirm else "",
        confirm_ok=bool(not requires_confirm or confirm_header_valid(x_confirm, "enable-send-orders")),
    )
    return result


def _diff(a: Any, b: Any, prefix: str = "") -> list[str]:
    """Return a list of 'key: old → new' strings for changed leaves."""
    out: list[str] = []
    if isinstance(a, dict) and isinstance(b, dict):
        keys = set(a.keys()) | set(b.keys())
        for k in keys:
            out.extend(_diff(a.get(k), b.get(k), f"{prefix}{k}."))
    elif a != b:
        out.append(f"{prefix.rstrip('.')}: {a!r} → {b!r}")
    return out
