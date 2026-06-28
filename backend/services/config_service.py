"""Config service — read/edit config/settings.yaml."""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from backend.core.paths import CONFIG_DIR
from config.runtime_config import RuntimeConfig, replace as replace_runtime_config


SETTINGS_PATH = CONFIG_DIR / "settings.yaml"
BACKUP_SUFFIX = ".bak"
TEMP_SUFFIX = ".tmp"


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


def put_config(yaml_text: str) -> dict:
    """Validate + atomically write settings.yaml. Returns changes summary."""
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        line_no = (line.line + 1) if line else None
        col_no = (line.column + 1) if line else None
        raise ValueError(f"yaml_parse_error: line={line_no} col={col_no} msg={e}")

    old = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    changes = _diff(old or {}, parsed or {})

    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    backup_path = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.name}{BACKUP_SUFFIX}")
    tmp_path = SETTINGS_PATH.with_name(f"{SETTINGS_PATH.name}{TEMP_SUFFIX}")

    if SETTINGS_PATH.exists():
        shutil.copy2(SETTINGS_PATH, backup_path)

    tmp_path.write_text(yaml_text, encoding="utf-8")
    tmp_path.replace(SETTINGS_PATH)

    return {
        "ok": True,
        "changes": changes,
        "path": str(SETTINGS_PATH),
        "backup_path": str(backup_path) if backup_path.exists() else None,
    }


def patch_runtime_config(runtime_patch: dict[str, Any]) -> dict:
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

    merged_runtime = RuntimeConfig.from_yaml(current).to_dict()
    merged_runtime.update(runtime_patch)
    validated_runtime = RuntimeConfig.from_dict(merged_runtime)

    current["runtime"] = validated_runtime.to_dict()
    ctrader_cfg = current.get("ctrader")
    if not isinstance(ctrader_cfg, dict):
        ctrader_cfg = {}
        current["ctrader"] = ctrader_cfg
    if "ctrader_send_orders" in runtime_patch:
        ctrader_cfg["send_orders"] = bool(validated_runtime.ctrader_send_orders)

    yaml_text = yaml.safe_dump(current, sort_keys=False, allow_unicode=True)
    result = put_config(yaml_text)
    replace_runtime_config(validated_runtime)
    result["runtime"] = validated_runtime.to_dict()
    result["updated_keys"] = sorted(runtime_patch.keys())
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
