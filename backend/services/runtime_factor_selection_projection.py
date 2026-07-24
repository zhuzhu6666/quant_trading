"""Cross-process read model for the exact live factor selection."""
from __future__ import annotations

import json
import hashlib
import os
import sqlite3
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import validate_runtime_state_schema


PROJECTION_KEY = "runtime_factor_selection.v1"
_PROCESS_BOOT_ID = f"live-factor-selection:{os.getpid()}:{uuid.uuid4().hex}"


class RuntimeFactorSelectionProjectionService:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _conn(self, *, read_only: bool = False):
        if self._use_pg():
            return get_state_pg_conn(read_only=read_only)
        conn = connect_sqlite(self.db_path, read_only=read_only)
        conn.row_factory = sqlite3.Row
        return conn

    def publish(
        self,
        selection: Any,
        *,
        source: str = "live_factor_pipeline",
        live_generation_id: str = "",
        pipeline_warm: bool | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        from config import runtime_config

        cfg = runtime_config.shared_holder().get()
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        selected = list(getattr(selection, "selected_factor_ids", []) or [])
        selected_roles = {
            name: str((signal_cfg.get(name) or {}).get("role") or "alpha").lower()
            for name in selected
        }
        role_counts = Counter(selected_roles.values())
        exclusion_reason_counts = Counter(
            str(reason or "unknown")
            for reason in dict(
                getattr(selection, "reason_excluded", {}) or {}
            ).values()
        )
        config_payload = (
            cfg.to_dict()
            if hasattr(cfg, "to_dict")
            else dict(getattr(cfg, "__dict__", {}) or {})
        )
        config_hash = hashlib.sha256(
            json.dumps(
                config_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()
        selection_fingerprint = hashlib.sha256(
            json.dumps(
                {
                    "selected": selected,
                    "excluded": list(
                        getattr(selection, "excluded_factor_ids", []) or []
                    ),
                    "reasons": dict(
                        getattr(selection, "reason_excluded", {}) or {}
                    ),
                    "weights": {
                        name: float(weights.get(name, 0.0) or 0.0)
                        for name in selected
                    },
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        payload = {
            "schema_version": "runtime_factor_selection.v1",
            "source": source,
            "selected_factor_ids": selected,
            "excluded_factor_ids": list(getattr(selection, "excluded_factor_ids", []) or []),
            "reason_excluded": dict(getattr(selection, "reason_excluded", {}) or {}),
            "governance_profile": (
                "balanced_demo"
                if runtime_config.bounded_demo_mode_active(cfg)
                else "strict_live"
            ),
            "alpha_voter_count": sum(
                1
                for name, role in selected_roles.items()
                if role == "alpha" and float(weights.get(name, 0.0) or 0.0) > 0.0
            ),
            "context_count": int(role_counts.get("context", 0)),
            "gate_count": int(role_counts.get("gate", 0)),
            "exclusion_reason_counts": dict(exclusion_reason_counts),
            "soft_downweighted_factors": sorted(
                name
                for name, entry in signal_cfg.items()
                if "downweight" in str((entry or {}).get("reason") or "").lower()
            ),
            "hard_quarantined_factors": sorted(
                name
                for name, entry in signal_cfg.items()
                if str((entry or {}).get("lifecycle_status") or "").upper()
                in {"QUARANTINE", "QUARANTINED"}
            ),
            "shadow_canary_factors": sorted(
                name
                for name, entry in signal_cfg.items()
                if bool((entry or {}).get("autonomous_activation"))
                and str((entry or {}).get("lifecycle_status") or "").upper()
                in {"SHADOW", "PROMOTION_PREPARED", "ACTIVE"}
                and 0.0 < float(weights.get(name, 0.0) or 0.0) <= 0.05
            ),
            "signal_thresholds": {
                "base": float(getattr(cfg, "factor_signal_threshold", 0.30) or 0.30),
                "demo_cap": 0.55,
                "evidence_version": "entry_quality_governance_evidence.v2",
            },
            "process_boot_id": _PROCESS_BOOT_ID,
            "live_generation_id": str(live_generation_id or ""),
            "config_version": int(runtime_config.shared_holder().version()),
            "config_hash": config_hash,
            "selection_fingerprint": selection_fingerprint,
            "pipeline_warm": pipeline_warm,
            "heartbeat_at": now,
            "published_at": now,
        }
        conn = self._conn()
        try:
            declaration = self._sql("""
                CREATE TABLE IF NOT EXISTS runtime_kv (
                    key TEXT PRIMARY KEY, value_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
            """)
            if self._use_pg():
                validate_runtime_state_schema(conn, declaration)
            else:
                conn.execute(declaration)
            streak_row = conn.execute(
                self._sql("SELECT value_json FROM runtime_kv WHERE key=?"),
                ("factor_governance_evidence_streak.v1",),
            ).fetchone()
            if streak_row:
                raw_streak = streak_row["value_json"]
                payload["evidence_maturity"] = (
                    dict(raw_streak)
                    if isinstance(raw_streak, dict)
                    else json.loads(str(raw_streak or "{}"))
                )
            else:
                payload["evidence_maturity"] = {
                    "schema_version": "factor_governance_evidence_streak.v1",
                    "factors": {},
                }
            try:
                policy_row = conn.execute(
                    self._sql(
                        """SELECT evidence_json
                           FROM policy_suggestion
                           WHERE scope_type='entry_quality'
                             AND scope_key='weak_signal'
                             AND action='raise_weak_signal_threshold'
                             AND status='applied'
                           ORDER BY created_at DESC LIMIT 1"""
                    )
                ).fetchone()
                policy_evidence = (
                    dict(policy_row["evidence_json"])
                    if policy_row
                    and isinstance(policy_row["evidence_json"], dict)
                    else json.loads(str(policy_row["evidence_json"] or "{}"))
                    if policy_row
                    else {}
                )
                controls = dict(
                    policy_evidence.get("recommended_controls") or {}
                )
                payload["signal_thresholds"].update(
                    {
                        "learned": float(
                            controls.get("min_abs_signal_score") or 0.0
                        ),
                        "strong_signal_override": float(
                            controls.get("strong_signal_override") or 0.0
                        ),
                        "effective_sample_count": float(
                            policy_evidence.get("effective_sample_count") or 0.0
                        ),
                    }
                )
            except Exception:
                payload["signal_thresholds"].update(
                    {
                        "learned": 0.0,
                        "strong_signal_override": 0.0,
                        "effective_sample_count": 0.0,
                    }
                )
            conn.execute(self._sql("""
                INSERT INTO runtime_kv (key, value_json, updated_at) VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json,
                    updated_at=excluded.updated_at
            """), (PROJECTION_KEY, json.dumps(payload, ensure_ascii=False), now))
            conn.commit()
            return {**payload, "ok": True}
        finally:
            conn.close()

    def latest(self, *, max_age_seconds: float = 900.0) -> dict[str, Any]:
        conn = None
        try:
            conn = self._conn(read_only=True)
            row = conn.execute(self._sql(
                "SELECT value_json, updated_at FROM runtime_kv WHERE key=?"
            ), (PROJECTION_KEY,)).fetchone()
            if not row:
                return {"ok": False, "status": "missing"}
            raw = row["value_json"]
            payload = dict(raw) if isinstance(raw, dict) else json.loads(str(raw or "{}"))
            updated_at = float(row["updated_at"] or payload.get("published_at") or 0.0)
            age = max(0.0, time.time() - updated_at)
            fresh = age <= max(1.0, float(max_age_seconds))
            return {**payload, "ok": fresh, "status": "fresh" if fresh else "stale", "age_seconds": age}
        except Exception as exc:
            return {"ok": False, "status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}
        finally:
            if conn is not None:
                conn.close()
