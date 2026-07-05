from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from backend.core.db import (
    STATE_DB,
    STATE_DB_DDL,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_pg_enabled,
)
from backend.services.factor_cards import FactorCardService
from research.learning.governor import RuleEvolutionGovernor
from risk.policy_service import RiskPolicyService


_RECOMMENDATION_CACHE_TTL_SEC = 60.0
_RECOMMENDATION_CACHE_LOCK = threading.Lock()
_RECOMMENDATION_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def _use_pg(db_path: str | Path) -> bool:
    return Path(db_path).resolve() == Path(STATE_DB).resolve()


def _conn_is_pg(conn) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn, sql: str) -> str:
    return sql.replace("%", "%%").replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn, sql: str, params: Any = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), params)


def clear_parameter_template_recommendation_cache(db_path: str | Path | None = None) -> None:
    prefix = f"{Path(db_path).resolve()}|" if db_path else None
    with _RECOMMENDATION_CACHE_LOCK:
        if prefix is None:
            _RECOMMENDATION_CACHE.clear()
            return
        for key in list(_RECOMMENDATION_CACHE):
            if key.startswith(prefix):
                _RECOMMENDATION_CACHE.pop(key, None)


def _loads(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


_MANUAL_TEMPLATE_LIBRARY: dict[str, list[dict[str, Any]]] = {
    "rsi_14": [
        {
            "regime_key": "",
            "template_version": "default.v1",
            "template_role": "default",
            "parameters": {"length": 14, "upper_band": 70, "lower_band": 30},
            "applicable_regimes": ["range", "mean_reversion"],
            "avoid_regimes": ["strong_trend"],
            "holding_profile_hint": {"style": "short_swing", "min_bars": 2, "max_bars": 12},
            "tuning_bias": "neutral",
        },
        {
            "regime_key": "",
            "template_version": "conservative.v1",
            "template_role": "conservative",
            "parameters": {"length": 21, "upper_band": 74, "lower_band": 26},
            "applicable_regimes": ["strong_trend", "low_vol"],
            "avoid_regimes": ["breakout"],
            "holding_profile_hint": {"style": "confirmation", "min_bars": 3, "max_bars": 14},
            "tuning_bias": "stability",
        },
        {
            "regime_key": "",
            "template_version": "aggressive.v1",
            "template_role": "aggressive",
            "parameters": {"length": 9, "upper_band": 65, "lower_band": 35},
            "applicable_regimes": ["range", "breakout"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "fast_reversion", "min_bars": 1, "max_bars": 8},
            "tuning_bias": "responsiveness",
        },
    ],
    "macd_hist": [
        {
            "regime_key": "",
            "template_version": "default.v1",
            "template_role": "default",
            "parameters": {"fast_length": 12, "slow_length": 26, "signal_length": 9},
            "applicable_regimes": ["trend", "breakout"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "trend_follow", "min_bars": 4, "max_bars": 24},
            "tuning_bias": "neutral",
        },
        {
            "regime_key": "",
            "template_version": "conservative.v1",
            "template_role": "conservative",
            "parameters": {"fast_length": 16, "slow_length": 34, "signal_length": 11},
            "applicable_regimes": ["macro_drift", "strong_trend"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "trend_follow", "min_bars": 6, "max_bars": 30},
            "tuning_bias": "stability",
        },
        {
            "regime_key": "",
            "template_version": "aggressive.v1",
            "template_role": "aggressive",
            "parameters": {"fast_length": 8, "slow_length": 21, "signal_length": 5},
            "applicable_regimes": ["breakout", "high_vol"],
            "avoid_regimes": ["low_vol"],
            "holding_profile_hint": {"style": "momentum_burst", "min_bars": 2, "max_bars": 12},
            "tuning_bias": "responsiveness",
        },
    ],
    "adx": [
        {
            "regime_key": "",
            "template_version": "default.v1",
            "template_role": "default",
            "parameters": {"length": 14, "trend_gate": 25},
            "applicable_regimes": ["trend", "macro_drift"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "trend_confirm", "min_bars": 4, "max_bars": 24},
            "tuning_bias": "neutral",
        },
        {
            "regime_key": "",
            "template_version": "conservative.v1",
            "template_role": "conservative",
            "parameters": {"length": 20, "trend_gate": 28},
            "applicable_regimes": ["strong_trend"],
            "avoid_regimes": ["range", "low_vol"],
            "holding_profile_hint": {"style": "trend_confirm", "min_bars": 5, "max_bars": 28},
            "tuning_bias": "stability",
        },
        {
            "regime_key": "",
            "template_version": "aggressive.v1",
            "template_role": "aggressive",
            "parameters": {"length": 10, "trend_gate": 20},
            "applicable_regimes": ["breakout", "high_vol"],
            "avoid_regimes": ["range"],
            "holding_profile_hint": {"style": "fast_trend_confirm", "min_bars": 2, "max_bars": 14},
            "tuning_bias": "responsiveness",
        },
    ],
}


class ParameterTemplateService:
    RUNTIME_TUNABLE_FACTORS = {
        "rsi_14",
        "macd_hist",
        "adx",
        "stoch_k",
        "ema_slope",
        "bb_width",
        "obv_slope",
        "vol_ma_ratio",
        "supertrend_str",
        "keltner_width",
    }

    def __init__(self, db_path: str | None = None, *, ensure_schema: bool = True):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cards = FactorCardService(str(self.db_path), ensure_schema=ensure_schema)
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

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def list_templates(
        self,
        *,
        factor_id: str | None = None,
        regime: str | None = None,
        limit: int = 200,
        include_derived: bool = True,
    ) -> list[dict[str, Any]]:
        persisted = self._list_persisted(factor_id=factor_id, regime=regime)
        items = {(item["template_id"]): item for item in persisted}
        cards = self.cards.list_cards(limit=500, factor_id=factor_id)
        for card in cards:
            manual = self._manual_templates_for_card(card)
            generated = manual or (self._build_templates(card) if include_derived else [])
            for item in generated:
                if regime and regime not in item["applicable_regimes"]:
                    continue
                items.setdefault(item["template_id"], item)
            if include_derived and manual:
                for item in self._build_templates(card):
                    if regime and regime not in item["applicable_regimes"]:
                        continue
                    items.setdefault(item["template_id"], item)
        result = list(items.values())
        result.sort(
            key=lambda item: (
                str(item["factor_id"]),
                str(item["template_version"]),
                str(item.get("regime_key") or ""),
            )
        )
        return result[:limit]

    def list_active_templates(self, *, factor_id: str | None = None) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if factor_id:
                rows = _execute(
                    conn,
                    """
                    SELECT * FROM parameter_template_active
                    WHERE factor_id=?
                    ORDER BY updated_at DESC
                    """,
                    (factor_id,),
                ).fetchall()
            else:
                rows = _execute(
                    conn,
                    """
                    SELECT * FROM parameter_template_active
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["context"] = _loads(item.pop("context_json", "{}"), {})
            items.append(item)
        return items

    def list_recommendations(
        self,
        *,
        factor_id: str | None = None,
        limit: int = 50,
        allow_compute: bool = True,
    ) -> list[dict[str, Any]]:
        cache_key = f"{self.db_path.resolve()}|{factor_id or '*'}"
        now_ts = time.time()
        with _RECOMMENDATION_CACHE_LOCK:
            cached = _RECOMMENDATION_CACHE.get(cache_key)
            if cached and cached[0] > now_ts:
                return deepcopy(cached[1][:limit])
        if not allow_compute:
            return []

        cards = self.cards.list_cards(limit=max(200, limit * 3), factor_id=factor_id)
        items: list[dict[str, Any]] = []
        for card in cards:
            recommendation = self._build_recommendation(card)
            if recommendation:
                items.append(recommendation)
        items.sort(
            key=lambda item: (
                -float(item.get("priority_score") or 0.0),
                -float(item.get("updated_at_ts") or 0.0),
                str(item.get("factor_id") or ""),
            )
        )
        with _RECOMMENDATION_CACHE_LOCK:
            _RECOMMENDATION_CACHE[cache_key] = (now_ts + _RECOMMENDATION_CACHE_TTL_SEC, deepcopy(items))
        return items[:limit]

    def get_recommendation(self, recommendation_id: str) -> dict[str, Any] | None:
        items = self.list_recommendations(limit=500)
        for item in items:
            if str(item.get("recommendation_id") or "") == recommendation_id:
                return item
        return None

    def build_runtime_signal_config(self, base_config: dict[str, dict] | None = None) -> dict[str, dict]:
        from config.runtime_config import shared as _rc_shared

        runtime_cfg = _rc_shared()
        signal_config = deepcopy(base_config if base_config is not None else runtime_cfg.factor_signal_config)
        for item in signal_config.values():
            if not isinstance(item, dict):
                continue
            item.pop("parameter_template_version", None)
            item.pop("parameter_template_role", None)
            item.pop("parameter_template_source", None)
            item.pop("parameter_regime_key", None)
            item.pop("parameter_overrides", None)

        active_templates = self.list_active_templates()
        for active in active_templates:
            template = self.get_template(template_id=str(active.get("template_id") or ""))
            if not template:
                continue
            factor_id = str(template.get("factor_id") or "")
            if not factor_id:
                continue
            if factor_id not in signal_config or not isinstance(signal_config.get(factor_id), dict):
                card = self.cards.list_cards(factor_id=factor_id, limit=1)
                signal_config[factor_id] = {
                    "mode": "rank_mapping",
                    "window": 100,
                    "min_samples": 30,
                    "tags": list(((card[0].get("expected_regimes") if card else []) or [])),
                    "enabled": True,
                }
            cfg = signal_config[factor_id]
            cfg["parameter_template_version"] = str(template.get("template_version") or "")
            cfg["parameter_template_role"] = str(template.get("template_role") or "")
            cfg["parameter_template_source"] = str(template.get("source") or "")
            cfg["parameter_regime_key"] = str(template.get("regime_key") or "")
            cfg["parameter_overrides"] = deepcopy(template.get("parameters") or {})
        return signal_config

    def sync_runtime_config(self) -> int:
        from config.runtime_config import shared as _rc_shared
        from backend.services.runtime_config_mutation import RuntimeConfigMutationService

        runtime_cfg = _rc_shared()
        signal_config = self.build_runtime_signal_config()
        active_templates = self.list_active_templates()
        active_payload = {
            f"{item['factor_id']}:{item.get('regime_key') or 'default'}": {
                "template_id": item.get("template_id"),
                "template_version": item.get("template_version"),
                "status": item.get("status"),
            }
            for item in active_templates
        }
        merged_extra = dict(getattr(runtime_cfg, "extra", {}) or {})
        merged_extra["active_parameter_templates"] = active_payload
        result = RuntimeConfigMutationService(self.db_path).apply_patch(
            {
                "factor_signal_config": signal_config,
                "extra": merged_extra,
            },
            source="parameter_template_sync_runtime_config",
            run_id=f"parameter_template_sync_{int(time.time())}",
            actor="system:parameter_template_service",
            action="sync_runtime_parameter_templates",
            reason="sync active parameter templates into runtime config",
        )
        return int(result.get("version") or 0)

    def upsert_template(
        self,
        template: dict[str, Any],
        *,
        source: str = "manual",
        activate: bool = False,
    ) -> dict[str, Any]:
        item = self._normalize_template(template, source=source)
        now = time.time()
        with self._conn() as conn:
            _execute(
                conn,
                """
                INSERT INTO parameter_template_registry
                (template_id, factor_id, regime_key, template_version, template_role,
                 factor_family, formula_version, base_parameter_version, parameters_json,
                 applicable_regimes_json, avoid_regimes_json, holding_profile_hint_json,
                 evidence_json, source, active, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
                ON CONFLICT(template_id) DO UPDATE SET
                    factor_id=excluded.factor_id,
                    regime_key=excluded.regime_key,
                    template_version=excluded.template_version,
                    template_role=excluded.template_role,
                    factor_family=excluded.factor_family,
                    formula_version=excluded.formula_version,
                    base_parameter_version=excluded.base_parameter_version,
                    parameters_json=excluded.parameters_json,
                    applicable_regimes_json=excluded.applicable_regimes_json,
                    avoid_regimes_json=excluded.avoid_regimes_json,
                    holding_profile_hint_json=excluded.holding_profile_hint_json,
                    evidence_json=excluded.evidence_json,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (
                    item["template_id"],
                    item["factor_id"],
                    item["regime_key"],
                    item["template_version"],
                    item["template_role"],
                    item["factor_family"],
                    item["formula_version"],
                    item["base_parameter_version"],
                    json.dumps(item["parameters"], ensure_ascii=False, default=str),
                    json.dumps(item["applicable_regimes"], ensure_ascii=False, default=str),
                    json.dumps(item["avoid_regimes"], ensure_ascii=False, default=str),
                    json.dumps(item["holding_profile_hint"], ensure_ascii=False, default=str),
                    json.dumps(item["evidence"], ensure_ascii=False, default=str),
                    source,
                    now,
                    now,
                ),
            )
            conn.commit()
        if activate:
            return self.activate_template(
                factor_id=item["factor_id"],
                template_id=item["template_id"],
                regime_key=item["regime_key"],
                note="activate_on_upsert",
            )
        return item

    def create_switch_suggestion(
        self,
        *,
        factor_id: str,
        template_id: str,
        regime_key: str = "",
        note: str = "",
        evidence_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        target = self.get_template(template_id=template_id)
        if not target:
            raise ValueError(f"template not found: {template_id}")
        current = self.get_active_template(factor_id=factor_id, regime_key=regime_key)
        boundary = self.assess_template_change(
            factor_id=factor_id,
            target_template_id=template_id,
            regime_key=regime_key,
        )
        factor_card = self.cards.list_cards(factor_id=factor_id, limit=1)
        card = factor_card[0] if factor_card else {}
        evidence_summary = dict((card.get("evidence_summary") or {})) if card else {}
        suggestion_id = self._new_id("psg")
        payload = {
            "factor_id": factor_id,
            "regime_key": regime_key,
            "current_template_id": current.get("template_id", "") if current else "",
            "target_template_id": target["template_id"],
            "target_template_version": target["template_version"],
            "template_role": target["template_role"],
            "factor_card_evidence": {
                "last_primary_responsibility": str(evidence_summary.get("last_primary_responsibility") or ""),
                "recent_responsibility_labels": list(evidence_summary.get("recent_responsibility_labels") or []),
                "health_score": evidence_summary.get("health_score"),
                "sample_count": evidence_summary.get("sample_count"),
            },
            "boundary": boundary,
            "approval_path": (
                "governed_apply_switch"
                if boundary.get("recommended_scope") == "online_light"
                else "offline_validation_then_gray_release"
            ),
            "evidence_context": dict(evidence_context or {}),
            "note": note,
        }
        now = time.time()
        reason = (
            f"switch {factor_id} to {target['template_version']}"
            if boundary.get("recommended_scope") == "online_light"
            else f"{factor_id} requires offline_deep before switching to {target['template_version']}"
        )
        with self._conn() as conn:
            _execute(
                conn,
                """
                INSERT INTO policy_suggestion
                (suggestion_id, scope_type, scope_key, action, confidence, reason,
                 evidence_json, status, created_at)
                VALUES (?, 'parameter_template', ?, 'switch_parameter_template', ?, ?, ?, 'proposed', ?)
                """,
                (
                    suggestion_id,
                    f"{factor_id}:{regime_key or 'default'}",
                    0.55,
                    reason,
                    json.dumps(payload, ensure_ascii=False, default=str),
                    now,
                ),
            )
            conn.commit()
        return {
            "suggestion_id": suggestion_id,
            "scope_type": "parameter_template",
            "scope_key": f"{factor_id}:{regime_key or 'default'}",
            "action": "switch_parameter_template",
            "status": "proposed",
            "evidence": payload,
            "boundary": boundary,
        }

    def create_suggestion_from_recommendation(
        self,
        *,
        recommendation_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        recommendation = self.get_recommendation(recommendation_id)
        if not recommendation:
            raise ValueError(f"recommendation not found: {recommendation_id}")
        evidence_context = {
            "source": "parameter_template_recommendation",
            "recommendation_id": recommendation_id,
            "reason": recommendation.get("reason", ""),
            "responsibility": dict(recommendation.get("responsibility") or {}),
            "recommended_action": recommendation.get("recommended_action", ""),
            "approval_path": recommendation.get("approval_path", ""),
        }
        item = self.create_switch_suggestion(
            factor_id=str(recommendation.get("factor_id") or ""),
            template_id=str(recommendation.get("target_template_id") or ""),
            regime_key=str(recommendation.get("regime_key") or ""),
            note=note or f"materialized from recommendation {recommendation_id}",
            evidence_context=evidence_context,
        )
        return {
            "ok": True,
            "recommendation": recommendation,
            "item": item,
        }

    def _build_recommendation(self, card: dict[str, Any]) -> dict[str, Any] | None:
        factor_id = str(card.get("factor_id") or "")
        if not factor_id:
            return None
        evidence = dict(card.get("evidence_summary") or {})
        labels = [str(item or "") for item in (evidence.get("recent_responsibility_labels") or []) if str(item or "")]
        primary = str(evidence.get("last_primary_responsibility") or "")
        if primary != "parameter" and "factor_logic_ok_but_param_suspect" not in labels:
            return None

        current = self.get_active_template(factor_id=factor_id, regime_key="") or {}
        current_template_id = str(current.get("template_id") or "")
        templates = self.list_templates(factor_id=factor_id, limit=50)
        if not templates:
            return None

        preferred_roles = ["conservative", "aggressive", "default"]
        if "holding_too_long" in labels:
            preferred_roles = ["aggressive", "conservative", "default"]
        elif any(label in labels for label in ("holding_inefficient", "regime_changed_during_hold")):
            preferred_roles = ["conservative", "default", "aggressive"]

        selected = None
        for role in preferred_roles:
            for item in templates:
                if str(item.get("template_role") or "") != role:
                    continue
                if str(item.get("template_id") or "") == current_template_id:
                    continue
                selected = item
                break
            if selected:
                break
        if not selected:
            for item in templates:
                if str(item.get("template_id") or "") != current_template_id:
                    selected = item
                    break
        if not selected:
            return None

        boundary = self.assess_template_change(
            factor_id=factor_id,
            target_template_id=str(selected.get("template_id") or ""),
            regime_key=str(selected.get("regime_key") or ""),
        )
        priority_score = 1.0
        if primary == "parameter":
            priority_score += 1.0
        if "factor_logic_ok_but_param_suspect" in labels:
            priority_score += 0.75
        if "holding_too_long" in labels or "holding_inefficient" in labels:
            priority_score += 0.35

        return {
            "schema_version": "parameter_template_recommendation.v1",
            "recommendation_id": f"ptr_{factor_id}_{str(selected.get('template_id') or '').replace(':', '_')}",
            "factor_id": factor_id,
            "regime_key": str(selected.get("regime_key") or ""),
            "factor_family": str(card.get("factor_family") or ""),
            "current_template_id": current_template_id,
            "target_template_id": str(selected.get("template_id") or ""),
            "target_template_version": str(selected.get("template_version") or ""),
            "template_role": str(selected.get("template_role") or ""),
            "responsibility": {
                "primary_responsibility": primary,
                "responsibility_labels": labels,
            },
            "evidence_summary": {
                "health_score": evidence.get("health_score"),
                "sample_count": evidence.get("sample_count"),
                "last_primary_responsibility": primary,
                "recent_responsibility_labels": labels,
            },
            "boundary": boundary,
            "approval_path": (
                "governed_apply_switch"
                if boundary.get("recommended_scope") == "online_light"
                else "offline_validation_then_gray_release"
            ),
            "recommended_action": (
                "suggest_switch"
                if boundary.get("recommended_scope") == "online_light"
                else "offline_validate"
            ),
            "reason": self._recommendation_reason(labels=labels, template_role=str(selected.get("template_role") or "")),
            "priority_score": round(priority_score, 6),
            "updated_at_ts": float(card.get("updated_at_ts") or 0.0),
        }

    @staticmethod
    def _recommendation_reason(*, labels: list[str], template_role: str) -> str:
        if "holding_too_long" in labels and template_role == "aggressive":
            return "parameter suspicion suggests shortening reaction window"
        if "holding_inefficient" in labels and template_role == "conservative":
            return "parameter suspicion suggests stabilizing the template before further rollout"
        if "factor_logic_ok_but_param_suspect" in labels:
            return "factor logic looks usable but current parameters appear mismatched"
        return "parameter evidence suggests reviewing an alternative template"

    def activate_template(
        self,
        *,
        factor_id: str,
        template_id: str,
        regime_key: str = "",
        suggestion_id: str = "",
        note: str = "",
        allow_offline_deep: bool = False,
    ) -> dict[str, Any]:
        target = self.get_template(template_id=template_id)
        if not target:
            raise ValueError(f"template not found: {template_id}")
        if suggestion_id and not self._suggestion_is_approved(suggestion_id):
            raise ValueError(f"suggestion not approved: {suggestion_id}")
        current = self.get_active_template(factor_id=factor_id, regime_key=regime_key)
        boundary = self.assess_template_change(
            factor_id=factor_id,
            target_template_id=template_id,
            regime_key=regime_key,
        )
        if boundary.get("recommended_scope") != "online_light" and not allow_offline_deep:
            return {
                "ok": False,
                "blocked": True,
                "error": "offline_deep_validation_required",
                "message": "template requires offline_deep validation and gray release before activation",
                "boundary": boundary,
            }
        verdict = RiskPolicyService().evaluate(
            "switch_parameter_template",
            {
                "required_mode": "governed",
                "template_context": {
                    "factor_id": factor_id,
                    "regime_key": regime_key,
                    "current_template_id": current.get("template_id", "") if current else "",
                    "target_template_id": template_id,
                    "boundary": boundary,
                    "allow_offline_deep": bool(allow_offline_deep),
                },
            },
        ).to_dict()
        if not verdict.get("allowed", False):
            return {
                "ok": False,
                "blocked": True,
                "risk_verdict": verdict,
                "boundary": boundary,
            }
        switch_id = self._new_id("ptsw")
        now = time.time()
        context = {
            "note": note,
            "factor_id": factor_id,
            "regime_key": regime_key,
            "old_template_id": current.get("template_id", "") if current else "",
            "new_template_id": template_id,
            "boundary": boundary,
            "allow_offline_deep": bool(allow_offline_deep),
        }
        with self._conn() as conn:
            _execute(
                conn,
                """
                UPDATE parameter_template_registry
                SET active=CASE WHEN template_id=? THEN 1 ELSE 0 END, updated_at=?
                WHERE factor_id=? AND regime_key=?
                """,
                (template_id, now, factor_id, regime_key),
            )
            _execute(
                conn,
                """
                INSERT INTO parameter_template_active
                (factor_id, regime_key, template_id, template_version, status, suggestion_id,
                 context_json, activated_at, updated_at)
                VALUES (?, ?, ?, ?, 'active', ?, ?, ?, ?)
                ON CONFLICT(factor_id, regime_key) DO UPDATE SET
                    template_id=excluded.template_id,
                    template_version=excluded.template_version,
                    status=excluded.status,
                    suggestion_id=excluded.suggestion_id,
                    context_json=excluded.context_json,
                    activated_at=excluded.activated_at,
                    updated_at=excluded.updated_at
                """,
                (
                    factor_id,
                    regime_key,
                    template_id,
                    target["template_version"],
                    suggestion_id,
                    json.dumps(context, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
            _execute(
                conn,
                """
                INSERT INTO parameter_template_switch_log
                (switch_id, factor_id, regime_key, old_template_id, new_template_id,
                 suggestion_id, risk_verdict_json, context_json, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'applied', ?)
                """,
                (
                    switch_id,
                    factor_id,
                    regime_key,
                    current.get("template_id", "") if current else "",
                    template_id,
                    suggestion_id,
                    json.dumps(verdict, ensure_ascii=False, default=str),
                    json.dumps(context, ensure_ascii=False, default=str),
                    now,
                ),
            )
            conn.commit()
        RuleEvolutionGovernor(str(self.db_path)).log_application(
            scope_type="parameter_template",
            scope_key=f"{factor_id}:{regime_key or 'default'}",
            action="switch_parameter_template",
            bias_multiplier=1.0,
            old_weight=0.0,
            new_weight=0.0,
            suggestion_ids=[suggestion_id] if suggestion_id else [],
            cycle_ts=now,
            details={
                "factor_id": factor_id,
                "regime_key": regime_key,
                "old_template_id": current.get("template_id", "") if current else "",
                "new_template_id": template_id,
                "switch_id": switch_id,
                "boundary": boundary,
                "note": note,
            },
        )
        self.sync_runtime_config()
        return {
            "ok": True,
            "switch_id": switch_id,
            "factor_id": factor_id,
            "regime_key": regime_key,
            "old_template_id": current.get("template_id", "") if current else "",
            "new_template_id": template_id,
            "risk_verdict": verdict,
            "boundary": boundary,
        }

    def get_template(
        self,
        *,
        template_id: str = "",
        factor_id: str = "",
        template_version: str = "",
        regime_key: str = "",
    ) -> dict[str, Any] | None:
        if template_id:
            items = self._list_persisted(template_id=template_id)
            if items:
                return items[0]
        if factor_id and template_version:
            items = self._list_persisted(
                factor_id=factor_id,
                template_version=template_version,
                regime=regime_key,
            )
            if items:
                return items[0]
        for item in self.list_templates(factor_id=factor_id or None, regime=regime_key or None, limit=500):
            if template_id and item["template_id"] == template_id:
                return item
            if factor_id and template_version and item["factor_id"] == factor_id and item["template_version"] == template_version:
                return item
        return None

    def get_active_template(self, *, factor_id: str, regime_key: str = "") -> dict[str, Any] | None:
        with self._conn() as conn:
            row = _execute(
                conn,
                """
                SELECT * FROM parameter_template_active
                WHERE factor_id=? AND regime_key=?
                """,
                (factor_id, regime_key),
            ).fetchone()
        if not row:
            return None
        return {
            **dict(row),
            "context": _loads(row["context_json"], {}),
        }

    def list_switch_logs(self, *, factor_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as conn:
            if factor_id:
                rows = _execute(
                    conn,
                    """
                    SELECT * FROM parameter_template_switch_log
                    WHERE factor_id=?
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (factor_id, limit),
                ).fetchall()
            else:
                rows = _execute(
                    conn,
                    """
                    SELECT * FROM parameter_template_switch_log
                    ORDER BY created_at DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
        items = []
        for row in rows:
            item = dict(row)
            item["risk_verdict"] = _loads(item.pop("risk_verdict_json", "{}"), {})
            item["context"] = _loads(item.pop("context_json", "{}"), {})
            items.append(item)
        return items

    def assess_template_change(
        self,
        *,
        factor_id: str,
        target_template_id: str,
        regime_key: str = "",
    ) -> dict[str, Any]:
        current_active = self.get_active_template(factor_id=factor_id, regime_key=regime_key)
        current_template = (
            self.get_template(template_id=str(current_active.get("template_id") or ""))
            if current_active
            else None
        )
        target_template = self.get_template(template_id=target_template_id)
        if not target_template:
            raise ValueError(f"template not found: {target_template_id}")
        reasons: list[str] = []
        scope = "online_light"
        if factor_id not in self.RUNTIME_TUNABLE_FACTORS:
            scope = "offline_deep"
            reasons.append("factor_not_runtime_tunable")
        if current_template:
            if str(current_template.get("formula_version") or "") != str(target_template.get("formula_version") or ""):
                scope = "offline_deep"
                reasons.append("formula_version_changed")
            if str(current_template.get("factor_family") or "") != str(target_template.get("factor_family") or ""):
                scope = "offline_deep"
                reasons.append("factor_family_changed")
            if self._max_parameter_delta(
                current_template.get("parameters") or {},
                target_template.get("parameters") or {},
            ) > 0.35:
                scope = "offline_deep"
                reasons.append("parameter_delta_too_large")
        if str(target_template.get("template_role") or "") not in {"default", "conservative", "aggressive"}:
            scope = "offline_deep"
            reasons.append("unsupported_template_role")
        if not reasons:
            reasons.append("fits_runtime_guardrail")
        return {
            "factor_id": factor_id,
            "regime_key": regime_key,
            "current_template_id": str(current_active.get("template_id") or "") if current_active else "",
            "target_template_id": target_template_id,
            "recommended_scope": scope,
            "reasons": reasons,
            "current_template": current_template,
            "target_template": target_template,
        }

    def _list_persisted(
        self,
        *,
        template_id: str = "",
        factor_id: str | None = None,
        regime: str | None = None,
        template_version: str = "",
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM parameter_template_registry WHERE 1=1"
        params: list[Any] = []
        if template_id:
            sql += " AND template_id=?"
            params.append(template_id)
        if factor_id:
            sql += " AND factor_id=?"
            params.append(factor_id)
        if regime is not None:
            sql += " AND regime_key=?"
            params.append(regime)
        if template_version:
            sql += " AND template_version=?"
            params.append(template_version)
        sql += " ORDER BY updated_at DESC, created_at DESC"
        with self._conn() as conn:
            rows = _execute(conn, sql, tuple(params)).fetchall()
        return [self._parse_registry_row(row) for row in rows]

    @staticmethod
    def _parse_registry_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "schema_version": "parameter_template.v1",
            "template_id": str(row["template_id"] or ""),
            "factor_id": str(row["factor_id"] or ""),
            "regime_key": str(row["regime_key"] or ""),
            "factor_family": str(row["factor_family"] or ""),
            "template_version": str(row["template_version"] or ""),
            "template_role": str(row["template_role"] or "default"),
            "formula_version": str(row["formula_version"] or ""),
            "base_parameter_version": str(row["base_parameter_version"] or ""),
            "parameters": _loads(row["parameters_json"], {}),
            "applicable_regimes": _loads(row["applicable_regimes_json"], []),
            "avoid_regimes": _loads(row["avoid_regimes_json"], []),
            "holding_profile_hint": _loads(row["holding_profile_hint_json"], {}),
            "tuning_bias": str((_loads(row["evidence_json"], {}) or {}).get("tuning_bias") or ""),
            "evidence": _loads(row["evidence_json"], {}),
            "source": str(row["source"] or "derived"),
            "active": bool(row["active"]),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }

    def _normalize_template(self, template: dict[str, Any], *, source: str) -> dict[str, Any]:
        factor_id = str(template.get("factor_id") or "")
        template_version = str(template.get("template_version") or "")
        if not factor_id or not template_version:
            raise ValueError("factor_id and template_version are required")
        regime_key = str(template.get("regime_key") or "")
        template_id = str(template.get("template_id") or f"{factor_id}:{template_version}:{regime_key or 'default'}")
        evidence = deepcopy(template.get("evidence") or {})
        tuning_bias = str(template.get("tuning_bias") or evidence.get("tuning_bias") or "neutral")
        evidence["tuning_bias"] = tuning_bias
        return {
            "schema_version": "parameter_template.v1",
            "template_id": template_id,
            "factor_id": factor_id,
            "regime_key": regime_key,
            "factor_family": str(template.get("factor_family") or ""),
            "template_version": template_version,
            "template_role": str(template.get("template_role") or "default"),
            "formula_version": str(template.get("formula_version") or ""),
            "base_parameter_version": str(template.get("base_parameter_version") or "default.v1"),
            "parameters": deepcopy(template.get("parameters") or {}),
            "applicable_regimes": list(template.get("applicable_regimes") or []),
            "avoid_regimes": list(template.get("avoid_regimes") or []),
            "holding_profile_hint": deepcopy(template.get("holding_profile_hint") or {}),
            "tuning_bias": tuning_bias,
            "evidence": evidence,
            "source": source,
        }

    def _build_templates(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        base = card.get("parameters") or {}
        expected = list(card.get("expected_regimes") or [])
        weak = list(card.get("weak_regimes") or [])
        holding_profile = dict(card.get("expected_holding_profile") or {})
        formula_version = str(card.get("formula_version") or "")
        base_parameter_version = str(card.get("parameter_version") or "default.v1")
        evidence_summary = card.get("evidence_summary") or {}
        templates = [
            self._template(
                card,
                template_version="default.v1",
                template_role="default",
                parameters=base,
                applicable_regimes=expected,
                avoid_regimes=weak,
                tuning_bias="neutral",
                holding_profile_hint=holding_profile,
                formula_version=formula_version,
                base_parameter_version=base_parameter_version,
                evidence_summary=evidence_summary,
            ),
            self._template(
                card,
                template_version="conservative.v1",
                template_role="conservative",
                parameters=self._mutate_parameters(base, mode="conservative"),
                applicable_regimes=weak or ["low_vol"],
                avoid_regimes=expected,
                tuning_bias="stability",
                holding_profile_hint=self._adjust_holding_profile(holding_profile, mode="conservative"),
                formula_version=formula_version,
                base_parameter_version=base_parameter_version,
                evidence_summary=evidence_summary,
            ),
            self._template(
                card,
                template_version="aggressive.v1",
                template_role="aggressive",
                parameters=self._mutate_parameters(base, mode="aggressive"),
                applicable_regimes=expected[:1] + [reg for reg in expected[1:] if reg not in weak] or ["trend"],
                avoid_regimes=weak,
                tuning_bias="responsiveness",
                holding_profile_hint=self._adjust_holding_profile(holding_profile, mode="aggressive"),
                formula_version=formula_version,
                base_parameter_version=base_parameter_version,
                evidence_summary=evidence_summary,
            ),
        ]
        return templates

    def _manual_templates_for_card(self, card: dict[str, Any]) -> list[dict[str, Any]]:
        factor_id = str(card.get("factor_id") or "")
        specs = _MANUAL_TEMPLATE_LIBRARY.get(factor_id) or []
        if not specs:
            return []
        evidence_summary = card.get("evidence_summary") or {}
        result = []
        for spec in specs:
            tuning_bias = str(spec.get("tuning_bias") or "neutral")
            result.append(
                {
                    "schema_version": "parameter_template.v1",
                    "template_id": f"{factor_id}:{spec['template_version']}:{spec.get('regime_key') or 'default'}",
                    "factor_id": factor_id,
                    "regime_key": str(spec.get("regime_key") or ""),
                    "factor_family": str(card.get("factor_family") or ""),
                    "template_version": str(spec["template_version"]),
                    "template_role": str(spec["template_role"]),
                    "formula_version": str(card.get("formula_version") or ""),
                    "base_parameter_version": str(card.get("parameter_version") or "default.v1"),
                    "parameters": deepcopy(spec.get("parameters") or {}),
                    "applicable_regimes": list(spec.get("applicable_regimes") or []),
                    "avoid_regimes": list(spec.get("avoid_regimes") or []),
                    "holding_profile_hint": deepcopy(spec.get("holding_profile_hint") or {}),
                    "tuning_bias": tuning_bias,
                    "evidence": {
                        "derived_from_factor_card": False,
                        "manual_template": True,
                        "tuning_bias": tuning_bias,
                        "last_primary_responsibility": str(
                            evidence_summary.get("last_primary_responsibility") or ""
                        ),
                        "recent_responsibility_labels": list(
                            evidence_summary.get("recent_responsibility_labels") or []
                        ),
                    },
                    "source": "manual_library",
                }
            )
        return result

    @staticmethod
    def _template(
        card: dict[str, Any],
        *,
        template_version: str,
        template_role: str,
        parameters: dict[str, Any],
        applicable_regimes: list[str],
        avoid_regimes: list[str],
        tuning_bias: str,
        holding_profile_hint: dict[str, Any],
        formula_version: str,
        base_parameter_version: str,
        evidence_summary: dict[str, Any],
    ) -> dict[str, Any]:
        regime_key = ""
        return {
            "schema_version": "parameter_template.v1",
            "template_id": f"{card['factor_id']}:{template_version}:{regime_key or 'default'}",
            "factor_id": str(card.get("factor_id") or ""),
            "regime_key": regime_key,
            "factor_family": str(card.get("factor_family") or ""),
            "template_version": template_version,
            "template_role": template_role,
            "formula_version": formula_version,
            "base_parameter_version": base_parameter_version,
            "parameters": parameters,
            "applicable_regimes": applicable_regimes,
            "avoid_regimes": avoid_regimes,
            "holding_profile_hint": holding_profile_hint,
            "tuning_bias": tuning_bias,
            "evidence": {
                "derived_from_factor_card": True,
                "tuning_bias": tuning_bias,
                "last_primary_responsibility": str(
                    evidence_summary.get("last_primary_responsibility") or ""
                ),
                "recent_responsibility_labels": list(
                    evidence_summary.get("recent_responsibility_labels") or []
                ),
            },
            "source": "derived",
        }

    @staticmethod
    def _adjust_holding_profile(profile: dict[str, Any], *, mode: str) -> dict[str, Any]:
        item = deepcopy(profile)
        min_bars = int(item.get("min_bars", 0) or 0)
        max_bars = int(item.get("max_bars", 0) or 0)
        if mode == "conservative":
            if min_bars > 0:
                item["min_bars"] = max(1, round(min_bars * 1.2))
            if max_bars > 0:
                item["max_bars"] = max(item.get("min_bars", 1), round(max_bars * 1.25))
        elif mode == "aggressive":
            if min_bars > 0:
                item["min_bars"] = max(1, round(min_bars * 0.8))
            if max_bars > 0:
                item["max_bars"] = max(item.get("min_bars", 1), round(max_bars * 0.75))
        return item

    @staticmethod
    def _mutate_parameters(parameters: dict[str, Any], *, mode: str) -> dict[str, Any]:
        mutated: dict[str, Any] = {}
        for key, value in (parameters or {}).items():
            mutated[key] = ParameterTemplateService._mutate_value(key, value, mode=mode)
        return mutated

    @staticmethod
    def _mutate_value(key: str, value: Any, *, mode: str) -> Any:
        if isinstance(value, list):
            return [ParameterTemplateService._mutate_value(key, item, mode=mode) for item in value]
        if not isinstance(value, (int, float)):
            return value
        key_text = str(key or "").lower()
        factor = 1.0
        if any(token in key_text for token in ("length", "window", "lookback", "bars")):
            factor = 1.25 if mode == "conservative" else 0.8
        elif any(token in key_text for token in ("threshold", "band", "multiplier", "stddev")):
            factor = 1.1 if mode == "conservative" else 0.9
        if isinstance(value, int):
            return max(1, round(value * factor))
        return round(float(value) * factor, 6)

    def _suggestion_is_approved(self, suggestion_id: str) -> bool:
        with self._conn() as conn:
            row = _execute(
                conn,
                """
                SELECT status FROM policy_suggestion WHERE suggestion_id=?
                """,
                (suggestion_id,),
            ).fetchone()
        return bool(row and str(row["status"] or "") == "approved")

    @staticmethod
    def _max_parameter_delta(current: dict[str, Any], target: dict[str, Any]) -> float:
        max_delta = 0.0
        for key in set(current) | set(target):
            cur = current.get(key)
            nxt = target.get(key)
            if not isinstance(cur, (int, float)) or not isinstance(nxt, (int, float)):
                if cur != nxt:
                    max_delta = max(max_delta, 1.0)
                continue
            denom = max(abs(float(cur)), 1.0)
            delta = abs(float(nxt) - float(cur)) / denom
            max_delta = max(max_delta, delta)
        return max_delta
