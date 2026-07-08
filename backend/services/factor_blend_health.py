from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists


DEFAULT_TARGET_MAX_ACTIVE_ALPHA = 80
DEFAULT_WARN_MAX_ACTIVE_ALPHA = 120
DEFAULT_WARN_MAX_NOISE_FAMILY_COUNT = 40
DEFAULT_LOW_WEIGHT_THRESHOLD = 0.02


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


def _conn_is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn: Any, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


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

    def build(self, cfg: Any | None = None, *, use_catalog: bool | None = None) -> dict[str, Any]:
        cfg_was_none = cfg is None
        if cfg is None:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
        if use_catalog is None:
            use_catalog = cfg_was_none
        if use_catalog:
            active = self._active_from_factor_catalog(cfg)
            if active:
                return self._build_from_active(cfg, active, active_count_source="factor_catalog.used_in_score")
        return self._build_from_runtime_config(cfg, active_count_source="runtime_config")

    def build_current(self, cfg: Any | None = None) -> dict[str, Any]:
        if cfg is None:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
        active = self._active_from_factor_catalog(cfg)
        if active:
            return self._build_from_active(cfg, active, active_count_source="factor_catalog.used_in_score")
        return self._build_from_runtime_config(cfg, active_count_source="runtime_config_fallback")

    def _active_from_factor_catalog(self, cfg: Any) -> list[dict[str, Any]]:
        try:
            from backend.services.factor_catalog import build_factor_catalog
        except Exception:
            return []
        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        active: list[dict[str, Any]] = []
        try:
            catalog = build_factor_catalog(self.db_path)
        except Exception:
            return []
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
        )

    def _build_from_active(
        self,
        cfg: Any,
        active: list[dict[str, Any]],
        *,
        active_count_source: str,
        configured_alpha_count: int | None = None,
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
        issues = self._issues(
            active=active,
            family_stats=family_stats,
            tag_stats=tag_stats,
            redundancy_stats=redundancy_stats,
            low_weight=low_weight,
            health_stats=health_stats,
            cfg=cfg,
        )
        status = "ok"
        if any(item.get("severity") == "error" for item in issues):
            status = "degraded"
        elif issues:
            status = "watch"
        return {
            "ok": True,
            "schema_version": "factor_blend_health.v1",
            "status": status,
            "configured_alpha_count": configured_alpha_count,
            "active_alpha_count": len(active),
            "active_count_source": active_count_source,
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
        tag_limit = _safe_float(getattr(cfg, "awe_max_type_weight_pct", 0.40), 0.40)
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
