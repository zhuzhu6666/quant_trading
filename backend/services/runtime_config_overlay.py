"""Persistent autonomous runtime overlay.

The YAML settings remain the human/base configuration.  Autonomous governance
mutations are stored as a narrow DB overlay so they survive process restarts
without rewriting settings.yaml.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.evolution_ledger import (
    ensure_evolution_ledger_tables,
    persist_runtime_config_snapshot,
)
from config.runtime_config import RuntimeConfig
from config import runtime_config

OVERLAY_ID = "autonomous_factor_governance"


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    if is_state_db_path(db_path):
        return get_state_pg_conn(read_only=read_only)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


def _looks_like_test_run(value: str) -> bool:
    text = str(value or "").strip().lower()
    return text.startswith(("test", "pytest")) or text in {"unit", "unit-test", "unit_test"}


def _running_under_pytest() -> bool:
    return bool(os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"))


def _refuse_test_write_to_state(db_path: str | Path, *, source: str, run_id: str) -> None:
    if not is_state_db_path(db_path):
        return
    if os.getenv("QUANT_ALLOW_PYTEST_STATE_OVERLAY_WRITE", "").strip() == "1":
        return
    if _running_under_pytest() or _looks_like_test_run(run_id) or _looks_like_test_run(source):
        raise RuntimeError(
            "refusing to write test runtime_config_overlay into production state store"
        )


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def _apply_runtime_overlay(base: dict[str, Any], patch: dict[str, Any], *, replace_keys: bool = False) -> dict[str, Any]:
    if not replace_keys:
        return _deep_merge(base, patch)
    result = deepcopy(base)
    for key, value in dict(patch or {}).items():
        if key == "extra" and isinstance(value, dict):
            extra = dict(result.get("extra") or {})
            if "active_parameter_templates" in value:
                extra["active_parameter_templates"] = deepcopy(value["active_parameter_templates"])
            result["extra"] = extra
        else:
            result[key] = deepcopy(value)
    return result


def _sanitize_patch(patch: dict[str, Any]) -> dict[str, Any]:
    allowed: dict[str, Any] = {}
    for key, value in dict(patch or {}).items():
        if key in {"factor_signal_config", "factor_portfolio_weights"}:
            if isinstance(value, dict):
                allowed[key] = deepcopy(value)
            continue
        if key == "extra" and isinstance(value, dict):
            active = value.get("active_parameter_templates")
            if isinstance(active, dict):
                allowed["extra"] = {"active_parameter_templates": deepcopy(active)}
            continue
        if (
            str(key).startswith("factor_governance_")
            or str(key).startswith("factor_redundancy_")
            or key == "context_policy_enabled"
            or key == "runtime_incident_mode"
            or key == "autonomy_mode"
            or key == "live_autonomy_unlocked"
            or key == "live_autonomy_unlock_id"
            or key == "position_supervisor_template_id"
            or key == "kelly_risk_per_trade_pct"
            or key == "kelly_fraction"
            or key == "kelly_max_pct"
            or key == "kelly_min_closed_trades"
            or key == "kelly_canary_max_api_volume"
            or key == "dynamic_sizing_enabled"
            or key == "dynamic_sizing_max_api_volume"
            or key == "dynamic_sizing_api_units_per_display_unit"
        ):
            allowed[key] = deepcopy(value)
    return allowed


def _overlay_suspicion_report(overlay: dict[str, Any], *, source: str = "", run_id: str = "") -> dict[str, Any]:
    reasons: list[str] = []
    suspicious_factors: list[str] = []
    if _looks_like_test_run(source):
        reasons.append("test_like_source")
    if _looks_like_test_run(run_id):
        reasons.append("test_like_run_id")
    factor_names: set[str] = set()
    for section in ("factor_signal_config", "factor_portfolio_weights"):
        value = overlay.get(section)
        if isinstance(value, dict):
            factor_names.update(str(name) for name in value)
    for name in sorted(factor_names):
        lower = name.lower()
        if (
            lower in {"foo", "bar", "model_weak_factor", "weak_shadow"}
            or lower.startswith(("test_", "shadow_alpha_"))
        ):
            suspicious_factors.append(name)
    if suspicious_factors:
        reasons.append("test_like_factor_ids")
    return {
        "suspicious": bool(reasons),
        "reasons": sorted(set(reasons)),
        "suspicious_factors": suspicious_factors[:50],
    }


class RuntimeConfigOverlayService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def ensure_table(self) -> None:
        conn = _connect(self.db_path)
        try:
            conn.execute(
                _p(self.db_path, """
                CREATE TABLE IF NOT EXISTS runtime_config_overlay (
                    overlay_id TEXT PRIMARY KEY,
                    overlay_json TEXT NOT NULL DEFAULT '{}',
                    overlay_hash TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    run_id TEXT DEFAULT '',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
                """)
            )
            conn.commit()
        finally:
            conn.close()

    def latest(self) -> dict[str, Any]:
        self.ensure_table()
        conn = _connect(self.db_path, read_only=True)
        try:
            row = conn.execute(
                _p(self.db_path, """
                SELECT overlay_id, overlay_json, overlay_hash, source, run_id, updated_at
                FROM runtime_config_overlay
                WHERE overlay_id=?
                """),
                (OVERLAY_ID,),
            ).fetchone()
            if not row:
                return {
                    "ok": False,
                    "status": "missing",
                    "overlay": {},
                    "overlay_hash": "",
                    "updated_at": 0.0,
                    "source": "",
                    "run_id": "",
                }
            try:
                overlay = json.loads(row["overlay_json"] or "{}")
            except Exception:
                overlay = {}
            return {
                "ok": True,
                "status": "available",
                "overlay": overlay if isinstance(overlay, dict) else {},
                "overlay_hash": str(row["overlay_hash"] or ""),
                "updated_at": float(row["updated_at"] or 0.0),
                "source": str(row["source"] or ""),
                "run_id": str(row["run_id"] or ""),
            }
        finally:
            conn.close()

    def restore_on_startup(self, base_cfg: RuntimeConfig) -> dict[str, Any]:
        latest = self.latest()
        overlay = dict(latest.get("overlay") or {})
        suspicion = _overlay_suspicion_report(
            overlay,
            source=str(latest.get("source") or ""),
            run_id=str(latest.get("run_id") or ""),
        )
        if not overlay:
            return {
                "ok": True,
                "restored": False,
                "config": base_cfg,
                "overlay_hash": latest.get("overlay_hash", ""),
                **suspicion,
            }
        if suspicion["suspicious"] and (
            "test_like_factor_ids" in set(suspicion.get("reasons") or [])
            or is_state_db_path(self.db_path)
        ):
            raise RuntimeError(
                "runtime_config_overlay_suspicious: "
                f"reasons={suspicion['reasons']} factors={suspicion['suspicious_factors']}"
            )
        merged = _deep_merge(base_cfg.to_dict(), overlay)
        restored = RuntimeConfig.from_dict(merged)
        return {
            "ok": True,
            "restored": True,
            "config": restored,
            "overlay_hash": latest.get("overlay_hash", ""),
            "updated_at": latest.get("updated_at", 0.0),
            "source": latest.get("source", ""),
            "run_id": latest.get("run_id", ""),
            **suspicion,
        }

    def apply_patch(self, patch: dict[str, Any], *, source: str, run_id: str = "") -> dict[str, Any]:
        sanitized = _sanitize_patch(patch)
        if not sanitized:
            return {"ok": False, "status": "empty_overlay_patch", "updated_keys": []}
        return self._mutate_overlay(
            sanitized,
            source=source,
            run_id=run_id,
            replace_overlay=False,
        )

    def replace_overlay(self, overlay: dict[str, Any], *, source: str, run_id: str = "") -> dict[str, Any]:
        sanitized = _sanitize_patch(overlay)
        return self._mutate_overlay(
            sanitized,
            source=source,
            run_id=run_id,
            replace_overlay=True,
        )

    def clear_overlay_to_base(
        self,
        base_cfg: RuntimeConfig,
        *,
        source: str,
        run_id: str = "",
    ) -> dict[str, Any]:
        runtime_config.register_overlay_base(base_cfg, self.db_path, replace_existing=True)
        result = self._mutate_overlay(
            {},
            source=source,
            run_id=run_id,
            replace_overlay=True,
        )
        result["status"] = "cleared"
        return result

    def _read_overlay_in_transaction(self, conn: Any) -> dict[str, Any]:
        row = conn.execute(
            _p(self.db_path, """
            SELECT overlay_json
            FROM runtime_config_overlay
            WHERE overlay_id=?
            """),
            (OVERLAY_ID,),
        ).fetchone()
        if not row:
            return {}
        raw = row["overlay_json"] if hasattr(row, "keys") else row[0]
        try:
            parsed = json.loads(raw or "{}")
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    def _begin_serialized_write(self, conn: Any) -> None:
        if is_state_db_path(self.db_path):
            conn.execute(
                _p(self.db_path, "SELECT pg_advisory_xact_lock(hashtext(?))"),
                ("quant_runtime_config_overlay",),
            )
        else:
            # SQLite's deferred transactions allow two readers to calculate
            # from the same stale overlay.  Take the writer lock before read.
            conn.execute("BEGIN IMMEDIATE")

    def _persist_overlay_row(
        self,
        conn: Any,
        overlay: dict[str, Any],
        *,
        overlay_hash: str,
        source: str,
        run_id: str,
        updated_at: float,
    ) -> None:
        conn.execute(
            _p(self.db_path, """
            INSERT INTO runtime_config_overlay
            (overlay_id, overlay_json, overlay_hash, source, run_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(overlay_id) DO UPDATE SET
                overlay_json=excluded.overlay_json,
                overlay_hash=excluded.overlay_hash,
                source=excluded.source,
                run_id=excluded.run_id,
                updated_at=excluded.updated_at
            """),
            (
                OVERLAY_ID,
                _dumps(overlay),
                overlay_hash,
                str(source or ""),
                str(run_id or ""),
                updated_at,
            ),
        )

    def _mutate_overlay(
        self,
        sanitized: dict[str, Any],
        *,
        source: str,
        run_id: str = "",
        replace_overlay: bool,
    ) -> dict[str, Any]:
        _refuse_test_write_to_state(self.db_path, source=source, run_id=run_id)
        self.ensure_table()
        ensure_evolution_ledger_tables(self.db_path)
        conn = _connect(self.db_path)
        try:
            self._begin_serialized_write(conn)
            current = self._read_overlay_in_transaction(conn)
            overlay = deepcopy(sanitized) if replace_overlay else _deep_merge(current, sanitized)
            effective_config = runtime_config.config_from_overlay(overlay, self.db_path)
            now = time.time()
            overlay_hash = _hash(overlay)
            self._persist_overlay_row(
                conn,
                overlay,
                overlay_hash=overlay_hash,
                source=source,
                run_id=run_id,
                updated_at=now,
            )
            snapshot = persist_runtime_config_snapshot(
                effective_config,
                source=str(source or "runtime_config_overlay"),
                db_path=self.db_path,
                run_id=str(run_id or ""),
                conn=conn,
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001
                pass
            raise
        finally:
            conn.close()

        # Publish only after both the overlay row and its audit snapshot commit.
        version = runtime_config.replace(effective_config)
        return {
            "ok": True,
            "status": "applied",
            "version": version,
            "updated_keys": sorted(sanitized.keys()),
            "overlay_hash": overlay_hash,
            "updated_at": now,
            "snapshot": snapshot,
        }

    def status(self) -> dict[str, Any]:
        latest = self.latest()
        overlay = dict(latest.get("overlay") or {})
        suspicion = _overlay_suspicion_report(
            overlay,
            source=str(latest.get("source") or ""),
            run_id=str(latest.get("run_id") or ""),
        )
        return {
            "ok": bool(latest.get("ok")),
            "status": "suspicious" if suspicion["suspicious"] else latest.get("status", "missing"),
            "overlay_hash": latest.get("overlay_hash", ""),
            "updated_at": latest.get("updated_at", 0.0),
            "source": latest.get("source", ""),
            "run_id": latest.get("run_id", ""),
            "keys": sorted(overlay.keys()),
            **suspicion,
        }
