"""Persistent autonomous runtime overlay.

The YAML settings remain the human/base configuration.  Autonomous governance
mutations are stored as a narrow DB overlay so they survive process restarts
without rewriting settings.yaml.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.core.state_store import validate_runtime_state_schema
from backend.services.evolution_ledger import (
    ensure_evolution_ledger_tables,
    persist_runtime_config_snapshot,
)
from config.runtime_config import RuntimeConfig
from config import runtime_config

OVERLAY_ID = "autonomous_factor_governance"
LEGACY_AUTHORITY_SCHEMA = "runtime_overlay_legacy_authority.v1"
_LOG = logging.getLogger(__name__)


class RuntimeConfigOverlayAuthorityError(RuntimeError):
    """The persisted overlay is not backed by an accepted governance fact."""

    def __init__(
        self,
        report: dict[str, Any],
        *,
        quarantined_config: RuntimeConfig | None = None,
    ):
        self.report = dict(report)
        self.quarantined_config = quarantined_config
        reason = str(report.get("reason") or "runtime_overlay_authority_invalid")
        super().__init__(f"runtime_config_overlay_authority_invalid:{reason}")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _governance_config_hash(value: Any) -> str:
    payload = json.dumps(
        runtime_config.canonical_runtime_config_payload(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _loads_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    try:
        parsed = json.loads(str(value or "{}"))
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


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
            or key == "demo_model_influence_enabled"
            or key == "model_influence_config"
            or key == "context_policy_enabled"
            or key == "runtime_incident_mode"
            or key == "autonomy_mode"
            or key == "autonomy_expansion_frozen"
            or key == "governance_expansion_paused"
            or key == "live_autonomy_unlocked"
            or key == "live_autonomy_unlock_id"
            or key == "position_supervisor_template_id"
            or key == "risk_cvar_threshold_pct"
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
        production_state = is_state_db_path(self.db_path)
        conn = _connect(self.db_path, read_only=production_state)
        try:
            declaration = _p(self.db_path, """
                CREATE TABLE IF NOT EXISTS runtime_config_overlay (
                    overlay_id TEXT PRIMARY KEY,
                    overlay_json TEXT NOT NULL DEFAULT '{}',
                    overlay_hash TEXT DEFAULT '',
                    source TEXT DEFAULT '',
                    run_id TEXT DEFAULT '',
                    mutation_id TEXT NOT NULL DEFAULT '',
                    legacy_authority_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL DEFAULT 0.0
                )
                """)
            if production_state:
                validate_runtime_state_schema(conn, declaration)
            else:
                conn.execute(declaration)
                columns = state_table_columns(conn, "runtime_config_overlay")
                for name, ddl in {
                    "mutation_id": "TEXT NOT NULL DEFAULT ''",
                    "legacy_authority_json": "TEXT NOT NULL DEFAULT '{}'",
                }.items():
                    if name not in columns:
                        conn.execute(
                            f'ALTER TABLE "runtime_config_overlay" ADD COLUMN "{name}" {ddl}'
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
                SELECT overlay_id, overlay_json, overlay_hash, source, run_id,
                       mutation_id, legacy_authority_json, updated_at
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
                    "mutation_id": "",
                    "legacy_authority": {},
                }
            try:
                overlay = json.loads(row["overlay_json"] or "{}")
            except Exception:
                overlay = {}
                overlay_parse_error = True
            else:
                overlay_parse_error = not isinstance(overlay, dict)
            return {
                "ok": True,
                "status": "available",
                "overlay": overlay if isinstance(overlay, dict) else {},
                "overlay_parse_error": overlay_parse_error,
                "overlay_hash": str(row["overlay_hash"] or ""),
                "updated_at": float(row["updated_at"] or 0.0),
                "source": str(row["source"] or ""),
                "run_id": str(row["run_id"] or ""),
                "mutation_id": str(row["mutation_id"] or ""),
                "legacy_authority": _loads_object(row["legacy_authority_json"]),
            }
        finally:
            conn.close()

    @staticmethod
    def _coordinator_mode() -> str:
        from backend.core.static_feature_flags import shared_static_feature_flags

        mode = str(
            shared_static_feature_flags().governance_mutation_coordinator_v2_mode
            or "off"
        ).strip().lower()
        if mode not in {"off", "dual_record", "enforce"}:
            raise RuntimeConfigOverlayAuthorityError(
                {"reason": f"invalid_governance_coordinator_mode:{mode}"}
            )
        return mode

    def _authority_report(
        self,
        *,
        latest: dict[str, Any],
        overlay: dict[str, Any],
        effective_config: RuntimeConfig,
    ) -> dict[str, Any]:
        mode = self._coordinator_mode()
        stored_overlay_hash = str(latest.get("overlay_hash") or "")
        mutation_id = str(latest.get("mutation_id") or "")
        # The coordinator's canonical JSON uses compact separators so the
        # overlay/config/domain bindings share one hash contract.  Legacy
        # overlay rows retain their historical stable-hash encoding.
        actual_overlay_hash = (
            _governance_config_hash(overlay) if mutation_id else _hash(overlay)
        )
        common = {
            "schema_version": "runtime_config_overlay_authority.v1",
            "coordinator_mode": mode,
            "overlay_hash": actual_overlay_hash,
            "stored_overlay_hash": stored_overlay_hash,
            "mutation_id": mutation_id,
            "overlay_keys": sorted(str(key) for key in overlay),
        }
        if not stored_overlay_hash or stored_overlay_hash != actual_overlay_hash:
            return {**common, "ok": False, "reason": "overlay_hash_mismatch"}

        if mutation_id:
            conn = _connect(self.db_path, read_only=True)
            try:
                if not state_table_exists(conn, "governance_mutation_intent"):
                    return {
                        **common,
                        "ok": False,
                        "authority": "committed_mutation",
                        "reason": "governance_intent_table_missing",
                    }
                required = {
                    "mutation_id",
                    "status",
                    "projection_status",
                    "target_config_hash",
                    "committed_config_hash",
                    "domain_hash",
                }
                if not required <= state_table_columns(conn, "governance_mutation_intent"):
                    return {
                        **common,
                        "ok": False,
                        "authority": "committed_mutation",
                        "reason": "governance_intent_contract_incomplete",
                    }
                row = conn.execute(
                    _p(
                        self.db_path,
                        """
                        SELECT mutation_id, status, projection_status,
                               target_config_hash, committed_config_hash,
                               domain_hash
                        FROM governance_mutation_intent
                        WHERE mutation_id=?
                        LIMIT 1
                        """,
                    ),
                    (mutation_id,),
                ).fetchone()
            finally:
                conn.close()
            item = dict(row) if row is not None else {}
            config_hash = _governance_config_hash(effective_config.to_dict())
            checks = {
                "intent_found": bool(item),
                "committed": str(item.get("status") or "") == "committed",
                "projection_current": (
                    str(item.get("projection_status") or "") == "current"
                ),
                "target_hash_bound": (
                    str(item.get("target_config_hash") or "") == config_hash
                ),
                "committed_hash_bound": (
                    str(item.get("committed_config_hash") or "") == config_hash
                ),
                "domain_hash_bound": bool(str(item.get("domain_hash") or "")),
            }
            ok = all(checks.values())
            return {
                **common,
                "ok": ok,
                "authority": "committed_mutation",
                "config_hash": config_hash,
                "checks": checks,
                "reason": "committed_mutation_verified" if ok else "committed_mutation_unverified",
            }

        manifest = dict(latest.get("legacy_authority") or {})
        controls = manifest.get("controls") if isinstance(manifest.get("controls"), dict) else {}
        overlay_keys = set(str(key) for key in overlay)
        control_keys = set(str(key) for key in controls)
        try:
            reviewed_at = float(manifest.get("reviewed_at") or 0.0)
        except (TypeError, ValueError):
            reviewed_at = 0.0
        global_valid = bool(
            manifest.get("schema_version") == LEGACY_AUTHORITY_SCHEMA
            and str(manifest.get("overlay_hash") or "") == actual_overlay_hash
            and str(manifest.get("review_id") or "")
            and str(manifest.get("reviewer") or "").startswith("operator:")
            and reviewed_at > 0.0
            and control_keys == overlay_keys
        )
        invalid_controls = sorted(
            key
            for key in overlay_keys
            if not isinstance(controls.get(key), dict)
            or str(controls[key].get("governance_authority") or "")
            != "legacy_quarantined"
            or str(controls[key].get("risk_class") or "")
            != "risk_tightening"
        )
        ok = global_valid and not invalid_controls
        return {
            **common,
            "ok": ok,
            "authority": "legacy_quarantined",
            "review_id": str(manifest.get("review_id") or ""),
            "reviewer": str(manifest.get("reviewer") or ""),
            "reviewed_control_keys": sorted(control_keys),
            "invalid_control_keys": invalid_controls,
            "reason": "legacy_quarantine_verified" if ok else "legacy_quarantine_unverified",
        }

    @staticmethod
    def _quarantined_projection(
        *,
        base_cfg: RuntimeConfig,
        overlay: dict[str, Any],
        mutation_id: str,
    ) -> tuple[RuntimeConfig, dict[str, Any]]:
        """Preserve protection without granting an unverified risk expansion.

        A blank legacy row represents behavior that may already be protecting
        live positions, so it remains a read-only projection while a durable
        no-new-risk latch is active.  A dangling non-empty mutation is more
        suspicious: only top-level changes derived as tightening survive.
        In both cases operator control fields are combined monotonically so a
        legacy row cannot thaw incident/governance state during recovery.
        """

        from backend.services.governance_mutation_coordinator import (
            classify_governance_risk,
        )

        base = base_cfg.to_dict()
        retained: dict[str, Any] = {}
        excluded: list[str] = []
        classifications: dict[str, Any] = {}
        if not mutation_id:
            retained = deepcopy(overlay)
        else:
            full_target = RuntimeConfig.from_dict(_deep_merge(base, overlay)).to_dict()
            for key, value in overlay.items():
                result = classify_governance_risk(
                    {key: base.get(key)},
                    {key: full_target.get(key)},
                )
                classifications[str(key)] = result.to_dict()
                if result.risk_class == "risk_tightening":
                    retained[str(key)] = deepcopy(value)
                else:
                    excluded.append(str(key))

        projected = RuntimeConfig.from_dict(_deep_merge(base, retained))
        payload = projected.to_dict()
        base_mode = str(base.get("runtime_incident_mode") or "normal")
        projected_mode = str(payload.get("runtime_incident_mode") or "normal")
        incident_rank = {
            "normal": 0,
            "shadow_only": 1,
            "no_new_risk": 2,
            "only_close": 3,
            "frozen": 4,
        }
        payload["runtime_incident_mode"] = max(
            (base_mode, projected_mode),
            key=lambda value: incident_rank.get(value, incident_rank["frozen"]),
        )
        payload["governance_expansion_paused"] = bool(
            base.get("governance_expansion_paused", False)
            or payload.get("governance_expansion_paused", False)
        )
        payload["autonomy_expansion_frozen"] = bool(
            base.get("autonomy_expansion_frozen", False)
            or payload.get("autonomy_expansion_frozen", False)
        )
        payload["live_autonomy_unlocked"] = bool(
            base.get("live_autonomy_unlocked", False)
            and payload.get("live_autonomy_unlocked", False)
        )
        return RuntimeConfig.from_dict(payload), {
            "quarantine_projection": (
                "legacy_behavior_preserved"
                if not mutation_id
                else "dangling_mutation_tightening_only"
            ),
            "retained_keys": sorted(retained),
            "excluded_keys": sorted(excluded),
            "classifications": classifications,
            "new_risk_authorized": False,
        }

    def restore_on_startup(self, base_cfg: RuntimeConfig) -> dict[str, Any]:
        latest = self.latest()
        overlay = dict(latest.get("overlay") or {})
        if latest.get("overlay_parse_error"):
            raise RuntimeConfigOverlayAuthorityError(
                {
                    "schema_version": "runtime_config_overlay_authority.v1",
                    "ok": False,
                    "reason": "overlay_json_invalid",
                    "mutation_id": str(latest.get("mutation_id") or ""),
                    "new_risk_authorized": False,
                },
                quarantined_config=base_cfg,
            )
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
        authority = self._authority_report(
            latest=latest,
            overlay=overlay,
            effective_config=restored,
        )
        if not authority.get("ok"):
            quarantined_config, quarantine = self._quarantined_projection(
                base_cfg=base_cfg,
                overlay=overlay,
                mutation_id=str(latest.get("mutation_id") or ""),
            )
            raise RuntimeConfigOverlayAuthorityError(
                {**authority, **quarantine},
                quarantined_config=quarantined_config,
            )
        return {
            "ok": True,
            "restored": True,
            "config": restored,
            "overlay_hash": latest.get("overlay_hash", ""),
            "updated_at": latest.get("updated_at", 0.0),
            "source": latest.get("source", ""),
            "run_id": latest.get("run_id", ""),
            "mutation_id": latest.get("mutation_id", ""),
            "authority": authority,
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
        expected_overlay_hash: str = "",
    ) -> dict[str, Any]:
        runtime_config.register_overlay_base(base_cfg, self.db_path, replace_existing=True)
        result = self._mutate_overlay(
            {},
            source=source,
            run_id=run_id,
            replace_overlay=True,
            expected_overlay_hash=expected_overlay_hash,
        )
        result["status"] = "cleared"
        return result

    def review_legacy_quarantine(
        self,
        base_cfg: RuntimeConfig,
        *,
        expected_overlay_hash: str,
        reviewed_keys: list[str] | tuple[str, ...],
        reviewer: str,
        review_id: str,
    ) -> dict[str, Any]:
        """Add a hash-bound, per-control legacy quarantine review.

        This is an operator/backfill boundary, not a live governance mutation.
        Risk direction is derived from before/after facts by the coordinator's
        classifier; the caller cannot self-report a tightening exemption.  A
        partial review is durable and auditable but startup remains blocked
        until every key in the exact overlay hash has passed review.
        """

        reviewer = str(reviewer or "").strip()
        review_id = str(review_id or "").strip()
        if not reviewer.startswith("operator:") or not review_id:
            return {
                "ok": False,
                "status": "operator_review_identity_required",
            }
        latest = self.latest()
        overlay = dict(latest.get("overlay") or {})
        overlay_hash = str(latest.get("overlay_hash") or "")
        if str(latest.get("mutation_id") or ""):
            return {
                "ok": False,
                "status": "overlay_already_mutation_bound",
                "mutation_id": str(latest.get("mutation_id") or ""),
            }
        if (
            not overlay
            or overlay_hash != str(expected_overlay_hash or "")
            or overlay_hash != _hash(overlay)
        ):
            return {
                "ok": False,
                "status": "overlay_hash_changed",
                "overlay_hash": overlay_hash,
            }

        from backend.services.governance_mutation_coordinator import (
            classify_governance_risk,
        )

        effective = RuntimeConfig.from_dict(
            _deep_merge(base_cfg.to_dict(), overlay)
        ).to_dict()
        base = base_cfg.to_dict()
        requested = sorted({str(key) for key in reviewed_keys if str(key)})
        missing = sorted(set(requested) - set(overlay))
        if missing or not requested:
            return {
                "ok": False,
                "status": "review_control_key_invalid",
                "missing_keys": missing,
            }

        reviews: dict[str, Any] = {}
        blocked: dict[str, Any] = {}
        for key in requested:
            classification = classify_governance_risk(
                {key: base.get(key)},
                {key: effective.get(key)},
            )
            payload = classification.to_dict()
            if classification.risk_class != "risk_tightening":
                blocked[key] = payload
                continue
            reviews[key] = {
                "governance_authority": "legacy_quarantined",
                "risk_class": "risk_tightening",
                "classification": payload,
            }
        if blocked:
            return {
                "ok": False,
                "status": "legacy_control_not_tightening",
                "blocked": blocked,
                "reviewable": sorted(reviews),
            }

        existing = dict(latest.get("legacy_authority") or {})
        existing_controls = (
            dict(existing.get("controls") or {})
            if str(existing.get("overlay_hash") or "") == overlay_hash
            else {}
        )
        controls = {**existing_controls, **reviews}
        now = time.time()
        manifest = {
            "schema_version": LEGACY_AUTHORITY_SCHEMA,
            "overlay_hash": overlay_hash,
            "review_id": review_id,
            "reviewer": reviewer,
            "reviewed_at": now,
            "controls": controls,
        }
        conn = _connect(self.db_path)
        try:
            self._begin_serialized_write(conn)
            row = conn.execute(
                _p(
                    self.db_path,
                    """
                    SELECT overlay_hash, mutation_id
                    FROM runtime_config_overlay
                    WHERE overlay_id=?
                    """,
                ),
                (OVERLAY_ID,),
            ).fetchone()
            current = dict(row) if row is not None else {}
            if (
                str(current.get("overlay_hash") or "") != overlay_hash
                or str(current.get("mutation_id") or "")
            ):
                conn.rollback()
                return {
                    "ok": False,
                    "status": "overlay_changed_during_review",
                }
            update = conn.execute(
                _p(
                    self.db_path,
                    """
                    UPDATE runtime_config_overlay
                    SET legacy_authority_json=?
                    WHERE overlay_id=? AND overlay_hash=? AND mutation_id=''
                    """,
                ),
                (_dumps(manifest), OVERLAY_ID, overlay_hash),
            )
            if int(update.rowcount or 0) != 1:
                conn.rollback()
                return {
                    "ok": False,
                    "status": "overlay_changed_during_review",
                }
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        remaining = sorted(set(overlay) - set(controls))
        return {
            "ok": True,
            "status": (
                "legacy_quarantine_complete"
                if not remaining
                else "legacy_quarantine_partial"
            ),
            "overlay_hash": overlay_hash,
            "reviewed_keys": sorted(reviews),
            "remaining_keys": remaining,
            "manifest": manifest,
        }

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
            (overlay_id, overlay_json, overlay_hash, source, run_id,
             mutation_id, legacy_authority_json, updated_at)
            VALUES (?, ?, ?, ?, ?, '', '{}', ?)
            ON CONFLICT(overlay_id) DO UPDATE SET
                overlay_json=excluded.overlay_json,
                overlay_hash=excluded.overlay_hash,
                source=excluded.source,
                run_id=excluded.run_id,
                mutation_id='',
                legacy_authority_json='{}',
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
        expected_overlay_hash: str = "",
    ) -> dict[str, Any]:
        _refuse_test_write_to_state(self.db_path, source=source, run_id=run_id)
        self.ensure_table()
        ensure_evolution_ledger_tables(self.db_path)
        conn = _connect(self.db_path)
        try:
            self._begin_serialized_write(conn)
            current = self._read_overlay_in_transaction(conn)
            current_hash = _hash(current)
            if expected_overlay_hash and current_hash != str(expected_overlay_hash):
                raise RuntimeConfigOverlayAuthorityError(
                    {
                        "reason": "overlay_hash_changed",
                        "expected_overlay_hash": str(expected_overlay_hash),
                        "current_overlay_hash": current_hash,
                    }
                )
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

        # A direct/legacy writer is never allowed to inherit a prior review
        # manifest.  In production, latch before publishing so an off-mode
        # mutation cannot create new risk while waiting for explicit review or
        # coordinator adoption.
        latch: dict[str, Any] = {}
        latch_error = ""
        if is_state_db_path(self.db_path) and overlay:
            try:
                from backend.services.live_safety_state import (
                    activate_no_new_risk_latch,
                )

                latch = activate_no_new_risk_latch(
                    reason="runtime_overlay_direct_mutation_unverified",
                    actor="system:runtime_config_overlay",
                    metadata={
                        "overlay_hash": overlay_hash,
                        "source": str(source or ""),
                        "run_id": str(run_id or ""),
                        "updated_keys": sorted(sanitized),
                    },
                    cause="governance_authority",
                    cause_id="runtime_config_overlay_direct_mutation",
                )
            except Exception as exc:
                # The safety-state implementation already installed the
                # process-local fail-closed latch before raising.
                latch_error = f"{type(exc).__name__}:{exc}"
                _LOG.error(
                    "runtime overlay authority latch persistence failed: %s",
                    latch_error,
                )

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
            "authority_status": (
                "legacy_unverified_no_new_risk"
                if is_state_db_path(self.db_path) and overlay
                else "isolated_legacy_compatibility"
            ),
            "no_new_risk_latch": latch,
            "latch_error": latch_error,
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
            "mutation_id": latest.get("mutation_id", ""),
            "legacy_authority": latest.get("legacy_authority", {}),
            "keys": sorted(overlay.keys()),
            **suspicion,
        }
