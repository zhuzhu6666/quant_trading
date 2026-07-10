"""Shared RuntimeConfig startup restore path.

The backend API process and the learning worker must start from the same
runtime configuration: YAML base first, then the autonomous DB overlay, then a
startup snapshot for audit/readiness.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.config_service import get_config
from backend.services.evolution_ledger import persist_runtime_config_snapshot
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config.runtime_config import RuntimeConfig
from config import runtime_config


def load_yaml_runtime_config() -> tuple[RuntimeConfig, dict[str, Any]]:
    payload = get_config()
    if payload.get("parse_error"):
        raise ValueError(f"settings_parse_error: {payload.get('parse_error')}")
    parsed = payload.get("parsed") or {}
    return RuntimeConfig.from_yaml(parsed), parsed


def restore_runtime_config_on_startup(
    base_cfg: RuntimeConfig,
    *,
    snapshot_source: str,
    db_path: str | Path = STATE_DB,
    run_id: str = "",
) -> dict[str, Any]:
    runtime_config.register_overlay_base(base_cfg, db_path, replace_existing=True)
    overlay_restore = RuntimeConfigOverlayService(db_path).restore_on_startup(base_cfg)
    cfg = overlay_restore["config"] if overlay_restore.get("restored") else base_cfg
    version = runtime_config.replace(cfg)
    snapshot = persist_runtime_config_snapshot(
        cfg,
        source=snapshot_source,
        db_path=db_path,
        run_id=run_id,
    )
    return {
        "ok": True,
        "config": cfg,
        "version": version,
        "snapshot": snapshot,
        "overlay": overlay_restore,
    }
