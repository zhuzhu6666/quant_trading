from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import threading
import time
from typing import Any

from alpha.registry import factor_registry
from alpha.registry_adapter import RegistryAdapter
from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)
from backend.services.canonical_v2_reader import (
    iter_decision_factor_snapshots_by_factor,
    iter_decision_rows,
    latest_review_observed_at_by_id,
    review_row,
)
from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    execute as _execute,
    load_json as _loads,
    pg_sql as _sql,
)


def _use_pg(db_path: str | Path) -> bool:
    return is_state_db_path(db_path)


_CARD_CACHE_TTL_SEC = 60.0
_EVIDENCE_SNAPSHOT_LIMIT = 2000
_CANDIDATE_MIN_LIMIT = 250
_CARD_CACHE_LOCK = threading.Lock()
_CARD_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_ADMISSION_MIN_MATURE_EVIDENCE = 20
_TERMINAL_EFFECT_STATUSES = frozenset(
    {"effective", "ineffective", "mixed", "inconclusive", "rolled_back", "superseded"}
)


def clear_factor_card_cache(db_path: str | Path | None = None) -> None:
    prefix = f"{Path(db_path).resolve()}|" if db_path else None
    with _CARD_CACHE_LOCK:
        if prefix is None:
            _CARD_CACHE.clear()
            return
        for key in list(_CARD_CACHE):
            if key.startswith(prefix):
                _CARD_CACHE.pop(key, None)


def _round(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


def _optional_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed else None


def build_factor_admission_evidence(
    *,
    factor_id: str,
    catalog_item: dict[str, Any],
    evidence_counts: dict[str, Any],
    governance: dict[str, Any],
    now_ts: float | None = None,
    health_max_age_seconds: float | None = None,
) -> dict[str, Any]:
    """Build one fail-closed Candidate Card admission projection.

    This is a pure projection over existing lifecycle, health, Canary,
    runtime-selection and learning-effect facts.  It does not calculate a new
    lifecycle decision and never writes state.
    """

    now = float(time.time() if now_ts is None else now_ts)
    lifecycle_evidence = dict(catalog_item.get("lifecycle_evidence") or {})
    persisted = dict(lifecycle_evidence.get("admission_evidence") or {})
    validation_source = dict(
        lifecycle_evidence.get("candidate_validation")
        or persisted.get("validation")
        or {}
    )
    role = str(catalog_item.get("role") or "").lower()
    direction = catalog_item.get("direction")
    try:
        direction = 1 if float(direction) > 0 else -1 if float(direction) < 0 else 0
    except (TypeError, ValueError):
        direction = 0
    signed_ic = _optional_float(
        validation_source.get("signed_ic_mean")
        if validation_source.get("signed_ic_mean") is not None
        else catalog_item.get("health_rolling_ic")
    )
    directional_vote_allowed = role == "alpha"
    normalized_signed_ic = (
        float(signed_ic) * int(direction)
        if directional_vote_allowed
        and signed_ic is not None
        and direction in {-1, 1}
        else None
    )
    if not directional_vote_allowed:
        direction_status = "non_directional"
    elif direction not in {-1, 1}:
        direction_status = "missing_explicit_direction"
    elif signed_ic is None or abs(float(signed_ic)) <= 1e-12:
        direction_status = "signed_ic_unavailable"
    elif float(normalized_signed_ic or 0.0) <= 0.0:
        direction_status = "signed_ic_direction_mismatch"
    else:
        direction_status = "validated"
    direction_contract = {
        "role": role or "unknown",
        "directional_vote_allowed": directional_vote_allowed,
        "raw_sign": (
            1 if (signed_ic or 0.0) > 0 else -1 if (signed_ic or 0.0) < 0 else None
        ),
        "normalized_sign": (
            1
            if (normalized_signed_ic or 0.0) > 0
            else -1
            if (normalized_signed_ic or 0.0) < 0
            else None
        ),
        "direction": (
            direction
            if directional_vote_allowed and direction in {-1, 1}
            else None
        ),
        "polarity": (
            "positive"
            if directional_vote_allowed and direction == 1
            else "negative"
            if directional_vote_allowed and direction == -1
            else None
        ),
        "normalizer": str(catalog_item.get("normalizer") or "") or None,
        "signed_ic": _round(signed_ic) if signed_ic is not None else None,
        "magnitude_ic": _round(abs(signed_ic)) if signed_ic is not None else None,
        "normalized_signed_ic": (
            _round(normalized_signed_ic) if normalized_signed_ic is not None else None
        ),
        "status": direction_status,
    }

    loaded = dict(catalog_item.get("loaded_projection") or {})
    generation = int(catalog_item.get("lifecycle_generation") or 0)
    artifact_hash = str(catalog_item.get("lifecycle_artifact_hash") or "")
    definition_fingerprint = str(
        catalog_item.get("lifecycle_definition_fingerprint") or ""
    )
    loaded_matches = bool(
        loaded.get("loaded")
        and str(loaded.get("status") or "").lower() in {"loaded", "current", "healthy"}
        and int(loaded.get("generation") or 0) == generation
        and str(loaded.get("artifact_hash") or "") == artifact_hash
    )
    lineage = {
        "factor_id": str(catalog_item.get("lifecycle_factor_id") or factor_id),
        "generation": generation or None,
        "artifact_hash": artifact_hash or None,
        "definition_fingerprint": definition_fingerprint or None,
        "loaded_projection": {
            "status": str(loaded.get("status") or "unavailable"),
            "projection_id": str(loaded.get("projection_id") or "") or None,
            "process_role": str(loaded.get("process_role") or "") or None,
            "loaded": bool(loaded.get("loaded")),
            "matches_generation": loaded_matches,
        },
        "factor_set_fingerprint": (
            catalog_item.get("runtime_selection_fingerprint") or None
        ),
        "config_hash": (
            catalog_item.get("lifecycle_config_hash")
            or catalog_item.get("runtime_config_hash")
            or None
        ),
    }
    lineage_complete = bool(
        generation > 0
        and artifact_hash
        and definition_fingerprint
        and lineage["factor_set_fingerprint"]
        and lineage["config_hash"]
    )

    shadow = dict(catalog_item.get("shadow_perf") or {})
    canary = dict(catalog_item.get("canary") or {})
    # A completed canary ladder is the OOS validation package for bar-based
    # (GP/discovered) candidates: staged, fingerprinted, real-bar evidence
    # accumulated over 30+ days that these candidates can never replace with
    # enrollment-time validation artifacts.  At PROBATION/ACTIVE the ladder
    # substitutes for evidence that is structurally absent for them
    # (trade-review health, cost test, execution evidence, contamination
    # status, regime coverage).  Absence is waived; negative evidence
    # (DECAYING health, a failed cost test, contaminated counts) still blocks.
    canary_ladder_evidence = (
        str(canary.get("stage") or "").upper() in {"PROBATION", "ACTIVE"}
    )
    mature_reviews = evidence_counts.get("governance_eligible_mature")
    try:
        mature_reviews_int = int(mature_reviews) if mature_reviews is not None else 0
    except (TypeError, ValueError):
        mature_reviews_int = 0
    shadow_mature = int(shadow.get("n_valid") or 0) if shadow.get("evidence_hash") and shadow.get("dataset_hash") else 0
    mature_count = max(mature_reviews_int, shadow_mature)
    contaminated = evidence_counts.get("contaminated_or_ineligible")
    contamination_known = contaminated is not None
    try:
        contaminated_count = int(contaminated) if contaminated is not None else 0
    except (TypeError, ValueError):
        contaminated_count = 0
    contamination_status = str(
        validation_source.get("contamination_status")
        or ("clean" if contamination_known and contaminated_count == 0 else "contaminated" if contaminated_count > 0 else "unknown")
    ).lower()
    execution_complete = bool(
        validation_source.get("execution_evidence_complete")
        or (mature_reviews_int >= _ADMISSION_MIN_MATURE_EVIDENCE and contamination_status == "clean")
    )
    regime_ids = [
        str(item)
        for item in list(validation_source.get("regime_ids") or [])
        if str(item)
    ]
    validation = {
        "pit_passed": bool(validation_source.get("pit_passed")),
        "walk_forward_passed": bool(validation_source.get("walk_forward_passed")),
        "multi_forward_passed": bool(validation_source.get("multi_forward_passed")),
        "cost_test_passed": bool(validation_source.get("cost_test_passed")),
        "bar_oos": {
            "stage": str(canary.get("stage") or ""),
            "oos_bars": int(shadow.get("oos_bars") or canary.get("oos_bars") or 0),
            "n_valid": int(shadow.get("n_valid") or 0),
            "evidence_hash": str(shadow.get("evidence_hash") or canary.get("evidence_hash") or "") or None,
            "dataset_hash": str(shadow.get("dataset_hash") or canary.get("dataset_hash") or "") or None,
            "research_only": True,
        },
        "independent_mature_evidence_count": mature_count,
        "required_independent_mature_evidence_count": _ADMISSION_MIN_MATURE_EVIDENCE,
        "execution_evidence_complete": execution_complete,
        "contamination_status": contamination_status,
        "contaminated_or_ineligible": contaminated_count if contamination_known else None,
        "regime_coverage": {
            "count": len(set(regime_ids)),
            "regime_ids": sorted(set(regime_ids)),
        },
        "signed_ic_mean": signed_ic,
        "multi_forward": dict(validation_source.get("multi_forward") or {}),
        "walk_forward": dict(validation_source.get("walk_forward") or {}),
        "cost_test": dict(validation_source.get("cost_test") or {}),
    }

    health_updated_at = float(catalog_item.get("health_updated_at") or 0.0)
    health_age = now - health_updated_at if health_updated_at > 0.0 else float("inf")
    if health_max_age_seconds is not None:
        health_max_age = float(health_max_age_seconds)
    else:
        try:
            from config.runtime_config import shared as _runtime_config

            health_max_age = float(
                getattr(_runtime_config(), "factor_governance_health_max_age_seconds", 900.0)
                or 900.0
            )
        except Exception:
            health_max_age = 900.0
    health_status = str(catalog_item.get("health_status") or "UNKNOWN").upper()
    health_fresh = bool(-5.0 <= health_age <= health_max_age)
    health_valid = bool(health_fresh and health_status in {"HEALTHY", "WATCH"})
    lifecycle_stage = str(catalog_item.get("lifecycle_status") or "UNKNOWN").upper()
    v16 = dict(lifecycle_evidence.get("v16") or persisted.get("governance", {}).get("v16") or {})
    v16_bound = bool(v16.get("command_id") and v16.get("candidate_id"))
    coordinator_bound = bool(
        catalog_item.get("lifecycle_mutation_id")
        and str(catalog_item.get("runtime_admission") or "").lower()
        in {"projection_acknowledged", "admitted"}
    )
    governance_projection = {
        "lifecycle": lifecycle_stage,
        "health": {
            "status": health_status,
            "score": _round(catalog_item.get("health_score")),
            "n_obs": int(catalog_item.get("health_n_obs") or 0),
            "updated_at": health_updated_at or None,
            "age_seconds": _round(health_age) if health_age != float("inf") else None,
            "fresh": health_fresh,
        },
        "prepared": lifecycle_stage == "PROMOTION_PREPARED",
        "v16": {**v16, "bound": v16_bound},
        "coordinator": {
            "mutation_id": str(catalog_item.get("lifecycle_mutation_id") or "") or None,
            "runtime_admission": str(catalog_item.get("runtime_admission") or "") or None,
            "bound": coordinator_bound,
        },
        "canary": {
            "stage": str(canary.get("stage") or "") or None,
            "evidence_source": (
                "canary_ladder" if canary_ladder_evidence else "enrollment_validation"
            ),
        },
    }

    effect_status = str(governance.get("application_effect_status") or "").lower()
    effect_decision = dict(governance.get("application_effect_decision") or {})
    effect_quality = dict(effect_decision.get("evidence_quality") or {})
    effect_positive_mature = bool(
        effect_status == "effective"
        and effect_quality.get("bounded_attribution_allowed") is True
    )
    effect = {
        "application_id": governance.get("latest_application_id") or None,
        "application_status": governance.get("latest_application_status") or None,
        "status": effect_status or "missing",
        "maturity": "mature" if effect_status in _TERMINAL_EFFECT_STATUSES else "observing" if effect_status else "missing",
        "conclusion": effect_status or "unknown",
        "observed_trade_count": int(governance.get("application_effect_trade_count") or 0),
        "delta_avg_reward": governance.get("application_effect_delta"),
        "bounded_attribution_allowed": effect_quality.get("bounded_attribution_allowed"),
        "positive_mature": effect_positive_mature,
    }

    preflight_blockers: list[str] = []
    if directional_vote_allowed and direction_status != "validated":
        preflight_blockers.append("direction_contract_invalid")
    if not lineage_complete:
        preflight_blockers.append("lineage_missing")
    for field, code in (
        ("pit_passed", "pit_evidence_missing"),
        ("walk_forward_passed", "walk_forward_evidence_missing"),
        ("multi_forward_passed", "multi_forward_evidence_missing"),
        ("cost_test_passed", "cost_evidence_missing"),
    ):
        if validation[field]:
            continue
        if canary_ladder_evidence and (
            validation_source.get(field) is None
            or (
                field == "cost_test_passed"
                and str(
                    (validation_source.get("cost_test") or {}).get("status") or ""
                )
                == "not_evaluated"
            )
        ):
            # Never validated: the ladder is the sequential OOS / point-in-time
            # evaluation for bar-based candidates (see above).  The known
            # validation producer hardcodes cost_test_passed=False with
            # cost_test.status="not_evaluated" — a placeholder for "not run",
            # not a failed test.  An explicit False without that marker keeps
            # blocking (fail-closed for a real failed check).
            continue
        preflight_blockers.append(code)
    if str(canary.get("stage") or "").upper() not in {"ACTIVE", "PROBATION"}:
        # PROBATION is the terminal canary evidence stage.  The final hop to
        # ACTIVE requires committed lifecycle backing (D1 gate), and lifecycle
        # activation is produced by the promotion path that consumes this
        # preflight — requiring ACTIVE here circularly starved the pipeline.
        preflight_blockers.append("bar_oos_canary_incomplete")
    if mature_count < _ADMISSION_MIN_MATURE_EVIDENCE:
        preflight_blockers.append("independent_mature_evidence_below_20")
    if not execution_complete:
        if canary_ladder_evidence and not validation_source.get(
            "execution_evidence_complete"
        ):
            pass  # absent, never executed: ladder evidence covers it
        else:
            preflight_blockers.append("execution_evidence_incomplete")
    if contamination_status != "clean":
        if canary_ladder_evidence and contamination_status == "unknown":
            pass  # no contamination scan exists: ladder evidence covers it
        else:
            preflight_blockers.append("contamination_unresolved")
    if not health_valid and not (
        canary_ladder_evidence and health_status == "UNKNOWN"
    ):
        # UNKNOWN means the factor was never health-monitored (shadow
        # candidates produce no trade reviews); the ladder substitutes.
        # DECAYING, WATCH-below-threshold and stale-fresh rows are real
        # evidence and still block.
        preflight_blockers.append("factor_health_invalid_or_stale")
    if not regime_ids and not canary_ladder_evidence:
        preflight_blockers.append("regime_coverage_missing")
    preflight_blockers = sorted(set(preflight_blockers))
    activation_blockers = list(preflight_blockers)
    if lifecycle_stage == "PROMOTION_PREPARED":
        if not loaded_matches:
            activation_blockers.append("loaded_projection_missing_or_mismatched")
        if not v16_bound:
            activation_blockers.append("v16_binding_missing")
        if not coordinator_bound:
            activation_blockers.append("coordinator_binding_missing")
    elif lifecycle_stage != "ACTIVE":
        activation_blockers.append("promotion_not_prepared")
    weight_blockers: list[str] = []
    if lifecycle_stage != "ACTIVE":
        weight_blockers.append("factor_not_active")
    if not bool(catalog_item.get("activation_canary")):
        weight_blockers.append("controlled_active_canary_contract_missing")
    if not effect_positive_mature:
        weight_blockers.append("application_effect_not_mature_positive")
    if not lineage_complete:
        weight_blockers.append("lineage_missing")
    activation_blockers = sorted(set(activation_blockers))
    weight_blockers = sorted(set(weight_blockers))
    return {
        "schema_version": "factor_admission_evidence.v1",
        "direction": direction_contract,
        "lineage": lineage,
        "validation": validation,
        "governance": governance_projection,
        "effect": effect,
        "eligible_for_preparation": not preflight_blockers,
        "eligible_for_activation": (
            lifecycle_stage == "PROMOTION_PREPARED" and not activation_blockers
        ),
        "eligible_for_weight_expansion": not weight_blockers,
        "preflight_blocker_codes": preflight_blockers,
        "activation_blocker_codes": activation_blockers,
        "weight_expansion_blocker_codes": weight_blockers,
        "blocker_codes": sorted(set(activation_blockers + weight_blockers)),
    }


_FAMILY_DEFAULTS: dict[str, dict[str, Any]] = {
    "momentum": {
        "expected_regimes": ["trend", "breakout"],
        "weak_regimes": ["range"],
        "expected_holding_profile": {"style": "short_swing", "min_bars": 2, "max_bars": 16},
    },
    "momentum_oscillator": {
        "expected_regimes": ["range", "mean_reversion"],
        "weak_regimes": ["strong_trend"],
        "expected_holding_profile": {"style": "short_swing", "min_bars": 2, "max_bars": 12},
    },
    "trend": {
        "expected_regimes": ["trend", "breakout"],
        "weak_regimes": ["range"],
        "expected_holding_profile": {"style": "trend_follow", "min_bars": 6, "max_bars": 48},
    },
    "volatility": {
        "expected_regimes": ["high_vol", "breakout"],
        "weak_regimes": ["low_vol"],
        "expected_holding_profile": {"style": "event_driven", "min_bars": 1, "max_bars": 10},
    },
    "volume": {
        "expected_regimes": ["breakout", "high_vol"],
        "weak_regimes": ["low_vol"],
        "expected_holding_profile": {"style": "confirmation", "min_bars": 1, "max_bars": 8},
    },
    "pattern": {
        "expected_regimes": ["range", "breakout"],
        "weak_regimes": ["macro_drift"],
        "expected_holding_profile": {"style": "short_swing", "min_bars": 1, "max_bars": 6},
    },
    "macro": {
        "expected_regimes": ["macro_drift", "trend"],
        "weak_regimes": ["low_vol"],
        "expected_holding_profile": {"style": "swing", "min_bars": 12, "max_bars": 80},
    },
    "calendar": {
        "expected_regimes": ["event_risk"],
        "weak_regimes": ["low_vol"],
        "expected_holding_profile": {"style": "event_window", "min_bars": 1, "max_bars": 4},
    },
    "cross_asset": {
        "expected_regimes": ["macro_drift", "trend"],
        "weak_regimes": ["range"],
        "expected_holding_profile": {"style": "swing", "min_bars": 8, "max_bars": 60},
    },
    "ml_signal": {
        "expected_regimes": ["trend", "range"],
        "weak_regimes": ["event_risk"],
        "expected_holding_profile": {"style": "adaptive", "min_bars": 2, "max_bars": 24},
    },
    "composite": {
        "expected_regimes": ["trend", "range"],
        "weak_regimes": ["event_risk"],
        "expected_holding_profile": {"style": "adaptive", "min_bars": 4, "max_bars": 24},
    },
}


class FactorCardService:
    def __init__(self, db_path: str | None = None, *, ensure_schema: bool = True):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        if ensure_schema:
            self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = get_state_pg_conn() if _use_pg(self.db_path) else connect_sqlite(self.db_path)
        if not _use_pg(self.db_path):
            conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
            if not _conn_is_pg(conn):
                conn.executescript(STATE_DB_DDL)
            conn.commit()

    def list_cards(
        self,
        *,
        limit: int = 100,
        source: str | None = None,
        lifecycle_status: str | None = None,
        factor_id: str | None = None,
        factor_family: str | None = None,
        responsibility: str | None = None,
    ) -> list[dict[str, Any]]:
        cache_key = (
            f"{self.db_path.resolve()}|limit:{int(limit)}|{source or '*'}|{lifecycle_status or '*'}|"
            f"{factor_id or '*'}|{factor_family or '*'}|{responsibility or '*'}"
        )
        now_ts = time.time()
        with _CARD_CACHE_LOCK:
            cached = _CARD_CACHE.get(cache_key)
            if cached and cached[0] > now_ts:
                return deepcopy(cached[1][:limit])

        with self._conn() as conn:
            ids = self._factor_ids(conn=conn)
            catalog_by_factor: dict[str, dict[str, Any]] = {}
            runtime_projection: dict[str, Any] = {}
            try:
                from backend.services.factor_catalog import (
                    build_factor_catalog,
                    latest_factor_catalog_snapshot,
                )

                latest_snapshot = latest_factor_catalog_snapshot(self.db_path)
                catalog_items = (
                    latest_snapshot.get("items")
                    if latest_snapshot.get("ok")
                    and isinstance(latest_snapshot.get("items"), list)
                    else build_factor_catalog(self.db_path)
                )
                catalog_by_factor = {
                    str(item.get("factor_id") or ""): item
                    for item in catalog_items
                    if str(item.get("factor_id") or "")
                }
                ids = sorted(set(ids) | set(catalog_by_factor))
            except Exception:
                catalog_by_factor = {}
            try:
                from backend.services.runtime_factor_selection_projection import (
                    RuntimeFactorSelectionProjectionService,
                )

                runtime_projection = RuntimeFactorSelectionProjectionService(
                    self.db_path
                ).latest(max_age_seconds=900.0)
            except Exception:
                runtime_projection = {"status": "unavailable", "ok": False}
            if factor_id:
                ids = [factor_id]
            elif responsibility:
                # Responsibility filtering must inspect the complete catalog
                # before ranking.  Ranking the catalog first could discard a
                # shadow factor that is the only current parameter suspect.
                ids = sorted(ids)
            else:
                candidate_cap = max(_CANDIDATE_MIN_LIMIT, int(limit) * (10 if (source or lifecycle_status or factor_family) else 5))
                ids = self._rank_candidate_ids(ids, catalog_by_factor, candidate_cap)
            evidence_by_factor: dict[str, dict[str, Any]] = {}
            try:
                from research.features.feature_provider import LearningFeatureProvider

                evidence_by_factor = LearningFeatureProvider(
                    self.db_path
                ).factor_evidence_summary(ids)
            except Exception:
                evidence_by_factor = {}
            review_evidence_by_factor = self._batch_review_evidence(
                conn,
                ids,
            )
            card_evidence_by_factor: dict[str, dict[str, Any]] = {}
            for name in ids:
                merged = dict(evidence_by_factor.get(name) or {})
                merged.update(review_evidence_by_factor.get(name) or {})
                card_evidence_by_factor[name] = merged
            if responsibility:
                responsibility_key = str(responsibility).strip().lower()
                if responsibility_key != "parameter":
                    raise ValueError(
                        f"unsupported factor card responsibility filter: {responsibility}"
                    )
                if not all(
                    str(card_evidence_by_factor[name].get("status") or "") == "available"
                    and str(
                        card_evidence_by_factor[name].get("review_evidence_status") or ""
                    ) == "available"
                    for name in ids
                ):
                    # Parameter recommendations are governance inputs.  A
                    # partial batch must not fall back to stale/per-factor
                    # evidence and produce a recommendation.
                    return []
                ids = [
                    name
                    for name in ids
                    if str(
                        card_evidence_by_factor[name].get(
                            "last_primary_responsibility"
                        )
                        or ""
                    )
                    == "parameter"
                    or "factor_logic_ok_but_param_suspect"
                    in list(
                        card_evidence_by_factor[name].get(
                            "recent_responsibility_labels"
                        )
                        or []
                    )
                ]
            try:
                from config.runtime_config import shared as _runtime_config

                health_max_age_seconds = float(
                    getattr(
                        _runtime_config(),
                        "factor_governance_health_max_age_seconds",
                        900.0,
                    )
                    or 900.0
                )
            except Exception:
                health_max_age_seconds = 900.0
            built = [
                self._build_card(
                    name,
                    conn=conn,
                    catalog_item=catalog_by_factor.get(name),
                    runtime_projection=runtime_projection,
                    evidence_counts=card_evidence_by_factor.get(name),
                    health_max_age_seconds=health_max_age_seconds,
                )
                for name in ids
            ]
        items = []
        for card in built:
            if source and card["source"] != source:
                continue
            if lifecycle_status and card["lifecycle_status"] != lifecycle_status:
                continue
            if factor_id and card["factor_id"] != factor_id:
                continue
            if factor_family and card["factor_family"] != factor_family:
                continue
            items.append(card)
        items.sort(
            key=lambda item: (
                -float(item.get("updated_at_ts") or 0.0),
                str(item.get("factor_id") or ""),
            )
        )
        with _CARD_CACHE_LOCK:
            _CARD_CACHE[cache_key] = (now_ts + _CARD_CACHE_TTL_SEC, deepcopy(items))
        return items[:limit]

    @staticmethod
    def _rank_candidate_ids(
        ids: list[str],
        catalog_by_factor: dict[str, dict[str, Any]],
        limit: int,
    ) -> list[str]:
        if limit <= 0 or len(ids) <= limit:
            return ids

        def rank(factor_id: str) -> tuple[int, int, str]:
            item = catalog_by_factor.get(factor_id) or {}
            participates = bool(
                item.get("enabled")
                or item.get("eligible_for_live")
                or item.get("used_in_score")
            )
            lifecycle = str(item.get("lifecycle_status") or "").upper()
            active = participates or lifecycle in {"ACTIVE", "SHADOW", "DISCOVERED"}
            has_catalog = factor_id in catalog_by_factor
            return (
                0 if active else 1,
                0 if has_catalog else 1,
                factor_id,
            )

        return sorted(ids, key=rank)[:limit]

    def _factor_ids(self, conn=None) -> list[str]:
        adapter = RegistryAdapter.shared()
        names = set(factor_registry.list()) | set(adapter._meta.keys())
        if conn is None:
            with self._conn() as owned:
                return self._factor_ids(conn=owned)
        for table, column in (
            ("factor_health", "factor"),
            ("factor_contribution_review", "factor"),
        ):
            rows = _execute(conn, f"SELECT DISTINCT {column} AS value FROM {table}").fetchall()
            names.update(str(row["value"] or "") for row in rows if str(row["value"] or ""))
        # Factor names from canonical risk-decision payloads, using the public
        # reader contract rather than reading event/payload internals here.
        try:
            for decision in iter_decision_rows(conn, limit=500, reverse=True):
                for snapshot in decision.get("factor_snapshots") or []:
                    if not isinstance(snapshot, dict):
                        continue
                    fn = str(snapshot.get("factor") or "")
                    if fn:
                        names.add(fn)
        except Exception:
            pass
        rows = _execute(
            conn,
            """
            SELECT DISTINCT scope_key AS value
            FROM policy_suggestion
            WHERE scope_type='factor'
            """
        ).fetchall()
        names.update(str(row["value"] or "") for row in rows if str(row["value"] or ""))
        try:
            from backend.services.learning_application_store import (
                LearningApplicationStore,
            )

            for app in LearningApplicationStore(self.db_path).iter_applications(
                scope_type="factor"
            ):
                key = str(app.get("scope_key") or "")
                if key:
                    names.add(key)
        except Exception:
            pass
        return sorted(name for name in names if name)

    def _batch_review_evidence(
        self,
        conn,
        factor_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        ids = list(dict.fromkeys(str(item) for item in factor_ids if str(item)))
        result = {
            factor_id: {
                "review_evidence_status": "available",
                "last_primary_responsibility": "",
                "recent_responsibility_labels": [],
                "review_updated_at_ts": 0.0,
            }
            for factor_id in ids
        }
        if not ids:
            return result
        placeholders = ",".join("?" for _ in ids)
        try:
            rows = _execute(
                conn,
                f"""
                SELECT factor, review_id, notes
                FROM factor_contribution_review
                WHERE factor IN ({placeholders})
                ORDER BY id DESC
                """,
                tuple(ids),
            ).fetchall()
            recent_rows: dict[str, list[Any]] = {factor_id: [] for factor_id in ids}
            review_ids_by_factor: dict[str, list[str]] = {
                factor_id: [] for factor_id in ids
            }
            for row in rows:
                factor_id = str(row["factor"] or "")
                if factor_id not in result:
                    continue
                if len(recent_rows[factor_id]) < 12:
                    recent_rows[factor_id].append(row)
                review_id = str(row["review_id"] or "")
                if review_id:
                    review_ids_by_factor[factor_id].append(review_id)

            latest_review_times = latest_review_observed_at_by_id(
                conn,
                [
                    review_id
                    for review_ids in review_ids_by_factor.values()
                    for review_id in review_ids
                ],
            )
            for factor_id in ids:
                labels: list[str] = []
                primary = ""
                for row in recent_rows[factor_id]:
                    notes = str(row["notes"] or "")
                    payload = _loads(notes, {}) if notes.startswith("{") else {}
                    if not isinstance(payload, dict):
                        payload = {}
                    if not primary:
                        primary = str(payload.get("primary_responsibility") or "")
                    for label in payload.get("responsibility_labels") or []:
                        item = str(label or "")
                        if item and item not in labels:
                            labels.append(item)
                updated_at = max(
                    (
                        latest_review_times.get(review_id, 0.0)
                        for review_id in review_ids_by_factor[factor_id]
                    ),
                    default=0.0,
                )
                result[factor_id].update(
                    {
                        "last_primary_responsibility": primary,
                        "recent_responsibility_labels": labels,
                        "review_updated_at_ts": updated_at,
                    }
                )
        except Exception:
            for item in result.values():
                item["review_evidence_status"] = "unavailable"
        return result

    def _build_card(
        self,
        factor_id: str,
        *,
        conn=None,
        catalog_item: dict[str, Any] | None = None,
        runtime_projection: dict[str, Any] | None = None,
        evidence_counts: dict[str, Any] | None = None,
        health_max_age_seconds: float | None = None,
    ) -> dict[str, Any]:
        adapter = RegistryAdapter.shared()
        catalog_item = catalog_item or {}
        meta = adapter.get_meta(factor_id)
        func = factor_registry.get(factor_id)
        description = str(
            meta.get("description")
            or getattr(func, "_factor_desc", "")
            or factor_id
        )
        source = str(catalog_item.get("source") or meta.get("source") or ("builtin" if func else "unknown"))
        lifecycle = str(
            catalog_item.get("lifecycle_status")
            or adapter._lifecycle_statuses.get(factor_id)
            or ("ACTIVE" if func else "UNKNOWN")
        )
        family = self._infer_family(factor_id, description, source)
        defaults = _FAMILY_DEFAULTS.get(family, _FAMILY_DEFAULTS["composite"])
        parameters = self._infer_parameters(factor_id, description, family)
        formula_version = str(meta.get("formula_version") or self._default_formula_version(source, factor_id))
        parameter_version = str(meta.get("parameter_version") or self._default_parameter_version(source))
        evidence = self._evidence_summary(
            factor_id,
            description,
            conn=conn,
            batch_summary=evidence_counts,
        )
        governance = self._governance_state(factor_id, evidence=evidence, conn=conn)
        evidence_counts = self._evidence_counts(
            factor_id,
            evidence=evidence,
            summary=evidence_counts,
        )
        runtime_binding = self._runtime_binding(
            factor_id,
            catalog_item=catalog_item,
            projection=runtime_projection,
        )
        definition_lineage = self._definition_lineage(catalog_item)
        admission_evidence = build_factor_admission_evidence(
            factor_id=factor_id,
            catalog_item={
                **catalog_item,
                "runtime_selection_fingerprint": runtime_binding.get(
                    "selection_fingerprint"
                ),
                "runtime_config_hash": runtime_binding.get("config_hash"),
            },
            evidence_counts=evidence_counts,
            governance=governance,
            health_max_age_seconds=health_max_age_seconds,
        )
        direction_contract = dict(admission_evidence["direction"])
        posterior_summary = self._posterior_summary(
            factor_id,
            governance=governance,
            catalog_item=catalog_item,
        )
        failure_modes = list(evidence.get("recent_responsibility_labels") or [])
        if not failure_modes and evidence.get("last_primary_responsibility") == "parameter":
            failure_modes = ["factor_logic_ok_but_param_suspect"]
        updated_at_ts = max(
            float(meta.get("register_time") or 0.0),
            float(evidence.get("updated_at_ts") or 0.0),
            float(governance.get("updated_at_ts") or 0.0),
        )
        return {
            "schema_version": "factor_card.v1",
            "factor_id": factor_id,
            "display_name": description,
            "factor_family": family,
            "source": source,
            "role": str(catalog_item.get("role") or ""),
            "enabled": bool(catalog_item.get("enabled", True)),
            "eligible_for_live": bool(catalog_item.get("eligible_for_live", False)),
            "used_in_score": bool(catalog_item.get("used_in_score", False)),
            "weight": _round(catalog_item.get("weight", 0.0)),
            "lifecycle_status": lifecycle,
            "formula_version": formula_version,
            "parameter_version": parameter_version,
            "parameters": parameters,
            "expected_regimes": list(defaults["expected_regimes"]),
            "weak_regimes": list(defaults["weak_regimes"]),
            "expected_holding_profile": dict(defaults["expected_holding_profile"]),
            "failure_modes": failure_modes,
            "governance_state": {
                "weight_state": governance["weight_state"],
                "template_state": governance["template_state"],
                "review_status": governance["review_status"],
                "latest_suggestion_action": governance["latest_suggestion_action"],
                "latest_application_action": governance["latest_application_action"],
                "application_effect_status": governance["application_effect_status"],
                "autonomy_status": str(catalog_item.get("governance_status") or governance["review_status"]),
                "autonomy_action": str(catalog_item.get("governance_action") or governance["latest_suggestion_action"]),
                "rollback_state": str(catalog_item.get("rollback_state") or ""),
                "latest_catalog_snapshot_id": str(catalog_item.get("latest_catalog_snapshot_id") or ""),
                "latest_catalog_snapshot_run_id": str(catalog_item.get("latest_catalog_snapshot_run_id") or ""),
                "latest_template_candidate_trace": governance["latest_template_candidate_trace"],
                "latest_template_recommendation": governance["latest_template_recommendation"],
            },
            "runtime_binding": runtime_binding,
            "definition_lineage": definition_lineage,
            "evidence_counts": evidence_counts,
            "direction_contract": direction_contract,
            "admission_evidence": admission_evidence,
            "eligible_for_activation": bool(
                admission_evidence["eligible_for_activation"]
            ),
            "eligible_for_weight_expansion": bool(
                admission_evidence["eligible_for_weight_expansion"]
            ),
            "blocker_codes": list(admission_evidence["blocker_codes"]),
            "posterior_summary": posterior_summary,
            "evidence_summary": {
                "description": description,
                "health_score": evidence["health_score"],
                "shadow_score": evidence["shadow_score"],
                "factor_governance_shadow": dict(catalog_item.get("factor_governance_shadow") or {}),
                "model_weakness_score": _round(catalog_item.get("model_weakness_score", 0.0)),
                "model_positive_score": _round(catalog_item.get("model_positive_score", 0.0)),
                "avg_contribution_score": evidence["avg_contribution_score"],
                "last_primary_responsibility": evidence["last_primary_responsibility"],
                "recent_responsibility_labels": evidence["recent_responsibility_labels"],
                "sample_count": evidence["sample_count"],
                "factor_linked_trade_reviews": evidence_counts["factor_linked_trade_reviews"],
                "governance_eligible_mature": evidence_counts["governance_eligible_mature"],
                "contaminated_or_ineligible": evidence_counts["contaminated_or_ineligible"],
                "effects_observed": evidence_counts["effects_observed"],
            },
            "updated_at": governance["updated_at"] or evidence["updated_at"] or None,
            "updated_at_ts": updated_at_ts,
        }

    @staticmethod
    def _runtime_binding(
        factor_id: str,
        *,
        catalog_item: dict[str, Any],
        projection: dict[str, Any] | None,
    ) -> dict[str, Any]:
        projection = dict(projection or {})
        selected = {
            str(item)
            for item in list(projection.get("selected_factor_ids") or [])
            if str(item)
        }
        projection_status = str(projection.get("status") or "unknown").lower()
        if bool(projection.get("ok")):
            status = "bound" if factor_id in selected else "unavailable"
        elif projection_status in {"stale", "unavailable"}:
            status = projection_status
        elif projection_status == "missing":
            status = "unavailable"
        else:
            status = "unknown"
        roles = projection.get("selected_factor_roles") or {}
        weights = projection.get("selected_factor_weights") or {}
        is_bound = bool(projection.get("ok")) and factor_id in selected
        role = str(roles.get(factor_id) or "") if is_bound else None
        raw_weight = weights.get(factor_id) if is_bound else None
        try:
            weight = round(float(raw_weight), 8) if raw_weight is not None else None
        except (TypeError, ValueError):
            weight = None
        return {
            "status": status,
            "selection_fingerprint": projection.get("selection_fingerprint") or None,
            "config_version": projection.get("config_version"),
            "config_hash": projection.get("config_hash") or None,
            "live_generation_id": projection.get("live_generation_id") or None,
            "selection_source": (
                catalog_item.get("runtime_selection_source")
                or projection.get("source")
                or None
            ),
            "role": role,
            "weight": weight,
        }

    @staticmethod
    def _definition_lineage(catalog_item: dict[str, Any]) -> dict[str, Any]:
        return {
            "generation": catalog_item.get("lifecycle_generation") or None,
            "definition_fingerprint": catalog_item.get("lifecycle_definition_fingerprint") or None,
            "artifact_hash": catalog_item.get("lifecycle_artifact_hash") or None,
            "mutation_id": catalog_item.get("governance_mutation_id")
            or catalog_item.get("lifecycle_mutation_id")
            or None,
            "catalog_snapshot_id": catalog_item.get("latest_catalog_snapshot_id") or None,
        }

    @staticmethod
    def _posterior_summary(
        factor_id: str,
        *,
        governance: dict[str, Any],
        catalog_item: dict[str, Any],
    ) -> dict[str, Any]:
        effect_status = str(governance.get("application_effect_status") or "").lower()
        if effect_status in {"effective", "reinforced", "validated_effective"}:
            state = "probable"
        elif effect_status in {"mixed", "ineffective", "inconclusive"}:
            state = "inconclusive"
        else:
            state = "unobservable"
        action = "rollback" if effect_status == "rolled_back" else "no_change"
        refs: list[str] = []
        mutation_id = str(
            catalog_item.get("governance_mutation_id")
            or catalog_item.get("lifecycle_mutation_id")
            or ""
        )
        if mutation_id:
            refs.append(f"mutation:{mutation_id}")
        snapshot_id = str(catalog_item.get("latest_catalog_snapshot_id") or "")
        if snapshot_id:
            refs.append(f"catalog_snapshot:{snapshot_id}")
        return {
            "state": state,
            "action": action,
            "confidence": None,
            "evidence_refs": refs,
            "candidate_id": None,
            "review_id": None,
            "reason": f"factor={factor_id}; effect_status={effect_status or 'missing'}",
        }

    def _evidence_counts(
        self,
        factor_id: str,
        *,
        evidence: dict[str, Any],
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if isinstance(summary, dict):
            return {
                "decision_observations": summary.get(
                    "decision_observations",
                    int(evidence.get("sample_count") or 0),
                ),
                "factor_linked_trade_reviews": summary.get("factor_linked_trade_reviews"),
                "governance_eligible_mature": summary.get("governance_eligible_mature"),
                "contaminated_or_ineligible": summary.get("contaminated_or_ineligible"),
                "effects_observed": summary.get("effects_observed"),
                "status": str(summary.get("status") or "unknown"),
            }
        return {
            "decision_observations": int(evidence.get("sample_count") or 0),
            "factor_linked_trade_reviews": None,
            "governance_eligible_mature": None,
            "contaminated_or_ineligible": None,
            "effects_observed": None,
            "status": "unavailable",
        }

    def _evidence_summary(
        self,
        factor_id: str,
        description: str,
        *,
        conn=None,
        batch_summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if conn is None:
            with self._conn() as owned:
                return self._evidence_summary(
                    factor_id,
                    description,
                    conn=owned,
                    batch_summary=batch_summary,
                )
        health_row = _execute(
            conn,
            """
            SELECT score, updated_at
            FROM factor_health
            WHERE factor=?
            """,
            (factor_id,),
        ).fetchone()
        batch = batch_summary if isinstance(batch_summary, dict) else {}
        batch_available = (
            str(batch.get("status") or "") == "available"
            and str(batch.get("review_evidence_status") or "available")
            == "available"
            and "decision_observations" in batch
            and "shadow_score" in batch
            and "avg_contribution_score" in batch
        )
        if batch_available:
            snapshot_row = {
                "avg_shadow_score": float(batch.get("shadow_score") or 0.0),
                "avg_contribution_score": float(
                    batch.get("avg_contribution_score") or 0.0
                ),
                "sample_count": int(
                    batch.get("decision_observations") or 0
                ),
            }
        else:
            snapshot_rows = iter_decision_factor_snapshots_by_factor(
                conn,
                factor_id,
                limit=_EVIDENCE_SNAPSHOT_LIMIT,
            )
            if snapshot_rows:
                shadow_scores = [float(r.get("shadow_score") or 0) for r in snapshot_rows]
                contrib_scores = [float(r.get("contribution_score") or 0) for r in snapshot_rows]
                snapshot_row = {
                    "avg_shadow_score": sum(shadow_scores) / len(shadow_scores),
                    "avg_contribution_score": sum(contrib_scores) / len(contrib_scores),
                    "sample_count": len(snapshot_rows),
                }
            else:
                snapshot_row = None
        if batch_available:
            updated_at = float(batch.get("review_updated_at_ts") or 0.0)
            labels = [
                str(item or "")
                for item in list(batch.get("recent_responsibility_labels") or [])
                if str(item or "")
            ]
            last_primary = str(
                batch.get("last_primary_responsibility") or ""
            )
        else:
            review_rows = _execute(
                conn,
                """
                SELECT notes
                FROM factor_contribution_review
                WHERE factor=?
                ORDER BY id DESC
                LIMIT 12
                """,
                (factor_id,),
            ).fetchall()
            review_ids = [
                str(row["review_id"] or "")
                for row in _execute(
                    conn,
                    "SELECT review_id FROM factor_contribution_review WHERE factor=?",
                    (factor_id,),
                ).fetchall()
                if str(row["review_id"] or "")
            ]
            updated_at = 0.0
            for review_id in review_ids:
                review = review_row(conn, review_id)
                if review is not None:
                    updated_at = max(updated_at, float(review.get("created_at") or 0.0))
            labels = []
            last_primary = ""
            for row in review_rows:
                payload = _loads(row["notes"], {}) if str(row["notes"] or "").startswith("{") else {}
                if not isinstance(payload, dict):
                    payload = {}
                if not last_primary:
                    last_primary = str(payload.get("primary_responsibility") or "")
                for label in payload.get("responsibility_labels") or []:
                    item = str(label or "")
                    if item and item not in labels:
                        labels.append(item)
        updated_at_ts = max(
            float((health_row["updated_at"] if health_row else 0.0) or 0.0),
            float(updated_at or 0.0),
        )
        return {
            "description": description,
            "health_score": _round(health_row["score"] if health_row else 0.0),
            "shadow_score": _round(snapshot_row["avg_shadow_score"] if snapshot_row else 0.0),
            "avg_contribution_score": _round(snapshot_row["avg_contribution_score"] if snapshot_row else 0.0),
            "last_primary_responsibility": last_primary,
            "recent_responsibility_labels": labels,
            "sample_count": int((snapshot_row["sample_count"] if snapshot_row else 0) or 0),
            "updated_at_ts": updated_at_ts,
            "updated_at": self._format_ts(updated_at_ts),
        }

    def _governance_state(self, factor_id: str, *, evidence: dict[str, Any] | None = None, conn=None) -> dict[str, Any]:
        if conn is None:
            with self._conn() as owned:
                return self._governance_state(factor_id, evidence=evidence, conn=owned)
        suggestion = _execute(
            conn,
            """
            SELECT action, status, created_at
            FROM policy_suggestion
            WHERE scope_type='factor' AND scope_key=?
            ORDER BY created_at DESC
            LIMIT 1
            """,
            (factor_id,),
        ).fetchone()
        from backend.services.learning_application_store import (
            LearningApplicationStore,
        )

        store = LearningApplicationStore(self.db_path)
        app = store.latest_application(scope_type="factor", scope_key=factor_id)
        if app is not None:
            effect = next(
                (
                    e
                    for e in store.iter_effects(scope_key=factor_id)
                    if str(e.get("application_id") or "") == str(app["application_id"] or "")
                ),
                None,
            )
        else:
            effect = store.latest_effect(scope_key=factor_id, scope_type="factor")
        active_template = _execute(
            conn,
            """
            SELECT template_id, template_version, regime_key, updated_at
            FROM parameter_template_active
            WHERE factor_id=?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (factor_id,),
        ).fetchone()
        latest_candidate = _execute(
            conn,
            """
            SELECT candidate_id, status, updated_at, validation_summary_json
            FROM parameter_template_release_candidate
            WHERE factor_id=?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """,
            (factor_id,),
        ).fetchone()
        weight_state = "active"
        app_action = str(app["action"] or "") if app else ""
        effect_status = str(effect["status"] or "") if effect else ""
        if effect_status == "rolled_back":
            weight_state = "rolled_back"
        elif app_action == "downweight":
            weight_state = "downweighted"
        elif app_action == "upweight":
            weight_state = "upweighted"
        review_status = str(
            (suggestion["status"] if suggestion else "")
            or effect_status
            or "none"
        )
        template_state = "default_only"
        if active_template:
            template_version = str(active_template["template_version"] or "")
            regime_key = str(active_template["regime_key"] or "")
            if regime_key or template_version not in {"", "default.v1"}:
                template_state = "active_variant"
        if latest_candidate:
            candidate_status = str(latest_candidate["status"] or "")
            candidate_state_map = {
                "pending_review": "review_pending",
                "approved": "review_approved",
                "rejected": "review_rejected",
                "deployed": "deployed",
                "rolled_back": "rolled_back",
            }
            template_state = candidate_state_map.get(candidate_status, template_state)
        latest_candidate_trace = {}
        if latest_candidate:
            summary = _loads(latest_candidate["validation_summary_json"], {})
            trace = _loads((summary or {}).get("recommendation_source"), {})
            if isinstance(trace, dict) and trace:
                latest_candidate_trace = {
                    "source": str(trace.get("source") or ""),
                    "recommendation_id": str(trace.get("recommendation_id") or ""),
                    "reason": str(trace.get("reason") or ""),
                    "primary_responsibility": str(
                        ((trace.get("responsibility") or {}).get("primary_responsibility") or "")
                    ),
                    "responsibility_labels": list(
                        ((trace.get("responsibility") or {}).get("responsibility_labels") or [])
                    ),
                    "approval_path": str(trace.get("approval_path") or ""),
                }
        latest_recommendation = self._local_template_recommendation_summary(
            factor_id=factor_id,
            evidence=evidence or {},
            latest_candidate_trace=latest_candidate_trace,
        )
        updated_at_ts = max(
            float((suggestion["created_at"] if suggestion else 0.0) or 0.0),
            float((app["created_at"] if app else 0.0) or 0.0),
            float((effect["updated_at"] if effect else 0.0) or 0.0),
            float((active_template["updated_at"] if active_template else 0.0) or 0.0),
            float((latest_candidate["updated_at"] if latest_candidate else 0.0) or 0.0),
        )
        return {
            "weight_state": weight_state,
            "template_state": template_state,
            "review_status": review_status,
            "latest_suggestion_action": str(suggestion["action"] or "") if suggestion else "",
            "latest_application_action": app_action,
            "latest_application_id": (
                str(app["application_id"] or "") if app else ""
            ),
            "latest_application_status": (
                str(app["status"] or "") if app else ""
            ),
            "latest_application_old_weight": (
                float(app["old_weight"] or 0.0) if app else 0.0
            ),
            "latest_application_new_weight": (
                float(app["new_weight"] or 0.0) if app else 0.0
            ),
            "application_effect_status": effect_status,
            "application_effect_trade_count": (
                int(effect["observed_trade_count"] or 0) if effect else 0
            ),
            "application_effect_delta": (
                float(effect["delta_avg_reward"] or 0.0) if effect else None
            ),
            "application_effect_decision": (
                dict(effect.get("decision") or {}) if effect else {}
            ),
            "latest_template_candidate_trace": latest_candidate_trace,
            "latest_template_recommendation": latest_recommendation,
            "updated_at_ts": updated_at_ts,
            "updated_at": self._format_ts(updated_at_ts),
        }

    @staticmethod
    def _local_template_recommendation_summary(
        *,
        factor_id: str,
        evidence: dict[str, Any],
        latest_candidate_trace: dict[str, Any],
    ) -> dict[str, Any]:
        primary = str(evidence.get("last_primary_responsibility") or "")
        labels = [str(item or "") for item in (evidence.get("recent_responsibility_labels") or []) if str(item or "")]
        if primary != "parameter" and "factor_logic_ok_but_param_suspect" not in labels:
            return {
                "recommendation_id": "",
                "target_template_id": "",
                "target_template_version": "",
                "template_role": "",
                "recommended_action": "",
                "reason": "",
            }
        if latest_candidate_trace:
            recommendation_id = str(latest_candidate_trace.get("recommendation_id") or "")
            reason = str(latest_candidate_trace.get("reason") or "")
            recommended_action = (
                "offline_validate"
                if str(latest_candidate_trace.get("approval_path") or "") == "offline_validation_then_gray_release"
                else "suggest_switch"
            )
            template_role = "conservative"
            if "holding_too_long" in labels:
                template_role = "aggressive"
            elif "holding_inefficient" in labels:
                template_role = "conservative"
            return {
                "recommendation_id": recommendation_id,
                "target_template_id": "",
                "target_template_version": "",
                "template_role": template_role,
                "recommended_action": recommended_action,
                "reason": reason,
            }
        template_role = "conservative"
        if "holding_too_long" in labels:
            template_role = "aggressive"
        reason = "factor logic looks usable but current parameters appear mismatched"
        return {
            "recommendation_id": f"ptr_{factor_id}_local",
            "target_template_id": "",
            "target_template_version": "",
            "template_role": template_role,
            "recommended_action": "suggest_switch",
            "reason": reason,
        }

    @staticmethod
    def _default_formula_version(source: str, factor_id: str) -> str:
        if source == "builtin":
            return "registry_builtin.v1"
        if source in {"discovered", "shadow"}:
            return "registry_runtime.v1"
        if factor_id.startswith("ml_"):
            return "ml.runtime.v1"
        return "registry_unknown.v1"

    @staticmethod
    def _default_parameter_version(source: str) -> str:
        if source == "shadow":
            return "shadow.v1"
        return "default.v1"

    @staticmethod
    def _infer_family(factor_id: str, description: str, source: str) -> str:
        name = factor_id.lower()
        desc = description.lower()
        if source != "builtin" and name.startswith("ml_"):
            return "ml_signal"
        if any(token in name for token in ("rsi", "stoch")):
            return "momentum_oscillator"
        if any(token in name for token in ("macd", "adx", "ema", "supertrend", "di_spread")):
            return "trend"
        if any(token in name for token in ("atr", "bb_", "keltner", "width")):
            return "volatility"
        if any(token in name for token in ("obv", "vol_", "volume")):
            return "volume"
        if any(token in name for token in ("engulfing", "pin_bar", "inside_bar")):
            return "pattern"
        if any(token in name for token in ("fomc", "nfp", "hour_", "day_of_week")):
            return "calendar"
        if any(token in name for token in ("dxy", "slv_", "gld_", "ratio")):
            return "cross_asset"
        if any(token in name for token in ("yield", "cot_", "cb_")):
            return "macro"
        if "ml" in name or "model" in desc:
            return "ml_signal"
        return "composite" if source in {"discovered", "shadow"} else "momentum"

    @staticmethod
    def _infer_parameters(factor_id: str, description: str, family: str) -> dict[str, Any]:
        exact_length = re.fullmatch(r"(?:rsi|adx|atr)_(\d+)", factor_id)
        if exact_length:
            return {"length": int(exact_length.group(1))}
        text = f"{factor_id} {description}"
        numbers = [float(item) for item in re.findall(r"\d+(?:\.\d+)?", text)]
        if factor_id == "macd_hist" and len(numbers) >= 3:
            return {
                "fast_length": int(numbers[0]),
                "slow_length": int(numbers[1]),
                "signal_length": int(numbers[2]),
            }
        if factor_id == "stoch_k" and len(numbers) >= 1:
            item = {"k_length": int(numbers[0])}
            if len(numbers) >= 2:
                item["smooth_k"] = int(numbers[1])
            if len(numbers) >= 3:
                item["smooth_d"] = int(numbers[2])
            return item
        if factor_id == "ema_slope" and len(numbers) >= 2:
            return {"period": int(numbers[0]), "lookback": int(numbers[1])}
        if factor_id == "obv_slope" and len(numbers) >= 1:
            return {"lookback": int(numbers[0])}
        if factor_id == "vol_ma_ratio" and len(numbers) >= 1:
            return {"period": int(numbers[0])}
        if factor_id == "supertrend_str" and len(numbers) >= 2:
            return {"atr_length": int(numbers[0]), "multiplier": numbers[1]}
        if factor_id == "bb_width" and len(numbers) >= 2:
            return {"length": int(numbers[0]), "stddev": numbers[1]}
        if factor_id == "keltner_width" and len(numbers) >= 2:
            return {"ema_length": int(numbers[0]), "atr_multiplier": numbers[1]}
        if len(numbers) == 1:
            key = "length" if family != "calendar" else "window"
            value = numbers[0]
            return {key: int(value) if value.is_integer() else value}
        if numbers:
            return {
                "args": [int(value) if value.is_integer() else value for value in numbers]
            }
        return {}

    @staticmethod
    def _format_ts(ts: float) -> str:
        if ts <= 0:
            return ""
        try:
            from datetime import datetime, timezone

            return (
                datetime.fromtimestamp(ts, tz=timezone.utc)
                .isoformat()
                .replace("+00:00", "Z")
            )
        except Exception:
            return ""
