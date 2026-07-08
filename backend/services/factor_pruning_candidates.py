from __future__ import annotations

from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services.factor_blend_health import DEFAULT_LOW_WEIGHT_THRESHOLD, FactorBlendHealthService


DEFAULT_MAX_CANDIDATES = 50
WEAK_HEALTH_SCORE = 40.0
DEFAULT_RECENT_REVIEW_LIMIT = 50
MIN_LIVE_DECISION_REVIEWS = 1
MIN_LIVE_LOSS_REVIEWS = 2
HARMFUL_LOSS_CONTRIBUTION = 0.02
MIN_LIVE_AVG_ABS_CONTRIBUTION = 0.005


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


class FactorPruningCandidateService:
    """Build candidate-only factor pruning advice.

    The service deliberately does not write policy_suggestion or governance
    candidate rows. It gives the multi-agent layer a clean list to debate
    before any DecisionPolicy-authorized mutation exists.
    """

    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "read_only": True,
            "candidate_only": True,
            "affects_trading": False,
            "writes_runtime": False,
            "writes_policy_suggestion": False,
            "writes_brain_governance_candidate": False,
            "applies_pruning": False,
            "requires_decision_policy_for_weight_change": True,
            "requires_risk_policy_for_live_effect": True,
            "requires_recent_live_decision_participation": True,
            "ignores_zero_contribution_factors": True,
            "can_include_snapshot_only_discovered_factors": True,
        }

    def build(self, cfg: Any | None = None, *, limit: int = DEFAULT_MAX_CANDIDATES) -> dict[str, Any]:
        if cfg is None:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()

        health = FactorBlendHealthService(self.db_path).build_current(cfg)
        active = self._active_alpha_factors(cfg)
        active = self._merge_snapshot_alpha_factors(active)
        decision_evidence = self._recent_decision_evidence({item["factor"] for item in active})
        weak_health = self._weak_health_by_factor({item["factor"] for item in active})
        family_counts = dict(health.get("noise_family_counts") or {})
        runtime_family_counts: dict[str, int] = {}
        for item in active:
            if bool(item.get("snapshot_only")):
                continue
            family = str(item.get("family") or "core")
            runtime_family_counts[family] = runtime_family_counts.get(family, 0) + 1
        for family, count in runtime_family_counts.items():
            family_counts[family] = max(int(_safe_float(family_counts.get(family))), count)
        issue_codes = {str(item.get("code") or "") for item in (health.get("issues") or [])}
        active_alpha_count = int(health.get("active_alpha_count") or 0) or sum(
            1 for item in active if not bool(item.get("snapshot_only"))
        )
        candidates = []
        skipped_cold = 0
        for item in active:
            candidate = self._candidate_for_factor(
                item,
                decision_evidence=decision_evidence.get(item["factor"]),
                weak_health=weak_health.get(item["factor"]),
                family_counts=family_counts,
                issue_codes=issue_codes,
                active_alpha_count=active_alpha_count,
            )
            if candidate:
                candidates.append(candidate)
            elif item["factor"] not in decision_evidence:
                skipped_cold += 1
        candidates = sorted(
            candidates,
            key=lambda row: (
                bool((row.get("evidence") or {}).get("snapshot_only")),
                -float(row["priority_score"]),
                float(row["current_weight"]),
                row["factor"],
            ),
        )
        max_limit = max(1, int(limit or DEFAULT_MAX_CANDIDATES))
        selected = candidates[:max_limit]
        status = "ok"
        top_priority = max((float(item.get("priority_score") or 0.0) for item in selected), default=0.0)
        if selected and (health.get("status") == "degraded" or top_priority >= 0.75):
            status = "actionable"
        elif selected:
            status = "watch"
        return {
            "ok": True,
            "schema_version": "factor_pruning_candidates.v1",
            "status": status,
            "source_health_status": health.get("status"),
            "active_alpha_count": active_alpha_count,
            "generated_count": len(candidates),
            "candidate_count": len(selected),
            "skipped_cold_factor_count": skipped_cold,
            "max_candidates": max_limit,
            "candidates": selected,
            "summary": self._summary(selected),
            "boundary": self.boundary(),
        }

    def _active_alpha_factors(self, cfg: Any) -> list[dict[str, Any]]:
        from alpha.portfolio_compositor import resolve_factor_role

        signal_cfg = dict(getattr(cfg, "factor_signal_config", {}) or {})
        weights = dict(getattr(cfg, "factor_portfolio_weights", {}) or {})
        active = []
        for name in sorted(set(signal_cfg) | set(weights)):
            entry = signal_cfg.get(name, {})
            if not isinstance(entry, dict):
                entry = {}
            role = resolve_factor_role(name, entry)
            weight = abs(_safe_float(weights.get(name, entry.get("weight", 0.0)), 0.0))
            if role != "alpha" or not bool(entry.get("enabled", True)) or weight <= 0.0:
                continue
            active.append(
                {
                    "factor": name,
                    "current_weight": round(weight, 8),
                    "family": _family(name),
                    "tags": [str(tag) for tag in (entry.get("tags") or [])],
                    "source": str(entry.get("source") or ""),
                    "redundancy_group": str(entry.get("redundancy_group") or ""),
                }
            )
        return active

    def _merge_snapshot_alpha_factors(self, active: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_factor = {str(item.get("factor") or ""): dict(item) for item in active}
        for item in self._recent_snapshot_alpha_factors(exclude=set(by_factor)):
            by_factor[str(item["factor"])] = item
        return [by_factor[name] for name in sorted(by_factor)]

    def _recent_snapshot_alpha_factors(self, *, exclude: set[str]) -> list[dict[str, Any]]:
        try:
            conn = _connect(self.db_path, read_only=True)
        except Exception:
            return []
        try:
            if not state_table_exists(conn, "trade_outcome_review") or not state_table_exists(conn, "decision_factor_snapshot"):
                return []
            rows = _execute(
                conn,
                """
                SELECT dfs.factor,
                       AVG(dfs.policy_weight) AS avg_policy_weight,
                       AVG(ABS(dfs.contribution_score)) AS avg_abs_contribution,
                       COUNT(DISTINCT r.review_id) AS decision_review_count,
                       MAX(COALESCE(dfs.source, '')) AS source
                FROM (
                    SELECT review_id, entry_decision_id, created_at
                    FROM trade_outcome_review
                    WHERE COALESCE(entry_decision_id, '') <> ''
                    ORDER BY created_at DESC
                    LIMIT ?
                ) r
                JOIN decision_factor_snapshot dfs ON dfs.decision_id = r.entry_decision_id
                GROUP BY dfs.factor
                """,
                (DEFAULT_RECENT_REVIEW_LIMIT,),
            ).fetchall()
        except Exception:
            return []
        finally:
            conn.close()
        items = []
        for row in rows:
            factor = str(row["factor"] or "")
            family = _family(factor)
            if not factor or factor in exclude or family not in {"dsl_auto", "pca"}:
                continue
            weight = abs(_safe_float(row["avg_policy_weight"]))
            avg_abs = _safe_float(row["avg_abs_contribution"])
            if weight <= 0.0 or avg_abs < MIN_LIVE_AVG_ABS_CONTRIBUTION:
                continue
            items.append(
                {
                    "factor": factor,
                    "current_weight": round(weight, 8),
                    "family": family,
                    "tags": ["live_decision_snapshot", "runtime_missing_config"],
                    "source": str(row["source"] or "decision_factor_snapshot"),
                    "redundancy_group": "",
                    "snapshot_only": True,
                    "snapshot_decision_review_count": int(_safe_float(row["decision_review_count"])),
                    "snapshot_avg_abs_contribution": round(avg_abs, 6),
                }
            )
        return items

    def _weak_health_by_factor(self, active_names: set[str]) -> dict[str, dict[str, Any]]:
        if not active_names:
            return {}
        try:
            conn = _connect(self.db_path, read_only=True)
        except Exception:
            return {}
        try:
            if not state_table_exists(conn, "factor_health"):
                return {}
            rows = _execute(
                conn,
                """
                SELECT factor, score, status, n_obs, rolling_ic
                FROM factor_health
                """,
            ).fetchall()
        finally:
            conn.close()
        weak = {}
        for row in rows:
            factor = str(row["factor"] or "")
            if factor not in active_names:
                continue
            score = _safe_float(row["score"])
            status = str(row["status"] or "").lower()
            if score < WEAK_HEALTH_SCORE or status in {"watch", "decaying", "retired"}:
                weak[factor] = {
                    "score": round(score, 3),
                    "status": status,
                    "n_obs": int(_safe_float(row["n_obs"])),
                    "rolling_ic": round(_safe_float(row["rolling_ic"]), 6),
                }
        return weak

    def _recent_decision_evidence(self, active_names: set[str], *, review_limit: int = DEFAULT_RECENT_REVIEW_LIMIT) -> dict[str, dict[str, Any]]:
        if not active_names:
            return {}
        try:
            conn = _connect(self.db_path, read_only=True)
        except Exception:
            return {}
        try:
            if not state_table_exists(conn, "trade_outcome_review") or not state_table_exists(conn, "decision_factor_snapshot"):
                return {}
            rows = _execute(
                conn,
                """
                SELECT dfs.factor,
                       COUNT(DISTINCT r.review_id) AS decision_review_count,
                       SUM(CASE WHEN r.pnl <= 0 THEN 1 ELSE 0 END) AS loss_review_count,
                       SUM(CASE WHEN r.pnl > 0 THEN 1 ELSE 0 END) AS win_review_count,
                       AVG(ABS(dfs.contribution_score)) AS avg_abs_contribution,
                       AVG(CASE WHEN r.pnl <= 0 THEN ABS(dfs.contribution_score) END) AS loss_abs_contribution,
                       AVG(CASE WHEN r.pnl > 0 THEN ABS(dfs.contribution_score) END) AS win_abs_contribution,
                       AVG(CASE WHEN r.pnl <= 0 THEN dfs.contribution_score END) AS loss_avg_contribution,
                       AVG(CASE WHEN r.pnl > 0 THEN dfs.contribution_score END) AS win_avg_contribution,
                       AVG(dfs.policy_weight) AS avg_policy_weight,
                       SUM(CASE WHEN r.failure_tags_json LIKE '%market_data_stale%' THEN 1 ELSE 0 END) AS market_data_stale_count,
                       SUM(CASE WHEN r.failure_tags_json LIKE '%signal_execution_delay%' THEN 1 ELSE 0 END) AS signal_execution_delay_count,
                       MAX(r.created_at) AS latest_review_at
                FROM (
                    SELECT review_id, entry_decision_id, pnl, failure_tags_json, created_at
                    FROM trade_outcome_review
                    WHERE COALESCE(entry_decision_id, '') <> ''
                    ORDER BY created_at DESC
                    LIMIT ?
                ) r
                JOIN decision_factor_snapshot dfs ON dfs.decision_id = r.entry_decision_id
                GROUP BY dfs.factor
                """,
                (max(1, int(review_limit or DEFAULT_RECENT_REVIEW_LIMIT)),),
            ).fetchall()
        except Exception:
            return {}
        finally:
            conn.close()
        evidence = {}
        for row in rows:
            factor = str(row["factor"] or "")
            if factor not in active_names:
                continue
            decision_count = int(_safe_float(row["decision_review_count"]))
            loss_count = int(_safe_float(row["loss_review_count"]))
            win_count = int(_safe_float(row["win_review_count"]))
            loss_avg = _safe_float(row["loss_avg_contribution"])
            win_avg = _safe_float(row["win_avg_contribution"])
            loss_abs = _safe_float(row["loss_abs_contribution"])
            win_abs = _safe_float(row["win_abs_contribution"])
            evidence[factor] = {
                "schema_version": "factor_recent_decision_evidence.v1",
                "lookback_review_limit": max(1, int(review_limit or DEFAULT_RECENT_REVIEW_LIMIT)),
                "decision_review_count": decision_count,
                "loss_review_count": loss_count,
                "win_review_count": win_count,
                "loss_rate": round(loss_count / max(decision_count, 1), 4),
                "avg_abs_contribution": round(_safe_float(row["avg_abs_contribution"]), 6),
                "loss_abs_contribution": round(loss_abs, 6),
                "win_abs_contribution": round(win_abs, 6),
                "loss_avg_contribution": round(loss_avg, 6),
                "win_avg_contribution": round(win_avg, 6),
                "sign_flip_between_loss_and_win": bool(loss_count and win_count and loss_avg * win_avg < 0),
                "avg_policy_weight": round(_safe_float(row["avg_policy_weight"]), 6),
                "market_data_stale_count": int(_safe_float(row["market_data_stale_count"])),
                "signal_execution_delay_count": int(_safe_float(row["signal_execution_delay_count"])),
                "latest_review_at": _safe_float(row["latest_review_at"]),
            }
        return evidence

    @staticmethod
    def _candidate_for_factor(
        item: dict[str, Any],
        *,
        decision_evidence: dict[str, Any] | None,
        weak_health: dict[str, Any] | None,
        family_counts: dict[str, Any],
        issue_codes: set[str],
        active_alpha_count: int,
    ) -> dict[str, Any] | None:
        reasons = []
        priority = 0.0
        pruning_pressure = False
        weight = float(item["current_weight"])
        family = str(item.get("family") or "core")
        if not decision_evidence or int(_safe_float(decision_evidence.get("decision_review_count"))) < MIN_LIVE_DECISION_REVIEWS:
            return None
        decision_count = int(_safe_float(decision_evidence.get("decision_review_count")))
        loss_count = int(_safe_float(decision_evidence.get("loss_review_count")))
        loss_rate = _safe_float(decision_evidence.get("loss_rate"))
        avg_abs = _safe_float(decision_evidence.get("avg_abs_contribution"))
        loss_abs = _safe_float(decision_evidence.get("loss_abs_contribution"))
        win_abs = _safe_float(decision_evidence.get("win_abs_contribution"))
        sign_flip = bool(decision_evidence.get("sign_flip_between_loss_and_win"))
        if avg_abs < MIN_LIVE_AVG_ABS_CONTRIBUTION:
            return None
        priority += min(0.20, decision_count / 50.0 * 0.20)
        reasons.append(
            {
                "code": "recent_live_decision_participation",
                "decision_review_count": decision_count,
                "lookback_review_limit": decision_evidence.get("lookback_review_limit"),
            }
        )
        if loss_count >= MIN_LIVE_LOSS_REVIEWS and loss_abs >= HARMFUL_LOSS_CONTRIBUTION:
            pruning_pressure = True
            priority += 0.45
            reasons.append(
                {
                    "code": "recent_loss_contribution_pressure",
                    "loss_review_count": loss_count,
                    "loss_abs_contribution": round(loss_abs, 6),
                    "win_abs_contribution": round(win_abs, 6),
                    "sign_flip_between_loss_and_win": sign_flip,
                }
            )
        if sign_flip and loss_abs >= HARMFUL_LOSS_CONTRIBUTION:
            pruning_pressure = True
            priority += 0.20
            reasons.append({"code": "loss_win_contribution_sign_flip"})
        if loss_rate >= 0.55 and loss_count >= MIN_LIVE_LOSS_REVIEWS:
            pruning_pressure = True
            priority += 0.10
            reasons.append({"code": "recent_loss_rate_pressure", "loss_rate": round(loss_rate, 4)})
        if weight <= DEFAULT_LOW_WEIGHT_THRESHOLD:
            pruning_pressure = True
            priority += 0.35
            reasons.append({"code": "low_weight_tail", "threshold": DEFAULT_LOW_WEIGHT_THRESHOLD})
        family_count = int(_safe_float(family_counts.get(family)))
        if family in {"dsl_auto", "pca"} and family_count > 40:
            pruning_pressure = True
            priority += 0.25
            reasons.append({"code": "large_noise_family", "family": family, "family_count": family_count})
        if weak_health:
            pruning_pressure = True
            priority += 0.35
            reasons.append({"code": "weak_factor_health", **weak_health})
        if "too_many_active_alpha_factors" in issue_codes or "active_alpha_above_target" in issue_codes:
            pruning_pressure = True
            priority += 0.10
            reasons.append({"code": "active_alpha_population_pressure", "active_alpha_count": active_alpha_count})
        if not pruning_pressure:
            return None
        system_issue_count = int(_safe_float(decision_evidence.get("market_data_stale_count"))) + int(
            _safe_float(decision_evidence.get("signal_execution_delay_count"))
        )
        if system_issue_count:
            reasons.append(
                {
                    "code": "system_issue_caveat",
                    "market_data_stale_count": int(_safe_float(decision_evidence.get("market_data_stale_count"))),
                    "signal_execution_delay_count": int(_safe_float(decision_evidence.get("signal_execution_delay_count"))),
                    "priority_bonus": 0.0,
                }
            )
        if weak_health and weight <= DEFAULT_LOW_WEIGHT_THRESHOLD:
            action = "review_disable"
            target_weight = 0.0
        elif weak_health:
            action = "review_downweight"
            target_weight = round(max(0.0, weight * 0.5), 8)
        elif family in {"dsl_auto", "pca"} and weight <= DEFAULT_LOW_WEIGHT_THRESHOLD:
            action = "review_disable"
            target_weight = 0.0
        else:
            action = "review_downweight"
            target_weight = round(max(0.0, weight * 0.5), 8)
        priority = min(1.0, priority)
        return {
            "candidate_id": f"factor_prune:{item['factor']}",
            "schema_version": "factor_pruning_candidate.v1",
            "factor": item["factor"],
            "family": family,
            "current_weight": weight,
            "recommended_action": action,
            "suggested_target_weight": target_weight,
            "priority_score": round(priority, 4),
            "confidence": round(min(0.9, 0.45 + priority * 0.45), 4),
            "reasons": reasons,
            "evidence": {
                "source": item.get("source") or "",
                "tags": item.get("tags") or [],
                "redundancy_group": item.get("redundancy_group") or "",
                "snapshot_only": bool(item.get("snapshot_only")),
                "snapshot_decision_review_count": int(_safe_float(item.get("snapshot_decision_review_count"))),
                "snapshot_avg_abs_contribution": _safe_float(item.get("snapshot_avg_abs_contribution")),
                "family_count": family_count,
                "active_alpha_count": active_alpha_count,
                "recent_decision_evidence": decision_evidence,
            },
            "allowed_uses": ["agent_review", "shadow_counterevidence", "decision_policy_proposal_draft"],
            "blocked_uses": ["direct_runtime_write", "direct_factor_disable", "direct_policy_suggestion_submit"],
        }

    @staticmethod
    def _summary(candidates: list[dict[str, Any]]) -> dict[str, Any]:
        by_action: dict[str, int] = {}
        by_family: dict[str, int] = {}
        for item in candidates:
            by_action[str(item.get("recommended_action") or "")] = by_action.get(str(item.get("recommended_action") or ""), 0) + 1
            by_family[str(item.get("family") or "")] = by_family.get(str(item.get("family") or ""), 0) + 1
        return {
            "by_action": dict(sorted(by_action.items())),
            "by_family": dict(sorted(by_family.items())),
            "top_priority_score": max((float(item.get("priority_score") or 0.0) for item in candidates), default=0.0),
        }
