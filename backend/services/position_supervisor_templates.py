from __future__ import annotations

import json
import sqlite3
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.db_helpers import conn_is_pg as _conn_is_pg, pg_sql as _sql


SCHEMA_VERSION = "position_supervisor_template.v1"
DEFAULT_TEMPLATE_ID = "position_supervisor:default.v1"
CONSERVATIVE_TEMPLATE_ID = "position_supervisor:conservative.v1"
PROFIT_PROTECTION_TEMPLATE_ID = "position_supervisor:profit_protection.v1"


_TEMPLATES: dict[str, dict[str, Any]] = {
    DEFAULT_TEMPLATE_ID: {
        "schema_version": SCHEMA_VERSION,
        "template_id": DEFAULT_TEMPLATE_ID,
        "template_version": "default.v1",
        "template_role": "baseline",
        "status": "active",
        "description": "Keep current supervisor behavior unchanged.",
        "thresholds": {
            "min_thesis_break_seconds": 0.0,
            "min_closed_bars_high_vol_or_weak_trend": 1,
            "min_closed_bars_default": 2,
            "hard_risk_bypass": True,
            "min_independent_thesis_break_evidence": 2,
            "broken_holding_efficiency_threshold": 1.00,
            "giveback_reduce_threshold": 0.70,
            "giveback_tighten_threshold": 0.35,
            "profit_capture_min_threshold": 0.35,
            "time_decay_reduce_threshold": 0.35,
            "timeout_tighten_ratio": 0.80,
            "timeout_reduce_ratio": 0.80,
            "weakening_holding_efficiency_threshold": 0.45,
            "near_take_profit_progress": 0.92,
            "near_stop_loss_progress": 0.85,
            "near_stop_loss_efficiency_threshold": 0.25,
        },
        "sl_policy": {
            "breakeven_lock_ratio": 0.25,
            "profit_lock_multiplier": 0.60,
            "min_stop_tighten_points": 0.01,
        },
        "tp_policy": {
            "near_take_profit_action": "close",
            "extension_enabled": False,
            "extension_progress_threshold": 0.80,
            "extension_efficiency_threshold": 0.70,
            "extension_profit_capture_min": 0.65,
            "extension_factor": 0.25,
            "max_extension_factor": 0.50,
        },
        "capture_policy": {
            "mfe_capture_failure_threshold": 0.15,
            "giveback_lock_threshold": 0.35,
        },
        "learning_bounds": {
            "max_threshold_delta": 0.12,
            "max_tp_extension_factor": 0.50,
            "min_profit_lock_multiplier": 0.35,
            "max_profit_lock_multiplier": 0.85,
        },
        "risk_boundary": {
            "approval_path": "built_in_default",
            "can_auto_deploy": True,
            "auto_deploy_modes": ["demo_autonomous"],
            "requires_offline_replay": False,
            # The baseline is the one live supervisor authority in Demo and
            # future production.  RiskPolicy, broker confirmation and fresh
            # reconcile remain mandatory; this boundary only selects the
            # governed execution path.
            "adaptive_execution_mode": "governed_execute",
        },
    },
    CONSERVATIVE_TEMPLATE_ID: {
        "schema_version": SCHEMA_VERSION,
        "template_id": CONSERVATIVE_TEMPLATE_ID,
        "template_version": "conservative.v1",
        "template_role": "reduce_early_small_loss_exits",
        "status": "candidate",
        "description": "Delay early thesis-broken full exits and prefer tighten/reduce evidence first.",
        "thresholds": {
            "min_thesis_break_seconds": 300.0,
            "min_closed_bars_high_vol_or_weak_trend": 1,
            "min_closed_bars_default": 2,
            "hard_risk_bypass": True,
            "min_independent_thesis_break_evidence": 2,
            "broken_holding_efficiency_threshold": 0.12,
            "giveback_reduce_threshold": 0.78,
            "giveback_tighten_threshold": 0.42,
            "profit_capture_min_threshold": 0.28,
            "time_decay_reduce_threshold": 0.28,
            "timeout_tighten_ratio": 0.88,
            "timeout_reduce_ratio": 0.90,
            "weakening_holding_efficiency_threshold": 0.38,
            "near_take_profit_progress": 0.95,
            "near_stop_loss_progress": 0.90,
            "near_stop_loss_efficiency_threshold": 0.18,
        },
        "sl_policy": {
            "breakeven_lock_ratio": 0.18,
            "profit_lock_multiplier": 0.50,
            "min_stop_tighten_points": 0.01,
        },
        "tp_policy": {
            "near_take_profit_action": "protect",
            "extension_enabled": False,
            "extension_progress_threshold": 0.86,
            "extension_efficiency_threshold": 0.78,
            "extension_profit_capture_min": 0.70,
            "extension_factor": 0.15,
            "max_extension_factor": 0.35,
        },
        "capture_policy": {
            "mfe_capture_failure_threshold": 0.18,
            "giveback_lock_threshold": 0.42,
        },
        "learning_bounds": {
            "max_threshold_delta": 0.10,
            "max_tp_extension_factor": 0.35,
            "min_profit_lock_multiplier": 0.30,
            "max_profit_lock_multiplier": 0.75,
        },
        "risk_boundary": {
            "approval_path": "offline_replay_then_governed_release",
            "can_auto_deploy": False,
            "auto_deploy_modes": ["demo_autonomous"],
            "requires_offline_replay": True,
            "adaptive_execution_mode": "governed_execute",
        },
    },
    PROFIT_PROTECTION_TEMPLATE_ID: {
        "schema_version": SCHEMA_VERSION,
        "template_id": PROFIT_PROTECTION_TEMPLATE_ID,
        "template_version": "profit_protection.v1",
        "template_role": "reduce_mfe_giveback_and_capture_profit",
        "status": "candidate",
        "description": "Protect positions that already showed useful MFE before profit capture deteriorates.",
        "thresholds": {
            "min_thesis_break_seconds": 300.0,
            "min_closed_bars_high_vol_or_weak_trend": 1,
            "min_closed_bars_default": 2,
            "hard_risk_bypass": True,
            "min_independent_thesis_break_evidence": 2,
            "broken_holding_efficiency_threshold": 0.18,
            "giveback_reduce_threshold": 0.52,
            "giveback_tighten_threshold": 0.22,
            "profit_capture_min_threshold": 0.42,
            "time_decay_reduce_threshold": 0.32,
            "timeout_tighten_ratio": 0.72,
            "timeout_reduce_ratio": 0.84,
            "weakening_holding_efficiency_threshold": 0.42,
            "near_take_profit_progress": 0.88,
            "near_stop_loss_progress": 0.82,
            "near_stop_loss_efficiency_threshold": 0.22,
        },
        "sl_policy": {
            "breakeven_lock_ratio": 0.35,
            "profit_lock_multiplier": 0.72,
            "min_stop_tighten_points": 0.01,
        },
        "tp_policy": {
            "near_take_profit_action": "protect",
            "extension_enabled": True,
            "extension_progress_threshold": 0.76,
            "extension_efficiency_threshold": 0.62,
            "extension_profit_capture_min": 0.42,
            "extension_factor": 0.20,
            "max_extension_factor": 0.45,
        },
        "capture_policy": {
            "mfe_capture_failure_threshold": 0.22,
            "giveback_lock_threshold": 0.22,
        },
        "learning_bounds": {
            "max_threshold_delta": 0.12,
            "max_tp_extension_factor": 0.45,
            "min_profit_lock_multiplier": 0.45,
            "max_profit_lock_multiplier": 0.90,
        },
        "risk_boundary": {
            "approval_path": "offline_replay_then_governed_release",
            "can_auto_deploy": False,
            "auto_deploy_modes": ["demo_autonomous"],
            "requires_offline_replay": True,
            "adaptive_execution_mode": "governed_execute",
        },
    },
}


def _merge_template(base: dict[str, Any], template: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for policy_key in ("thresholds", "sl_policy", "tp_policy", "capture_policy", "learning_bounds"):
        policy = dict(base.get(policy_key) or {})
        policy.update(dict(template.get(policy_key) or {}))
        merged[policy_key] = policy
    merged.update(
        {
            k: deepcopy(v)
            for k, v in template.items()
            if k not in {"thresholds", "sl_policy", "tp_policy", "capture_policy", "learning_bounds"}
        }
    )
    merged["schema_version"] = str(merged.get("schema_version") or SCHEMA_VERSION)
    merged["template_id"] = str(merged.get("template_id") or "")
    merged["template_version"] = str(merged.get("template_version") or "")
    merged["status"] = str(merged.get("status") or "candidate")
    merged["source"] = str(merged.get("source") or "generated")
    return merged


def _generated_templates_from_state(db_path: str | Path | None = None) -> list[dict[str, Any]]:
    try:
        from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path

        path = Path(db_path or STATE_DB)
        use_pg = is_state_db_path(path)
        conn = get_state_pg_conn(read_only=True) if use_pg else connect_sqlite(path, read_only=True)
        if not use_pg:
            conn.row_factory = sqlite3.Row
    except Exception:
        return []
    payloads: list[Any] = []
    # Application snapshots are evaluated first.  ``setdefault`` below then
    # prevents a newer proposed/approved suggestion with the same template ID
    # from replacing the content that was actually committed and applied.
    try:
        from backend.services.learning_application_store import LearningApplicationStore

        store = LearningApplicationStore(path)
        count = 0
        # Lean store already merged details_json into each dict (scope_type,
        # scope_key, action, status all live in the parsed details blob).
        for app in store.iter_applications(scope_type="position_supervisor_template"):
            if str(app.get("action") or "") != "switch_position_supervisor_template":
                continue
            if str(app.get("status") or "") not in (
                "applied",
                "observing",
                "reinforced",
                "mixed",
            ):
                continue
            payloads.append(app)
            count += 1
            if count >= 100:
                break
    except Exception:
        pass
    try:
        suggestion_rows = conn.execute(
            _sql(
                conn,
                """
                SELECT evidence_json
                FROM policy_suggestion
                WHERE scope_type='position_supervisor_template'
                  AND status IN ('proposed', 'approved', 'applied')
                ORDER BY created_at DESC
                LIMIT 100
                """,
            )
        ).fetchall()
        payloads.extend(row["evidence_json"] for row in suggestion_rows)
    except Exception:
        pass
    finally:
        conn.close()

    result: dict[str, dict[str, Any]] = {}
    for raw_payload in payloads:
        if isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            try:
                payload = json.loads(raw_payload or "{}")
            except Exception:
                continue
        if not isinstance(payload, dict):
            continue
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        raw = (
            payload.get("candidate_template")
            or payload.get("template_snapshot")
            or evidence.get("candidate_template")
            or evidence.get("template_snapshot")
            or {}
        )
        if not isinstance(raw, dict):
            continue
        template_id = str(raw.get("template_id") or "")
        if not template_id.startswith("position_supervisor:"):
            continue
        base_id = str(raw.get("base_template_id") or raw.get("base_template") or PROFIT_PROTECTION_TEMPLATE_ID)
        base = _TEMPLATES.get(base_id) or _TEMPLATES[PROFIT_PROTECTION_TEMPLATE_ID]
        item = _merge_template(base, raw)
        if str(item.get("template_id") or "") and str(item.get("template_version") or ""):
            result.setdefault(item["template_id"], item)
    return list(result.values())


def list_position_supervisor_templates(*, db_path: str | Path | None = None) -> list[dict[str, Any]]:
    items = {template_id: deepcopy(item) for template_id, item in _TEMPLATES.items()}
    for item in _generated_templates_from_state(db_path):
        items[item["template_id"]] = item
    return list(items.values())


def get_position_supervisor_template(template_id: str | None = None, *, db_path: str | Path | None = None) -> dict[str, Any]:
    key = str(template_id or DEFAULT_TEMPLATE_ID)
    if key in _TEMPLATES:
        return deepcopy(_TEMPLATES[key])
    generated = {str(item.get("template_id") or ""): item for item in _generated_templates_from_state(db_path)}
    if key in generated:
        return deepcopy(generated[key])
    return deepcopy(_TEMPLATES[DEFAULT_TEMPLATE_ID])


def latest_applied_position_supervisor_template_id(
    *,
    db_path: str | Path | None = None,
) -> str:
    """Return the newest governance-committed supervisor template.

    A proposal/application row is never a runtime authority by itself.  The
    row must point to a committed/current governance mutation with complete
    config and domain hashes; otherwise startup fails closed.
    """

    try:
        from backend.core.db import (
            STATE_DB,
            connect_sqlite,
            get_state_pg_conn,
            is_state_db_path,
            state_table_columns,
            state_table_exists,
        )

        path = Path(db_path or STATE_DB)
        use_pg = is_state_db_path(path)
        conn = get_state_pg_conn(read_only=True) if use_pg else connect_sqlite(path, read_only=True)
        if not use_pg:
            conn.row_factory = __import__("sqlite3").Row
    except Exception as exc:
        raise RuntimeError(
            f"position_supervisor_restore_state_unavailable:{type(exc).__name__}:{exc}"
        ) from exc
    try:
        from backend.services.learning_application_store import LearningApplicationStore

        store = LearningApplicationStore(path)
        row: dict[str, Any] | None = None
        # Lean store merges details_json into each dict, so action/status/
        # scope_key/mutation_id all come from the parsed details blob.
        for app in store.iter_applications(scope_type="position_supervisor_template"):
            if str(app.get("action") or "") != "switch_position_supervisor_template":
                continue
            if str(app.get("status") or "") not in (
                "applied",
                "observing",
                "reinforced",
                "mixed",
            ):
                continue
            row = app
            break
        if not row:
            return DEFAULT_TEMPLATE_ID

        template_id = str(row.get("scope_key") or "")
        details = dict(row)
        mutation_id = str(row.get("mutation_id") or "")
        details_mutation_id = str(details.get("mutation_id") or "")
        required = {
            "mutation_id",
            "status",
            "projection_status",
            "scope_type",
            "committed_config_hash",
            "domain_hash",
        }
        if (
            not mutation_id
            or details_mutation_id != mutation_id
            or str(details.get("commit_boundary") or "")
            != "governance_mutation_coordinator"
            or not state_table_exists(conn, "governance_mutation_intent")
            or not required <= state_table_columns(conn, "governance_mutation_intent")
        ):
            raise RuntimeError(
                "position_supervisor_restore_unverified:"
                f"{row.get('application_id', '')}:{template_id}"
            )
        intent = conn.execute(
            _sql(
                conn,
                """
                SELECT mutation_id
                FROM governance_mutation_intent
                WHERE mutation_id=?
                  AND status='committed'
                  AND projection_status='current'
                  AND scope_type='supervisor_template'
                  AND committed_config_hash<>''
                  AND domain_hash<>''
                LIMIT 1
                """,
            ),
            (mutation_id,),
        ).fetchone()
        if intent is None:
            raise RuntimeError(
                "position_supervisor_restore_unverified:"
                f"{row.get('application_id', '')}:{template_id}"
            )

        if template_id not in _TEMPLATES:
            evidence = details.get("evidence") if isinstance(details.get("evidence"), dict) else {}
            snapshot = (
                details.get("candidate_template")
                or details.get("template_snapshot")
                or evidence.get("candidate_template")
                or evidence.get("template_snapshot")
                or {}
            )
            if (
                not isinstance(snapshot, dict)
                or str(snapshot.get("template_id") or "") != template_id
                or not str(snapshot.get("template_version") or "")
            ):
                raise RuntimeError(
                    "position_supervisor_committed_snapshot_missing:"
                    f"{row.get('application_id', '')}:{template_id}"
                )

        valid_templates = {str(item.get("template_id") or "") for item in list_position_supervisor_templates(db_path=path)}
        if template_id in valid_templates:
            return template_id
        raise RuntimeError(
            "position_supervisor_restore_template_missing:"
            f"{row.get('application_id', '')}:{template_id}"
        )
    except Exception as exc:
        if isinstance(exc, RuntimeError):
            raise
        raise RuntimeError(
            f"position_supervisor_restore_failed:{type(exc).__name__}:{exc}"
        ) from exc
    finally:
        conn.close()


def normalize_position_supervisor_template(template: dict[str, Any] | str | None = None) -> dict[str, Any]:
    if template is None or template == "":
        return get_position_supervisor_template(DEFAULT_TEMPLATE_ID)
    if isinstance(template, str):
        return get_position_supervisor_template(template)
    base = get_position_supervisor_template(str(template.get("template_id") or DEFAULT_TEMPLATE_ID))
    merged = _merge_template(base, template)
    merged["schema_version"] = str(merged.get("schema_version") or SCHEMA_VERSION)
    merged["template_id"] = str(merged.get("template_id") or DEFAULT_TEMPLATE_ID)
    boundary = dict(merged.get("risk_boundary") or {})
    # observation_only is retained in historical rows for audit/read paths,
    # but an active template can no longer authorize a new live trace with
    # that boundary.
    if str(merged.get("status") or "").strip().lower() == "active":
        boundary["adaptive_execution_mode"] = "governed_execute"
    merged["risk_boundary"] = boundary
    return merged
