from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any, Mapping, Sequence

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists

from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    pg_sql as _sql,
    execute as _execute,
)



DEFAULT_TARGET_MAX_ACTIVE_ALPHA = 80
DEFAULT_WARN_MAX_ACTIVE_ALPHA = 120
DEFAULT_WARN_MAX_NOISE_FAMILY_COUNT = 40
DEFAULT_LOW_WEIGHT_THRESHOLD = 0.02
DIRECTIONAL_PORTFOLIO_MIN_VOTERS = 3
DIRECTIONAL_PORTFOLIO_MIN_INDEPENDENT_GROUPS = 2
_TERMINAL_LIFECYCLE_STAGES = {
    "DEAD",
    "SHADOW",
    "PROMOTION_PREPARED",
    "QUARANTINE",
    "QUARANTINED",
    "RETIRED",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _family(name: str) -> str:
    lower = str(name or "").lower()
    if lower.startswith("dsl_auto_") or lower.startswith("dsl_"):
        return "dsl_auto"
    if lower.startswith("pca_"):
        return "pca"
    return "core"


class FactorBlendHealthService:
    """Read-only health check for the live factor blend.

    It explains whether the current factor portfolio is too broad, too noisy,
    or too concentrated. It never mutates weights, config, suggestions, or
    broker state.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "read_only": True,
            "affects_trading": False,
            "does_not_mutate_runtime_config": True,
            "does_not_apply_pruning": True,
            "weight_writes_remain_decision_policy": True,
        }

    @staticmethod
    def evaluate_directional_portfolio_guard(
        *,
        selected_factor_ids: Sequence[str] | None,
        factor_configs: Mapping[str, Any],
        weights: Mapping[str, Any],
        enforced: bool = True,
    ) -> dict[str, Any]:
        """Evaluate the one canonical directional-portfolio sufficiency fact."""

        if selected_factor_ids is None:
            return {
                "schema_version": "directional_portfolio_guard.v1",
                "status": "unavailable",
                "enforced": bool(enforced),
                "min_voters": DIRECTIONAL_PORTFOLIO_MIN_VOTERS,
                "min_independent_groups": DIRECTIONAL_PORTFOLIO_MIN_INDEPENDENT_GROUPS,
                "voter_count": 0,
                "independent_group_count": 0,
                "voter_ids": [],
                "independent_group_keys": [],
                "reason_codes": ["directional_portfolio_evidence_unavailable"],
            }

        from alpha.portfolio_compositor import resolve_factor_role
        from alpha.runtime_factor_selection import runtime_factor_enabled

        voter_ids: list[str] = []
        group_keys: set[str] = set()
        for raw_name in selected_factor_ids:
            name = str(raw_name or "")
            if not name:
                continue
            raw_entry = factor_configs.get(name, {})
            entry = raw_entry if isinstance(raw_entry, dict) else {}
            if resolve_factor_role(name, entry) != "alpha":
                continue
            if not runtime_factor_enabled(entry):
                continue
            lifecycle = str(entry.get("lifecycle_status") or "ACTIVE").upper()
            if lifecycle in _TERMINAL_LIFECYCLE_STAGES:
                continue
            raw_weight = weights.get(name, entry.get("weight", 0.0))
            weight = _safe_float(
                raw_weight.get("weight") if isinstance(raw_weight, dict) else raw_weight,
                0.0,
            )
            if weight <= 0.0:
                continue
            voter_ids.append(name)
            redundancy_group = str(entry.get("redundancy_group") or "").strip()
            group_keys.add(redundancy_group or f"factor:{name}")

        voter_ids = sorted(set(voter_ids))
        independent_groups = sorted(group_keys)
        reasons: list[str] = []
        if len(voter_ids) < DIRECTIONAL_PORTFOLIO_MIN_VOTERS:
            reasons.append("insufficient_directional_alpha_voters")
        if len(independent_groups) < DIRECTIONAL_PORTFOLIO_MIN_INDEPENDENT_GROUPS:
            reasons.append("insufficient_directional_alpha_groups")
        return {
            "schema_version": "directional_portfolio_guard.v1",
            "status": "healthy" if not reasons else "degraded",
            "enforced": bool(enforced),
            "min_voters": DIRECTIONAL_PORTFOLIO_MIN_VOTERS,
            "min_independent_groups": DIRECTIONAL_PORTFOLIO_MIN_INDEPENDENT_GROUPS,
            "voter_count": len(voter_ids),
            "independent_group_count": len(independent_groups),
            "voter_ids": voter_ids,
            "independent_group_keys": independent_groups,
            "reason_codes": reasons,
        }

    @staticmethod
    def guard_allows_transition(
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> bool:
        """Allow a healthy result, or a non-worsening change during recovery."""

        if str(after.get("status") or "") == "unavailable":
            return False
        if str(before.get("status") or "") == "healthy":
            return str(after.get("status") or "") == "healthy"
        return bool(
            int(after.get("voter_count") or 0) >= int(before.get("voter_count") or 0)
            and int(after.get("independent_group_count") or 0)
            >= int(before.get("independent_group_count") or 0)
        )

    def build(self, cfg: Any | None = None, *, use_catalog: bool | None = None) -> dict[str, Any]:
        cfg_was_none = cfg is None
        if cfg is None:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
        if use_catalog is None:
            use_catalog = cfg_was_none
        if use_catalog:
            active = self._active_from_factor_catalog(cfg)
            if active is not None:
                return self._build_from_active(cfg, active, active_count_source="factor_catalog.used_in_score")
        return self._build_from_runtime_config(cfg, active_count_source="runtime_config")

    def build_current(self, cfg: Any | None = None) -> dict[str, Any]:
        if cfg is None:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
        active = self._active_from_factor_catalog(cfg)
        if active is not None:
            return self._build_from_active(cfg, active, active_count_source="factor_catalog.used_in_score")
        return self._build_from_runtime_config(cfg, active_count_source="runtime_config_fallback")

    def _active_from_factor_catalog(self, cfg: Any) -> list[dict[str, Any]] | None:
        try:
            from backend.services.factor_catalog import build_factor_catalog
        except Exception:
            return None
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        active: list[dict[str, Any]] = []
        try:
            catalog = build_factor_catalog(self.db_path)
        except Exception:
            return None
        unavailable_reasons = {
            "factor_admission_unavailable",
            "registry_metadata_unavailable",
        }
        if any(
            str(item.get("reason_excluded") or "") in unavailable_reasons
            for item in catalog
        ):
            return None
        for item in catalog:
            if not bool(item.get("used_in_score")):
                continue
            name = str(item.get("factor_id") or "")
            if not name:
                continue
            entry = signal_cfg.get(name, {})
            if not isinstance(entry, dict):
                entry = {}
            active.append(
                {
                    "factor": name,
                    "weight": _safe_float(item.get("weight")),
                    "abs_weight": abs(_safe_float(item.get("weight"))),
                    "family": _family(name),
                    "tags": [str(tag) for tag in (entry.get("tags") or [])],
                    "redundancy_group": str(item.get("redundancy_group") or entry.get("redundancy_group") or ""),
                    "source": str(item.get("source") or entry.get("source") or ""),
                }
            )
        return active

    def _build_from_runtime_config(self, cfg: Any, *, active_count_source: str) -> dict[str, Any]:
        from alpha.portfolio_compositor import resolve_factor_role

        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        active: list[dict[str, Any]] = []
        configured_alpha = 0
        for name in sorted(set(signal_cfg) | set(weights)):
            entry = signal_cfg.get(name, {})
            if not isinstance(entry, dict):
                entry = {}
            role = resolve_factor_role(name, entry)
            if role == "alpha":
                configured_alpha += 1
            weight = _safe_float(weights.get(name, entry.get("weight", 0.0)), 0.0)
            if role != "alpha" or not bool(entry.get("enabled", True)) or weight <= 0.0:
                continue
            tags = [str(tag) for tag in (entry.get("tags") or [])]
            active.append(
                {
                    "factor": name,
                    "weight": weight,
                    "abs_weight": abs(weight),
                    "family": _family(name),
                    "tags": tags,
                    "redundancy_group": str(entry.get("redundancy_group") or ""),
                    "source": str(entry.get("source") or ""),
                }
            )
        return self._build_from_active(
            cfg,
            active,
            active_count_source=active_count_source,
            configured_alpha_count=configured_alpha,
            selection_authoritative=False,
        )

    def _build_from_active(
        self,
        cfg: Any,
        active: list[dict[str, Any]],
        *,
        active_count_source: str,
        configured_alpha_count: int | None = None,
        selection_authoritative: bool = True,
    ) -> dict[str, Any]:
        if configured_alpha_count is None:
            from alpha.portfolio_compositor import resolve_factor_role

            signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
            configured_alpha_count = sum(
                1
                for name, entry in signal_cfg.items()
                if resolve_factor_role(name, entry if isinstance(entry, dict) else {}) == "alpha"
            )
        total_abs_weight = sum(item["abs_weight"] for item in active)
        family_stats = self._group_stats(active, total_abs_weight, key="family")
        tag_stats = self._tag_stats(active, total_abs_weight)
        redundancy_stats = self._redundancy_stats(active, total_abs_weight)
        low_weight = [item for item in active if item["abs_weight"] <= DEFAULT_LOW_WEIGHT_THRESHOLD]
        health_stats = self._health_stats({item["factor"] for item in active})
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        configured_weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        guard_weights = {
            **configured_weights,
            **{str(item["factor"]): float(item["weight"]) for item in active},
        }
        directional_guard = self.evaluate_directional_portfolio_guard(
            selected_factor_ids=(
                [str(item["factor"]) for item in active]
                if selection_authoritative
                else None
            ),
            factor_configs=signal_cfg,
            weights=guard_weights,
        )
        issues = self._issues(
            active=active,
            family_stats=family_stats,
            tag_stats=tag_stats,
            redundancy_stats=redundancy_stats,
            low_weight=low_weight,
            health_stats=health_stats,
            cfg=cfg,
        )
        if directional_guard["status"] != "healthy":
            issues.extend(
                {
                    "severity": "error",
                    "code": reason,
                    "value": directional_guard["voter_count"],
                }
                for reason in directional_guard["reason_codes"]
            )
        status = "ok"
        if directional_guard["status"] != "healthy":
            status = "critical"
        elif any(item.get("severity") == "error" for item in issues):
            status = "degraded"
        elif issues:
            status = "watch"
        return {
            "ok": directional_guard["status"] == "healthy",
            "schema_version": "factor_blend_health.v1",
            "status": status,
            "configured_alpha_count": configured_alpha_count,
            "active_alpha_count": len(active),
            "active_count_source": active_count_source,
            "directional_portfolio_guard": directional_guard,
            "target_max_active_alpha": DEFAULT_TARGET_MAX_ACTIVE_ALPHA,
            "warn_max_active_alpha": DEFAULT_WARN_MAX_ACTIVE_ALPHA,
            "total_abs_weight": round(total_abs_weight, 6),
            "family_exposure": family_stats,
            "tag_exposure_top": tag_stats[:10],
            "redundancy_group_exposure_top": redundancy_stats[:10],
            "low_weight_alpha_count": len(low_weight),
            "low_weight_alpha_sample": [item["factor"] for item in low_weight[:25]],
            "noise_family_counts": {
                "dsl_auto": int(family_stats.get("dsl_auto", {}).get("count", 0)),
                "pca": int(family_stats.get("pca", {}).get("count", 0)),
            },
            "weak_active_health": health_stats,
            "issues": issues,
            "recommendations": self._recommendations(issues),
            "boundary": self.boundary(),
        }

    def _health_stats(self, active_names: set[str]) -> dict[str, Any]:
        if not active_names:
            return {"available": False, "weak_count": 0, "weak_factors": []}
        try:
            conn = _connect(self.db_path, read_only=True)
        except Exception:
            return {"available": False, "weak_count": 0, "weak_factors": []}
        try:
            if not state_table_exists(conn, "factor_health"):
                return {"available": False, "weak_count": 0, "weak_factors": []}
            rows = _execute(
                conn,
                """
                SELECT factor, score, status, n_obs, rolling_ic
                FROM factor_health
                """,
            ).fetchall()
        finally:
            conn.close()
        weak = []
        for row in rows:
            factor = str(row["factor"] or "")
            if factor not in active_names:
                continue
            score = _safe_float(row["score"])
            status = str(row["status"] or "").lower()
            if score < 40.0 or status in {"watch", "decaying", "retired"}:
                weak.append(
                    {
                        "factor": factor,
                        "score": round(score, 3),
                        "status": status,
                        "n_obs": int(_safe_float(row["n_obs"])),
                        "rolling_ic": round(_safe_float(row["rolling_ic"]), 6),
                    }
                )
        weak = sorted(weak, key=lambda item: (item["score"], item["factor"]))
        return {
            "available": True,
            "weak_count": len(weak),
            "weak_factors": weak[:25],
        }

    @staticmethod
    def _group_stats(active: list[dict[str, Any]], total_abs_weight: float, *, key: str) -> dict[str, Any]:
        buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "abs_weight": 0.0, "sample": []})
        for item in active:
            name = str(item.get(key) or "ungrouped")
            bucket = buckets[name]
            bucket["count"] += 1
            bucket["abs_weight"] += float(item["abs_weight"])
            if len(bucket["sample"]) < 15:
                bucket["sample"].append(item["factor"])
        result = {}
        for name, bucket in buckets.items():
            result[name] = {
                "count": bucket["count"],
                "abs_weight": round(bucket["abs_weight"], 6),
                "pct_abs_weight": round(bucket["abs_weight"] / total_abs_weight, 6) if total_abs_weight > 0 else 0.0,
                "sample": bucket["sample"],
            }
        return dict(sorted(result.items(), key=lambda kv: (kv[1]["abs_weight"], kv[1]["count"]), reverse=True))

    @staticmethod
    def _tag_stats(active: list[dict[str, Any]], total_abs_weight: float) -> list[dict[str, Any]]:
        buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {"count": 0, "abs_weight": 0.0, "sample": []})
        for item in active:
            tags = item.get("tags") or ["untagged"]
            for tag in tags:
                bucket = buckets[str(tag or "untagged")]
                bucket["count"] += 1
                bucket["abs_weight"] += float(item["abs_weight"])
                if len(bucket["sample"]) < 15:
                    bucket["sample"].append(item["factor"])
        rows = []
        for tag, bucket in buckets.items():
            rows.append(
                {
                    "tag": tag,
                    "count": bucket["count"],
                    "abs_weight": round(bucket["abs_weight"], 6),
                    "pct_abs_weight": round(bucket["abs_weight"] / total_abs_weight, 6) if total_abs_weight > 0 else 0.0,
                    "sample": bucket["sample"],
                }
            )
        return sorted(rows, key=lambda item: (item["abs_weight"], item["count"]), reverse=True)

    @staticmethod
    def _redundancy_stats(active: list[dict[str, Any]], total_abs_weight: float) -> list[dict[str, Any]]:
        grouped = [item for item in active if item.get("redundancy_group")]
        stats = FactorBlendHealthService._group_stats(grouped, total_abs_weight, key="redundancy_group")
        rows = [{"group": group, **payload} for group, payload in stats.items()]
        return sorted(rows, key=lambda item: (item["abs_weight"], item["count"]), reverse=True)

    @staticmethod
    def _issues(
        *,
        active: list[dict[str, Any]],
        family_stats: dict[str, Any],
        tag_stats: list[dict[str, Any]],
        redundancy_stats: list[dict[str, Any]],
        low_weight: list[dict[str, Any]],
        health_stats: dict[str, Any],
        cfg: Any,
    ) -> list[dict[str, Any]]:
        issues: list[dict[str, Any]] = []
        active_count = len(active)
        if active_count > DEFAULT_WARN_MAX_ACTIVE_ALPHA:
            issues.append(
                {
                    "severity": "error",
                    "code": "too_many_active_alpha_factors",
                    "value": active_count,
                    "limit": DEFAULT_WARN_MAX_ACTIVE_ALPHA,
                }
            )
        elif active_count > DEFAULT_TARGET_MAX_ACTIVE_ALPHA:
            issues.append(
                {
                    "severity": "warn",
                    "code": "active_alpha_above_target",
                    "value": active_count,
                    "limit": DEFAULT_TARGET_MAX_ACTIVE_ALPHA,
                }
            )
        for family in ("dsl_auto", "pca"):
            count = int(family_stats.get(family, {}).get("count", 0))
            if count > DEFAULT_WARN_MAX_NOISE_FAMILY_COUNT:
                issues.append(
                    {
                        "severity": "warn",
                        "code": f"large_{family}_population",
                        "value": count,
                        "limit": DEFAULT_WARN_MAX_NOISE_FAMILY_COUNT,
                    }
                )
        if len(low_weight) > max(20, active_count // 3):
            issues.append(
                {
                    "severity": "warn",
                    "code": "many_low_weight_alpha_factors",
                    "value": len(low_weight),
                    "threshold": DEFAULT_LOW_WEIGHT_THRESHOLD,
                }
            )
        redundancy_limit = _safe_float(getattr(cfg, "factor_redundancy_max_group_weight", 0.35), 0.35)
        for item in redundancy_stats:
            if int(item.get("count") or 0) > 1 and float(item.get("pct_abs_weight") or 0.0) > redundancy_limit:
                issues.append(
                    {
                        "severity": "warn",
                        "code": "redundancy_group_weight_concentration",
                        "group": item["group"],
                        "value": item["pct_abs_weight"],
                        "limit": redundancy_limit,
                    }
                )
        # single-factor-type cap (was awe_max_type_weight_pct)
        tag_limit = 0.40
        for item in tag_stats[:3]:
            if active_count >= 5 and float(item.get("pct_abs_weight") or 0.0) > tag_limit:
                issues.append(
                    {
                        "severity": "warn",
                        "code": "tag_weight_concentration",
                        "tag": item["tag"],
                        "value": item["pct_abs_weight"],
                        "limit": tag_limit,
                    }
                )
        weak_count = int(health_stats.get("weak_count") or 0)
        if weak_count > 0:
            issues.append(
                {
                    "severity": "warn",
                    "code": "weak_health_active_alpha_factors",
                    "value": weak_count,
                }
            )
        return issues

    @staticmethod
    def _recommendations(issues: list[dict[str, Any]]) -> list[str]:
        codes = {str(item.get("code") or "") for item in issues}
        recommendations: list[str] = []
        if "too_many_active_alpha_factors" in codes or "active_alpha_above_target" in codes:
            recommendations.append("materialize_pruning_candidates_before_next_factor_promotion")
        if "many_low_weight_alpha_factors" in codes:
            recommendations.append("review_or_retire_low_weight_alpha_tail")
        if "large_dsl_auto_population" in codes:
            recommendations.append("cap_dsl_auto_population_with_oos_evidence")
        if "large_pca_population" in codes:
            recommendations.append("cap_pca_population_with_redundancy_review")
        if "redundancy_group_weight_concentration" in codes or "tag_weight_concentration" in codes:
            recommendations.append("rebalance_concentrated_factor_groups_through_decision_policy")
        if "weak_health_active_alpha_factors" in codes:
            recommendations.append("prioritize_weak_health_active_factors_for_shadow_review")
        return recommendations
