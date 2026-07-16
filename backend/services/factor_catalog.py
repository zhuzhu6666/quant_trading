"""Unified factor catalog for runtime and autonomous governance."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
import time
import uuid
from typing import Any

from alpha.factor_cadence import infer_factor_cadence
from alpha.portfolio_compositor import resolve_factor_role
from alpha.registry import factor_registry
from alpha.runtime_factor_selection import select_runtime_factors
from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from config.runtime_config import shared as runtime_config


def _connect_state(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    if is_state_db_path(db_path):
        return get_state_pg_conn(read_only=read_only)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


def ensure_factor_catalog_snapshot_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect_state(db_path)
    try:
        conn.execute(
            _p(db_path, """
            CREATE TABLE IF NOT EXISTS factor_catalog_snapshot (
                snapshot_id TEXT PRIMARY KEY,
                run_id TEXT DEFAULT '',
                catalog_hash TEXT DEFAULT '',
                catalog_json TEXT NOT NULL DEFAULT '[]',
                source TEXT DEFAULT '',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """)
        )
        try:
            conn.execute(
                _p(db_path, "CREATE INDEX IF NOT EXISTS idx_factor_catalog_snapshot_created ON factor_catalog_snapshot(created_at)")
            )
        except Exception:
            pass
        conn.commit()
    finally:
        conn.close()


def _health_by_factor(db_path: str | Path = STATE_DB) -> dict[str, dict[str, Any]]:
    try:
        conn = _connect_state(db_path, read_only=True)
        try:
            rows = conn.execute(
                "SELECT factor, score, status, n_obs, rolling_ic, components_json, updated_at FROM factor_health"
            ).fetchall()
            return {
                str(row["factor"]): {
                    "score": float(row["score"] or 0.0),
                    "status": str(row["status"] or "UNKNOWN"),
                    "n_obs": int(row["n_obs"] or 0),
                    "rolling_ic": float(row["rolling_ic"] or 0.0),
                    "components": _loads(row["components_json"], {}),
                    "updated_at": float(row["updated_at"] or 0.0),
                }
                for row in rows
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _canary_by_factor(db_path: str | Path = STATE_DB) -> dict[str, dict[str, Any]]:
    try:
        conn = _connect_state(db_path, read_only=True)
        try:
            rows = conn.execute(
                """SELECT factor_name, stage, oos_bars, cumulative_pnl,
                          evidence_hash, dataset_hash, evidence_end_at,
                          stage_evidence_hash, fresh_evidence_bars, updated_at
                   FROM canary_state"""
            ).fetchall()
            return {
                str(row["factor_name"]): {
                    "stage": str(row["stage"] or "SHADOW").upper(),
                    "oos_bars": int(row["oos_bars"] or 0),
                    "cumulative_pnl": float(row["cumulative_pnl"] or 0.0),
                    "evidence_hash": str(row["evidence_hash"] or ""),
                    "dataset_hash": str(row["dataset_hash"] or ""),
                    "evidence_end_at": str(row["evidence_end_at"] or ""),
                    "stage_evidence_hash": str(row["stage_evidence_hash"] or ""),
                    "fresh_evidence_bars": int(row["fresh_evidence_bars"] or 0),
                    "updated_at": float(row["updated_at"] or 0.0),
                }
                for row in rows
                if str(row["factor_name"] or "")
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _latest_policy_by_factor() -> dict[str, dict[str, Any]]:
    return _latest_policy_by_factor_for_db(STATE_DB)


def _latest_policy_by_factor_for_db(db_path: str | Path = STATE_DB) -> dict[str, dict[str, Any]]:
    try:
        conn = _connect_state(db_path, read_only=True)
        try:
            rows = conn.execute(
                """
                SELECT scope_key, action, confidence, status, created_at
                FROM policy_suggestion
                WHERE scope_type='factor'
                ORDER BY created_at DESC
                """
            ).fetchall()
            latest: dict[str, dict[str, Any]] = {}
            for row in rows:
                key = str(row["scope_key"])
                if key in latest:
                    continue
                latest[key] = {
                    "action": str(row["action"] or ""),
                    "confidence": float(row["confidence"] or 0.0),
                    "status": str(row["status"] or ""),
                    "created_at": float(row["created_at"] or 0.0),
                }
            return latest
        finally:
            conn.close()
    except Exception:
        return {}


def _factor_governance_shadow_by_factor(db_path: str | Path = STATE_DB) -> dict[str, dict[str, Any]]:
    try:
        conn = _connect_state(db_path, read_only=True)
        try:
            aggregate_rows = conn.execute(
                """
                SELECT factor,
                       COUNT(*) AS sample_count,
                       AVG(weakness_score) AS avg_weakness_score,
                       AVG(positive_score) AS avg_positive_score,
                       SUM(CASE WHEN weakness_score >= 0.65 THEN 1 ELSE 0 END) AS weak_sample_count,
                       MAX(created_at) AS latest_created_at
                FROM factor_governance_shadow_audit
                GROUP BY factor
                """
            ).fetchall()
            result = {
                str(row["factor"]): {
                    "sample_count": int(row["sample_count"] or 0),
                    "weak_sample_count": int(row["weak_sample_count"] or 0),
                    "avg_weakness_score": float(row["avg_weakness_score"] or 0.0),
                    "avg_positive_score": float(row["avg_positive_score"] or 0.0),
                    "latest_created_at": float(row["latest_created_at"] or 0.0),
                }
                for row in aggregate_rows
                if str(row["factor"] or "")
            }
            latest_rows = conn.execute(
                """
                SELECT inference_id, factor, model_type, model_version, artifact_path,
                       mode, positive_score, weakness_score, prediction,
                       payload_json, result_json, created_at
                FROM factor_governance_shadow_audit
                ORDER BY created_at DESC
                LIMIT 5000
                """
            ).fetchall()
            seen: set[str] = set()
            for row in latest_rows:
                factor = str(row["factor"] or "")
                if not factor or factor in seen:
                    continue
                seen.add(factor)
                item = result.setdefault(factor, {})
                item.update({
                    "latest_inference_id": str(row["inference_id"] or ""),
                    "model_type": str(row["model_type"] or ""),
                    "model_version": str(row["model_version"] or ""),
                    "artifact_path": str(row["artifact_path"] or ""),
                    "mode": str(row["mode"] or ""),
                    "positive_score": float(row["positive_score"] or 0.0),
                    "weakness_score": float(row["weakness_score"] or 0.0),
                    "prediction": int(row["prediction"] or 0),
                    "payload": _loads(row["payload_json"], {}),
                    "result": _loads(row["result_json"], {}),
                    "created_at": float(row["created_at"] or 0.0),
                })
            return result
        finally:
            conn.close()
    except Exception:
        return {}


def _latest_catalog_snapshot_meta(db_path: str | Path = STATE_DB) -> dict[str, Any]:
    try:
        ensure_factor_catalog_snapshot_table(db_path)
        conn = _connect_state(db_path, read_only=True)
        try:
            row = conn.execute(
                _p(db_path, """
                SELECT snapshot_id, run_id, catalog_hash, source, created_at
                FROM factor_catalog_snapshot
                ORDER BY created_at DESC
                LIMIT 1
                """)
            ).fetchone()
            if not row:
                return {}
            return {
                "snapshot_id": str(row["snapshot_id"] or ""),
                "run_id": str(row["run_id"] or ""),
                "catalog_hash": str(row["catalog_hash"] or ""),
                "source": str(row["source"] or ""),
                "created_at": float(row["created_at"] or 0.0),
            }
        finally:
            conn.close()
    except Exception:
        return {}


def _shadow_perf(name: str) -> dict[str, Any]:
    try:
        from alpha.shadow_trader import load_shadow_perf

        perf = load_shadow_perf(name)
        if perf is None:
            return {}
        if hasattr(perf, "__dict__"):
            return dict(perf.__dict__)
        return dict(perf)
    except Exception:
        return {}


def build_factor_catalog(db_path: str | Path = STATE_DB) -> list[dict[str, Any]]:
    cfg = runtime_config()
    signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
    weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
    selection = select_runtime_factors(signal_cfg)
    selected = set(selection.selected_factor_ids if selection is not None else factor_registry.list())
    excluded_reasons = dict(selection.reason_excluded if selection is not None else {})
    selection_source = "local_fallback"
    try:
        from backend.services.runtime_factor_selection_projection import RuntimeFactorSelectionProjectionService

        projection = RuntimeFactorSelectionProjectionService(db_path).latest()
        if projection.get("ok"):
            selected = set(projection.get("selected_factor_ids") or [])
            excluded_reasons = dict(projection.get("reason_excluded") or {})
            selection_source = "live_runtime_projection"
    except Exception:
        pass

    try:
        from alpha.registry_adapter import RegistryAdapter

        adapter = RegistryAdapter.shared()
        meta_names = set(adapter._meta.keys())
        dead = set(adapter.dead_names())
    except Exception:
        adapter = None
        meta_names = set()
        dead = set()

    health = _health_by_factor(db_path)
    canary = _canary_by_factor(db_path)
    latest_policy = _latest_policy_by_factor_for_db(db_path)
    factor_governance_shadow = _factor_governance_shadow_by_factor(db_path)
    latest_snapshot = _latest_catalog_snapshot_meta(db_path)
    names = sorted(
        set(factor_registry.list())
        | set(signal_cfg)
        | set(weights)
        | meta_names
        | set(health)
        | set(factor_governance_shadow)
        | set(canary)
    )

    items: list[dict[str, Any]] = []
    now = time.time()
    for name in names:
        meta = adapter.get_meta(name) if adapter is not None else {}
        source = str(meta.get("source") or ("builtin" if factor_registry.get(name) else "unknown"))
        cfg_entry = signal_cfg.get(name)
        cfg_dict = cfg_entry if isinstance(cfg_entry, dict) else {}
        role = resolve_factor_role(name, cfg_dict)
        enabled = not (isinstance(cfg_entry, dict) and cfg_entry.get("enabled") is False)
        configured_lifecycle = str(cfg_dict.get("lifecycle_status") or "").upper()
        lifecycle_status = "DEAD" if name in dead else (
            configured_lifecycle
            if configured_lifecycle
            else str(
                getattr(adapter, "_lifecycle_statuses", {}).get(name) if adapter is not None else ""
                or ("ACTIVE" if factor_registry.get(name) else "UNKNOWN")
            )
        )
        weight = float(weights.get(name, 0.3 if source == "discovered" else 0.0) or 0.0)
        eligible = (
            name in selected
            and enabled
            and lifecycle_status not in {"DEAD", "SHADOW", "QUARANTINE", "QUARANTINED"}
            and source != "shadow"
        )
        used_in_score = bool(eligible and role == "alpha" and weight > 0)
        cadence, sample_policy = infer_factor_cadence(name, cfg_dict)
        policy = latest_policy.get(name, {})
        reason = "" if eligible else excluded_reasons.get(name)
        if not reason:
            if source == "shadow":
                reason = "shadow_only"
            elif lifecycle_status == "DEAD":
                reason = "lifecycle_dead"
            elif not enabled:
                reason = "disabled_by_runtime_config"
            elif role != "alpha" and weight <= 0:
                reason = "observe_only"
        h = health.get(name, {})
        fg_shadow = factor_governance_shadow.get(name, {})
        items.append({
            "factor_id": name,
            "source": source,
            "role": role,
            "enabled": bool(enabled),
            "lifecycle_status": lifecycle_status,
            "eligible_for_live": bool(eligible),
            "used_in_score": used_in_score,
            "weight": weight,
            "cadence": cadence,
            "history_sample_policy": sample_policy,
            "health_status": str(h.get("status") or "UNKNOWN"),
            "health_score": float(h.get("score") or 0.0),
            "health_n_obs": int(h.get("n_obs") or 0),
            "health_updated_at": float(h.get("updated_at") or 0.0),
            "canary": canary.get(name, {}),
            "shadow_perf": _shadow_perf(name) if source in {"shadow", "discovered"} else {},
            "factor_governance_shadow": fg_shadow,
            "model_weakness_score": float(fg_shadow.get("weakness_score") or fg_shadow.get("avg_weakness_score") or 0.0),
            "model_positive_score": float(fg_shadow.get("positive_score") or fg_shadow.get("avg_positive_score") or 0.0),
            "redundancy_group": str(cfg_dict.get("redundancy_group") or ""),
            "redundancy_leader": str(cfg_dict.get("redundancy_leader") or ""),
            "context_policy_effect": {},
            "governance_action": str(policy.get("action") or ""),
            "governance_status": str(policy.get("status") or ""),
            "last_action_ts": float(policy.get("created_at") or meta.get("promote_time") or meta.get("register_time") or 0.0),
            "rollback_state": "available" if policy else "",
            "reason_excluded": reason,
            "runtime_selection_source": selection_source,
            "catalog_ts": now,
            "latest_catalog_snapshot_id": str(latest_snapshot.get("snapshot_id") or ""),
            "latest_catalog_snapshot_run_id": str(latest_snapshot.get("run_id") or ""),
        })
    return items


def persist_factor_catalog_snapshot(
    catalog: list[dict[str, Any]] | None = None,
    *,
    run_id: str = "",
    source: str = "factor_governance",
    db_path: str | Path = STATE_DB,
) -> dict[str, Any]:
    ensure_factor_catalog_snapshot_table(db_path)
    payload = list(catalog if catalog is not None else build_factor_catalog(db_path))
    now = time.time()
    snapshot_id = f"fcatsnap_{uuid.uuid4().hex[:16]}"
    catalog_hash = _hash(payload)
    conn = _connect_state(db_path)
    try:
        conn.execute(
            _p(db_path, """
            INSERT INTO factor_catalog_snapshot
            (snapshot_id, run_id, catalog_hash, catalog_json, source, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """),
            (snapshot_id, str(run_id or ""), catalog_hash, _dumps(payload), str(source or ""), now),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "snapshot_id": snapshot_id,
        "run_id": str(run_id or ""),
        "catalog_hash": catalog_hash,
        "source": str(source or ""),
        "created_at": now,
        "count": len(payload),
    }


def latest_factor_catalog_snapshot(db_path: str | Path = STATE_DB) -> dict[str, Any]:
    ensure_factor_catalog_snapshot_table(db_path)
    conn = _connect_state(db_path, read_only=True)
    try:
        row = conn.execute(
            _p(db_path, """
            SELECT snapshot_id, run_id, catalog_hash, catalog_json, source, created_at
            FROM factor_catalog_snapshot
            ORDER BY created_at DESC
            LIMIT 1
            """)
        ).fetchone()
        if not row:
            return {"ok": False, "status": "missing", "items": [], "count": 0}
        items = _loads(row["catalog_json"], [])
        if not isinstance(items, list):
            items = []
        return {
            "ok": True,
            "schema_version": "factor_catalog_snapshot.v1",
            "snapshot_id": str(row["snapshot_id"] or ""),
            "run_id": str(row["run_id"] or ""),
            "catalog_hash": str(row["catalog_hash"] or ""),
            "source": str(row["source"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "items": items,
            "count": len(items),
        }
    finally:
        conn.close()
