"""Build bounded factor priors from terminal, comparable application effects."""
from __future__ import annotations

import json
import math
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.learning_application_store import LearningApplicationStore


_CACHE_LOCK = threading.Lock()
_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(str(raw or "{}"))
    except Exception:
        return {}


class ExperiencePriorService:
    """Read-only adapter from the effect ledger to ``DecisionPolicy`` priors."""

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = Path(db_path)

    def _conn(self):
        conn = get_state_pg_conn(read_only=True) if is_state_db_path(self.db_path) else connect_sqlite(self.db_path, read_only=True)
        if not is_state_db_path(self.db_path):
            conn.row_factory = __import__("sqlite3").Row
        return conn

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "experience_prior_boundary.v1",
            "read_only": True,
            "terminal_bounded_effects_only": True,
            "multiplier_floor": 0.85,
            "multiplier_ceiling": 1.15,
            "does_not_apply_runtime_mutation": True,
            "decision_policy_remains_authority": True,
        }

    def build(self, *, max_age_days: int = 90, cache_seconds: float = 300.0) -> dict[str, Any]:
        cache_key = str(self.db_path)
        now = time.time()
        with _CACHE_LOCK:
            cached = _CACHE.get(cache_key)
            if cached and now - cached[0] <= max(0.0, float(cache_seconds)):
                return dict(cached[1])
        conn = self._conn()
        try:
            if not state_table_exists(conn, "learning_application_effect"):
                result = self._empty("missing_effect_ledger")
            else:
                cutoff = now - max(1, int(max_age_days)) * 86400.0
                terminal_statuses = {"reinforced", "effective", "ineffective", "rolled_back"}
                store = LearningApplicationStore(self.db_path)
                grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
                rejected = 0
                for eff in store.iter_effects(scope_type="factor"):
                    if str(eff.get("status") or "") not in terminal_statuses:
                        continue
                    if float(eff.get("updated_at") or 0.0) < cutoff:
                        continue
                    decision = eff.get("decision") or {}
                    quality = decision.get("evidence_quality") if isinstance(decision.get("evidence_quality"), dict) else {}
                    if not bool(quality.get("bounded_attribution_allowed")):
                        rejected += 1
                        continue
                    factor = str(eff.get("scope_key") or "")
                    if not factor:
                        rejected += 1
                        continue
                    grouped[factor].append({
                        "application_id": str(eff.get("application_id") or ""),
                        "status": str(eff.get("status") or ""),
                        "sample_count": max(0, int(eff.get("observed_trade_count") or 0)),
                        "delta": float(eff.get("delta_avg_reward") or 0.0),
                        "updated_at": float(eff.get("updated_at") or 0.0),
                        "regime": str(quality.get("target_regime") or ""),
                    })
                priors: dict[str, dict[str, Any]] = {}
                for factor, effects in grouped.items():
                    sample_count = sum(item["sample_count"] for item in effects)
                    if sample_count <= 0:
                        continue
                    weighted_delta = sum(item["delta"] * max(1, item["sample_count"]) for item in effects) / sum(
                        max(1, item["sample_count"]) for item in effects
                    )
                    latest_at = max(item["updated_at"] for item in effects)
                    age_days = max(0.0, (now - latest_at) / 86400.0)
                    freshness = math.exp(-math.log(2.0) * age_days / 30.0)
                    confidence = min(0.95, (0.5 + min(sample_count, 20) / 40.0 + min(len(effects), 3) * 0.05) * freshness)
                    multiplier = max(0.85, min(1.15, 1.0 + 0.5 * weighted_delta))
                    priors[factor] = {
                        "schema_version": "experience_prior.v1",
                        "factor": factor,
                        "sample_count": sample_count,
                        "effect_count": len(effects),
                        "confidence": round(confidence, 6),
                        "multiplier": round(multiplier, 6),
                        "weighted_delta_avg_reward": round(weighted_delta, 6),
                        "bounded_attribution_allowed": True,
                        "latest_effect_at": latest_at,
                        "age_days": round(age_days, 3),
                        "regimes": sorted({item["regime"] for item in effects if item["regime"]}),
                        "source_application_ids": [item["application_id"] for item in effects[:10]],
                    }
                eligible = {
                    factor: prior
                    for factor, prior in priors.items()
                    if int(prior["sample_count"]) >= 5 and float(prior["confidence"]) >= 0.6
                }
                result = {
                    "ok": True,
                    "schema_version": "experience_prior_set.v1",
                    "status": "available" if eligible else "insufficient_bounded_effects",
                    "priors": eligible,
                    "eligible_count": len(eligible),
                    "bounded_factor_count": len(priors),
                    "rejected_unbounded_count": rejected,
                    "generated_at": now,
                    "boundary": self.boundary(),
                }
        finally:
            conn.close()
        with _CACHE_LOCK:
            _CACHE[cache_key] = (now, result)
        return dict(result)

    def priors(self) -> dict[str, dict[str, Any]]:
        return dict(self.build().get("priors") or {})

    def _empty(self, status: str) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "experience_prior_set.v1",
            "status": status,
            "priors": {},
            "eligible_count": 0,
            "bounded_factor_count": 0,
            "rejected_unbounded_count": 0,
            "generated_at": time.time(),
            "boundary": self.boundary(),
        }
