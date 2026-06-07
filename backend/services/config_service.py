"""Config service — read/edit config/settings.yaml."""
import yaml
from pathlib import Path
from typing import Any

from backend.core.paths import CONFIG_DIR


SETTINGS_PATH = CONFIG_DIR / "settings.yaml"


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
    """Validate + write settings.yaml. Returns changes summary."""
    try:
        parsed = yaml.safe_load(yaml_text)
    except yaml.YAMLError as e:
        line = getattr(e, "problem_mark", None)
        line_no = (line.line + 1) if line else None
        col_no = (line.column + 1) if line else None
        raise ValueError(f"yaml_parse_error: line={line_no} col={col_no} msg={e}")

    old = yaml.safe_load(SETTINGS_PATH.read_text(encoding="utf-8")) if SETTINGS_PATH.exists() else {}
    changes = _diff(old or {}, parsed or {})

    SETTINGS_PATH.write_text(yaml_text, encoding="utf-8")
    return {"ok": True, "changes": changes, "path": str(SETTINGS_PATH)}


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
