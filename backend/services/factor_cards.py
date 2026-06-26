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
from backend.core.db import STATE_DB, STATE_DB_DDL, connect_sqlite


_CARD_CACHE_TTL_SEC = 60.0
_CARD_CACHE_LOCK = threading.Lock()
_CARD_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def clear_factor_card_cache(db_path: str | Path | None = None) -> None:
    prefix = f"{Path(db_path).resolve()}|" if db_path else None
    with _CARD_CACHE_LOCK:
        if prefix is None:
            _CARD_CACHE.clear()
            return
        for key in list(_CARD_CACHE):
            if key.startswith(prefix):
                _CARD_CACHE.pop(key, None)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _round(value: Any, digits: int = 6) -> float:
    try:
        return round(float(value), digits)
    except Exception:
        return 0.0


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
    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    @contextmanager
    def _conn(self):
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_schema(self) -> None:
        with self._conn() as conn:
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
    ) -> list[dict[str, Any]]:
        cache_key = (
            f"{self.db_path.resolve()}|{source or '*'}|{lifecycle_status or '*'}|"
            f"{factor_id or '*'}|{factor_family or '*'}"
        )
        now_ts = time.time()
        with _CARD_CACHE_LOCK:
            cached = _CARD_CACHE.get(cache_key)
            if cached and cached[0] > now_ts:
                return deepcopy(cached[1][:limit])

        ids = self._factor_ids()
        items = []
        for name in ids:
            card = self._build_card(name)
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

    def _factor_ids(self) -> list[str]:
        adapter = RegistryAdapter.shared()
        names = set(factor_registry.list()) | set(adapter._meta.keys())
        with self._conn() as conn:
            for table, column in (
                ("factor_health", "factor"),
                ("decision_factor_snapshot", "factor"),
                ("factor_contribution_review", "factor"),
            ):
                rows = conn.execute(f"SELECT DISTINCT {column} AS value FROM {table}").fetchall()
                names.update(str(row["value"] or "") for row in rows if str(row["value"] or ""))
            rows = conn.execute(
                """
                SELECT DISTINCT scope_key AS value
                FROM policy_suggestion
                WHERE scope_type='factor'
                UNION
                SELECT DISTINCT scope_key AS value
                FROM learning_application_log
                WHERE scope_type='factor'
                """
            ).fetchall()
            names.update(str(row["value"] or "") for row in rows if str(row["value"] or ""))
        return sorted(name for name in names if name)

    def _build_card(self, factor_id: str) -> dict[str, Any]:
        adapter = RegistryAdapter.shared()
        meta = adapter.get_meta(factor_id)
        func = factor_registry.get(factor_id)
        description = str(
            meta.get("description")
            or getattr(func, "_factor_desc", "")
            or factor_id
        )
        source = str(meta.get("source") or ("builtin" if func else "unknown"))
        lifecycle = str(
            adapter._lifecycle_statuses.get(factor_id)
            or ("ACTIVE" if func else "UNKNOWN")
        )
        family = self._infer_family(factor_id, description, source)
        defaults = _FAMILY_DEFAULTS.get(family, _FAMILY_DEFAULTS["composite"])
        parameters = self._infer_parameters(factor_id, description, family)
        formula_version = str(meta.get("formula_version") or self._default_formula_version(source, factor_id))
        parameter_version = str(meta.get("parameter_version") or self._default_parameter_version(source))
        evidence = self._evidence_summary(factor_id, description)
        governance = self._governance_state(factor_id, evidence=evidence)
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
                "latest_template_candidate_trace": governance["latest_template_candidate_trace"],
                "latest_template_recommendation": governance["latest_template_recommendation"],
            },
            "evidence_summary": {
                "description": description,
                "health_score": evidence["health_score"],
                "shadow_score": evidence["shadow_score"],
                "avg_contribution_score": evidence["avg_contribution_score"],
                "last_primary_responsibility": evidence["last_primary_responsibility"],
                "recent_responsibility_labels": evidence["recent_responsibility_labels"],
                "sample_count": evidence["sample_count"],
            },
            "updated_at": governance["updated_at"] or evidence["updated_at"] or None,
            "updated_at_ts": updated_at_ts,
        }

    def _evidence_summary(self, factor_id: str, description: str) -> dict[str, Any]:
        with self._conn() as conn:
            health_row = conn.execute(
                """
                SELECT score, updated_at
                FROM factor_health
                WHERE factor=?
                """,
                (factor_id,),
            ).fetchone()
            snapshot_row = conn.execute(
                """
                SELECT AVG(shadow_score) AS avg_shadow_score,
                       AVG(contribution_score) AS avg_contribution_score,
                       COUNT(*) AS sample_count
                FROM decision_factor_snapshot
                WHERE factor=?
                """,
                (factor_id,),
            ).fetchone()
            review_rows = conn.execute(
                """
                SELECT notes
                FROM factor_contribution_review
                WHERE factor=?
                ORDER BY id DESC
                LIMIT 12
                """,
                (factor_id,),
            ).fetchall()
            review_updated_row = conn.execute(
                """
                SELECT MAX(r.created_at) AS updated_at
                FROM factor_contribution_review f
                JOIN trade_outcome_review r ON r.review_id = f.review_id
                WHERE f.factor=?
                """,
                (factor_id,),
            ).fetchone()
        labels: list[str] = []
        last_primary = ""
        for row in review_rows:
            payload = _loads(row["notes"], {}) if str(row["notes"] or "").startswith("{") else {}
            if not last_primary:
                last_primary = str(payload.get("primary_responsibility") or "")
            for label in payload.get("responsibility_labels") or []:
                item = str(label or "")
                if item and item not in labels:
                    labels.append(item)
        updated_at_ts = max(
            float((health_row["updated_at"] if health_row else 0.0) or 0.0),
            float((review_updated_row["updated_at"] if review_updated_row else 0.0) or 0.0),
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

    def _governance_state(self, factor_id: str, *, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
        with self._conn() as conn:
            suggestion = conn.execute(
                """
                SELECT action, status, created_at
                FROM policy_suggestion
                WHERE scope_type='factor' AND scope_key=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (factor_id,),
            ).fetchone()
            app = conn.execute(
                """
                SELECT action, status, created_at
                FROM learning_application_log
                WHERE scope_type='factor' AND scope_key=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (factor_id,),
            ).fetchone()
            effect = conn.execute(
                """
                SELECT status, updated_at
                FROM learning_application_effect
                WHERE scope_type='factor' AND scope_key=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (factor_id,),
            ).fetchone()
            active_template = conn.execute(
                """
                SELECT template_id, template_version, regime_key, updated_at
                FROM parameter_template_active
                WHERE factor_id=?
                ORDER BY updated_at DESC
                LIMIT 1
                """,
                (factor_id,),
            ).fetchone()
            latest_candidate = conn.execute(
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
            "application_effect_status": effect_status,
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
