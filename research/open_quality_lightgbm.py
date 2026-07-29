from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR, STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact


MODEL_TYPE = "open_quality_lightgbm"
MODEL_VERSION = "3.0"
FEATURE_SCHEMA_VERSION = "pit.v2.open_lineage"

FEATURE_NAMES = [
    "action_score",
    "abs_action_score",
    "direction",
    "same_direction_open_count",
    "pyramid_depth",
    "is_pyramid",
    "recent_same_direction_5m",
    "recent_same_direction_15m",
    "recent_same_direction_30m",
    "same_direction_api_volume_before",
    "open_position_count_before",
    "event_near",
    "event_multiplier",
    "spread",
    "quote_fresh",
    "quote_age_seconds",
    "bar_body_ratio",
    "bar_close_location",
    "bar_range_points",
    "factor_conflict_ratio",
    "positive_contribution_abs",
    "negative_contribution_abs",
    "tactical_score",
    "macro_score",
    "alpha_score",
    "n_active_factors",
    "n_active_alpha_factors",
    "n_abstain_factors",
]


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw))
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
        if math.isnan(out) or math.isinf(out):
            return default
        return out
    except Exception:
        return default


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _dependency_error() -> str:
    try:
        import joblib  # noqa: F401
        import lightgbm  # noqa: F401
        import pandas  # noqa: F401
        import sklearn  # noqa: F401
    except Exception as exc:
        return str(exc)
    return ""


def _allowed_supervised(contract: dict[str, Any]) -> bool:
    return bool(contract.get("model_ready")) and "supervised_training" in set(contract.get("allowed_uses") or [])


def _label_from_open_outcome(label: dict[str, Any]) -> int:
    outcome = str(label.get("outcome_label") or "").lower()
    if outcome in {"good_win", "good_loss"}:
        return 1
    if outcome in {"bad_loss", "lucky_win", "small_loss", "loss"}:
        return 0
    return 1 if _safe_float(label.get("pnl")) > 0 else 0


def _rule_baseline_label(features: dict[str, float]) -> int:
    # Mirrors the current conservative rule intuition: stronger score and no
    # crowded same-direction pyramid is treated as a better open.
    return 1 if features["abs_action_score"] >= 0.55 and features["same_direction_open_count"] < 2 else 0


def _features_from_sample(row: Any) -> dict[str, float]:
    features_json = _loads(row["features_json"], {})
    action = features_json.get("action") or {}
    entry_cluster = features_json.get("entry_cluster") or action.get("entry_cluster") or {}
    portfolio = features_json.get("portfolio_exposure") or action.get("portfolio_exposure") or {}
    micro = features_json.get("market_micro_context") or action.get("market_micro_context") or {}
    bar = features_json.get("bar_context") or action.get("bar_context") or {}
    event = features_json.get("event_context") or action.get("event_sizing") or {}
    decision_quality = features_json.get("decision_quality_context") or action.get("decision_quality_context") or {}
    recent = entry_cluster.get("recent_same_direction_entries") or action.get("recent_same_direction_entries") or {}
    action_score = _safe_float(features_json.get("action_score"), _safe_float(action.get("score")))
    out = {
        "action_score": action_score,
        "abs_action_score": abs(action_score),
        "direction": _safe_float(action.get("direction")),
        "same_direction_open_count": _safe_float(action.get("same_direction_open_count"), _safe_float(entry_cluster.get("same_direction_open_count_before"))),
        "pyramid_depth": _safe_float(entry_cluster.get("pyramid_depth")),
        "is_pyramid": 1.0 if bool(entry_cluster.get("is_pyramid")) else 0.0,
        "recent_same_direction_5m": _safe_float(recent.get("5m")),
        "recent_same_direction_15m": _safe_float(recent.get("15m")),
        "recent_same_direction_30m": _safe_float(recent.get("30m")),
        "same_direction_api_volume_before": _safe_float(entry_cluster.get("same_direction_api_volume_before"), _safe_float(portfolio.get("same_direction_api_volume_before"))),
        "open_position_count_before": _safe_float(entry_cluster.get("open_position_count_before"), _safe_float(portfolio.get("open_position_count_before"))),
        "event_near": 1.0 if bool(event.get("event_near")) else 0.0,
        "event_multiplier": _safe_float(event.get("multiplier"), 1.0),
        "spread": _safe_float(micro.get("spread"), _safe_float(action.get("spread"))),
        "quote_fresh": 1.0 if bool(micro.get("quote_fresh", True)) else 0.0,
        "quote_age_seconds": _safe_float(micro.get("quote_age_seconds")),
        "bar_body_ratio": _safe_float(bar.get("body_ratio")),
        "bar_close_location": _safe_float(bar.get("close_location")),
        "bar_range_points": _safe_float(bar.get("range_points")),
        "factor_conflict_ratio": _safe_float(decision_quality.get("factor_conflict_ratio")),
        "positive_contribution_abs": _safe_float(decision_quality.get("positive_contribution_abs")),
        "negative_contribution_abs": _safe_float(decision_quality.get("negative_contribution_abs")),
        "tactical_score": _safe_float(action.get("tactical_score"), _safe_float(decision_quality.get("tactical_score"))),
        "macro_score": _safe_float(action.get("macro_score"), _safe_float(decision_quality.get("macro_score"))),
        "alpha_score": _safe_float(action.get("alpha_score"), _safe_float(decision_quality.get("alpha_score"), action_score)),
        "n_active_factors": _safe_float(action.get("n_active_factors"), _safe_float(decision_quality.get("n_active_factors"))),
        "n_active_alpha_factors": _safe_float(action.get("n_active_alpha_factors"), _safe_float(decision_quality.get("n_active_alpha_factors"))),
        "n_abstain_factors": _safe_float(action.get("n_abstain_factors"), _safe_float(decision_quality.get("n_abstain_factors"))),
    }
    return {name: out.get(name, 0.0) for name in FEATURE_NAMES}


def _sample_from_row(row: Any) -> dict[str, Any] | None:
    label_json = _loads(row["label_json"], {})
    contract = _loads(row["evidence_contract_json"], {})
    if not _allowed_supervised(contract):
        return None
    if str(label_json.get("label") or "") != "open_outcome":
        return None
    features_json = _loads(row["features_json"], {})
    action = dict(features_json.get("action") or {})
    decision_quality = dict(
        features_json.get("decision_quality_context")
        or action.get("decision_quality_context")
        or {}
    )
    bar_context = dict(features_json.get("bar_context") or action.get("bar_context") or {})
    micro = dict(features_json.get("market_micro_context") or action.get("market_micro_context") or {})
    factor_roles = dict(action.get("factor_roles") or decision_quality.get("factor_roles") or {})
    config_hash = str(row["config_hash"] or "")
    composer_version = str(action.get("composer_version") or decision_quality.get("composer_version") or "")
    strategy_version = f"factor_pipeline_v4:{composer_version or 'unknown'}"
    factor_universe = sorted(factor_roles)
    lineage_payload = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "strategy_version": strategy_version,
        "decision_quality_schema": str(decision_quality.get("schema_version") or ""),
        "bar_schema": str(bar_context.get("schema_version") or ""),
    }
    lineage_id = hashlib.sha256(
        json.dumps(lineage_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    quality_errors = []
    if not config_hash:
        quality_errors.append("missing_config_hash")
    if not composer_version:
        quality_errors.append("missing_composer_version")
    if str(decision_quality.get("schema_version") or "") != "decision_quality_context.v1":
        quality_errors.append("invalid_decision_quality_schema")
    if not factor_roles or int(_safe_float(decision_quality.get("n_active_alpha_factors"))) <= 0:
        quality_errors.append("missing_active_alpha_universe")
    if bar_context.get("complete") is not True:
        quality_errors.append("decision_bar_not_complete")
    if micro.get("quote_fresh") is False:
        quality_errors.append("stale_entry_quote")
    features = _features_from_sample(row)
    return {
        "sample_id": str(row["sample_id"] or ""),
        "decision_id": str(row["decision_id"] or ""),
        "trade_id": str(row["trade_id"] or ""),
        "position_id": str(row["position_id"] or ""),
        "created_at": _safe_float(row["event_ts"] or row["created_at"]),
        "outcome_label": str(label_json.get("outcome_label") or ""),
        "pnl": _safe_float(label_json.get("pnl")),
        "label": _label_from_open_outcome(label_json),
        "rule_label": _rule_baseline_label(features),
        "features": features,
        "config_hash": config_hash,
        "strategy_version": strategy_version,
        "lineage_id": lineage_id,
        "lineage": {**lineage_payload, "observed_factor_universe": factor_universe},
        "quality_errors": quality_errors,
    }


class OpenQualityLightGBMService:
    """Shadow-only entry/open quality model trained from matured open outcomes."""

    def __init__(self, *, db_path: str | Path = STATE_DB, artifact_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts" / MODEL_TYPE
        self.last_data_quality: dict[str, Any] = {}
        self._ensure_audit_table()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        rendered = self._sql(sql)
        if self._use_pg() and is_state_schema_write_sql(rendered):
            return validate_runtime_state_schema(conn, rendered)
        if params is None:
            return conn.execute(rendered)
        return conn.execute(rendered, tuple(params))

    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = __import__("sqlite3").Row
        return conn

    def _ensure_audit_table(self) -> None:
        conn = self._conn()
        try:
            self._execute(
                conn,
                """
                CREATE TABLE IF NOT EXISTS open_quality_shadow_audit (
                    inference_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_path TEXT DEFAULT '',
                    sample_id TEXT DEFAULT '',
                    decision_id TEXT DEFAULT '',
                    trade_id TEXT DEFAULT '',
                    position_id TEXT DEFAULT '',
                    mode TEXT DEFAULT 'shadow',
                    quality_score REAL DEFAULT 0.0,
                    risk_score REAL DEFAULT 0.0,
                    prediction INTEGER DEFAULT 0,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """,
            )
            self._execute(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_open_quality_shadow_audit_created
                ON open_quality_shadow_audit(created_at)
                """,
            )
            self._execute(
                conn,
                """
                CREATE INDEX IF NOT EXISTS idx_open_quality_shadow_audit_position
                ON open_quality_shadow_audit(position_id, created_at)
                """,
            )
            conn.commit()
        finally:
            conn.close()

    def load_samples(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT *
                FROM (
                    SELECT *
                    FROM autonomous_learning_sample
                    WHERE sample_type='shadow_open_decision'
                      AND label_status='matured'
                    ORDER BY event_ts DESC, created_at DESC
                    LIMIT ?
                ) recent
                ORDER BY event_ts ASC, created_at ASC
                """,
                (int(limit),),
            ).fetchall()
            candidates = []
            for row in rows:
                item = _sample_from_row(row)
                if item is not None:
                    candidates.append(item)
            valid = [item for item in candidates if not item.get("quality_errors")]
            latest_lineage = str(valid[-1].get("lineage_id") or "") if valid else ""
            samples = [item for item in valid if str(item.get("lineage_id") or "") == latest_lineage]
            rejection_counts: dict[str, int] = {}
            for item in candidates:
                for reason in item.get("quality_errors") or []:
                    rejection_counts[str(reason)] = rejection_counts.get(str(reason), 0) + 1
            self.last_data_quality = {
                "schema_version": "model_training_data_quality.v1",
                "candidate_count": len(candidates),
                "valid_count": len(valid),
                "selected_count": len(samples),
                "selected_lineage_id": latest_lineage,
                "selected_lineage": dict(samples[-1].get("lineage") or {}) if samples else {},
                "selected_config_hashes": sorted({str(item.get("config_hash") or "") for item in samples}),
                "excluded_other_lineage_count": len(valid) - len(samples),
                "rejection_counts": rejection_counts,
            }
            return samples
        finally:
            conn.close()

    def train(
        self,
        *,
        limit: int = 2000,
        holdout_ratio: float = 0.25,
        min_samples: int = 30,
        register: bool = True,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> dict[str, Any]:
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "error": "dependency_missing", "detail": dep_error}

        import joblib
        import lightgbm as lgb
        import pandas as pd
        from sklearn.metrics import accuracy_score, balanced_accuracy_score, recall_score, roc_auc_score

        samples = self.load_samples(limit=limit)
        from backend.services.parity_replay import load_parity_learning_samples

        replay_samples = load_parity_learning_samples("open")
        if len(samples) < 2 or len(samples) + len(replay_samples) < int(min_samples):
            return {
                "ok": False, "model_type": MODEL_TYPE, "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples), "replay_sample_count": len(replay_samples),
                "data_quality": dict(self.last_data_quality),
                "error": "insufficient_open_outcome_samples",
            }
        labels = [int(item["label"]) for item in samples]
        if len(set(labels)) < 2:
            return {"ok": False, "model_type": MODEL_TYPE, "model_version": MODEL_VERSION, "sample_count": len(samples), "error": "single_class_training_data"}

        holdout_count = max(1, int(round(len(samples) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_count = min(holdout_count, len(samples) - 1)
        train_samples = samples[:-holdout_count]
        holdout_samples = samples[-holdout_count:]

        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]

        def _metrics(items: list[dict[str, Any]], y_true: list[int], probs: Any) -> dict[str, Any]:
            preds = [1 if float(x) >= 0.5 else 0 for x in probs]
            majority_label = 1 if sum(y_true) / max(len(y_true), 1) >= 0.5 else 0
            majority_preds = [majority_label] * len(y_true)
            rule_preds = [int(item["rule_label"]) for item in items]
            auc = None
            if len(set(y_true)) > 1:
                try:
                    auc = round(float(roc_auc_score(y_true, probs)), 6)
                except Exception:
                    auc = None
            accuracy = round(float(accuracy_score(y_true, preds)), 6) if y_true else None
            rule_accuracy = round(float(accuracy_score(y_true, rule_preds)), 6) if y_true else None
            majority_accuracy = round(float(accuracy_score(y_true, majority_preds)), 6) if y_true else None
            return {
                "count": len(y_true),
                "accuracy": accuracy,
                "balanced_accuracy": round(float(balanced_accuracy_score(y_true, preds)), 6) if y_true else None,
                "auc": auc,
                "rule_accuracy": rule_accuracy,
                "majority_baseline_accuracy": majority_accuracy,
                "model_lift_vs_rule": round(float((accuracy or 0.0) - (rule_accuracy or 0.0)), 6),
                "rule_lift_vs_majority": round(float((rule_accuracy or 0.0) - (majority_accuracy or 0.0)), 6),
                "negative_recall": round(float(recall_score(y_true, preds, pos_label=0, zero_division=0)), 6),
                "positive_recall": round(float(recall_score(y_true, preds, pos_label=1, zero_division=0)), 6),
                "positive_rate": round(sum(y_true) / max(len(y_true), 1), 6),
            }

        def _fit(
            training: list[dict[str, Any]],
            weights: list[float] | None = None,
        ):
            fitted = lgb.LGBMClassifier(
                objective="binary",
                n_estimators=140,
                learning_rate=0.04,
                num_leaves=15,
                min_child_samples=max(1, min(20, len(training) // 4)),
                subsample=0.9,
                colsample_bytree=0.9,
                class_weight="balanced",
                random_state=42,
                n_jobs=1,
                verbosity=-1,
            )
            x = pd.DataFrame([item["features"] for item in training], columns=FEATURE_NAMES)
            fitted.fit(x, [int(item["label"]) for item in training], sample_weight=weights)
            return fitted, x

        baseline_model, baseline_x = _fit(train_samples)
        baseline_holdout = _metrics(
            holdout_samples,
            y_holdout,
            baseline_model.predict_proba(x_holdout)[:, 1],
        )
        replay_weight = min(1.0, len(train_samples) / max(1, len(replay_samples)))
        augmented_samples = train_samples + replay_samples
        augmented_model, augmented_x = _fit(
            augmented_samples,
            [1.0] * len(train_samples) + [replay_weight] * len(replay_samples),
        )
        augmented_holdout = _metrics(
            holdout_samples,
            y_holdout,
            augmented_model.predict_proba(x_holdout)[:, 1],
        )
        compare_names = ("accuracy", "balanced_accuracy", "auc")
        comparable = [
            name for name in compare_names
            if baseline_holdout.get(name) is not None and augmented_holdout.get(name) is not None
        ]
        use_augmented = bool(replay_samples) and bool(comparable) and all(
            float(augmented_holdout[name]) >= float(baseline_holdout[name])
            for name in comparable
        ) and any(
            float(augmented_holdout[name]) > float(baseline_holdout[name])
            for name in comparable
        )
        model = augmented_model if use_augmented else baseline_model
        selected_train = augmented_samples if use_augmented else train_samples
        selected_x = augmented_x if use_augmented else baseline_x
        train_prob = model.predict_proba(selected_x)[:, 1]
        holdout_prob = model.predict_proba(x_holdout)[:, 1]

        feature_importance = [
            {"feature": name, "importance": int(value)}
            for name, value in sorted(zip(FEATURE_NAMES, model.feature_importances_), key=lambda item: (-int(item[1]), item[0]))
        ]
        metrics = {
            "train": _metrics(
                selected_train,
                [int(item["label"]) for item in selected_train],
                train_prob,
            ),
            "holdout": _metrics(holdout_samples, y_holdout, holdout_prob),
            "split": "time_ordered_grouped_purged",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "sample_count": len(samples),
            "replay_sample_count": len(replay_samples),
            "real_holdout_count": len(holdout_samples),
            "training_sources": {
                "real_train_samples": len(train_samples),
                "historical_replay_samples": len(replay_samples),
                "historical_replay_weight": replay_weight,
                "selected": "real_plus_replay" if use_augmented else "real_baseline",
            },
            "augmentation_comparison": {
                "baseline_real_holdout": baseline_holdout,
                "augmented_real_holdout": augmented_holdout,
                "selected": "augmented" if use_augmented else "baseline",
            },
            "feature_count": len(FEATURE_NAMES),
            "label_distribution": {"negative": labels.count(0), "positive": labels.count(1)},
            "safe_for_live_trading": False,
            "data_quality": dict(self.last_data_quality),
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, model_path)
        artifact = {
            "schema_version": "open_quality_lightgbm_artifact.v2",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "label": "acceptable_open_outcome",
            "sample_window": {"limit": int(limit), "sample_count": len(samples)},
            "training_lineage": dict(self.last_data_quality),
            "metrics": metrics,
            "explainability": {
                "feature_importance": feature_importance,
                "summary": "Shadow-only open quality model trained from matured open outcome samples with time-ordered holdout and rule/majority baselines.",
            },
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
            },
            "guardrails": [
                "MUST NOT place orders",
                "MUST NOT close positions",
                "MUST NOT change RiskPolicyService limits",
                "MUST log every inference before downstream review",
            ],
        }
        metadata_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
        artifact["artifact_sha256"] = _sha256(metadata_path)

        registry_version = None
        if register:
            from research.model_registry import ModelRegistry

            registry_version = ModelRegistry(db_path=registry_db_path).register(
                MODEL_TYPE,
                artifact_path=str(metadata_path),
                params={"model_version": MODEL_VERSION, "label": artifact["label"], "feature_names": FEATURE_NAMES, "safe_for_live_trading": False},
                metrics={
                    "sample_count": len(samples),
                    "feature_count": len(FEATURE_NAMES),
                    "holdout_accuracy": metrics["holdout"]["accuracy"],
                    "holdout_rule_accuracy": metrics["holdout"]["rule_accuracy"],
                    "holdout_majority_accuracy": metrics["holdout"]["majority_baseline_accuracy"],
                    "model_lift_vs_rule": metrics["holdout"]["model_lift_vs_rule"],
                    "safe_for_live_trading": False,
                },
                symbol=symbol,
                timeframe=timeframe,
            ).to_dict()

        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "artifact_path": str(metadata_path),
            "artifact_sha256": artifact["artifact_sha256"],
            "model_file": str(model_path),
            "metrics": metrics,
            "explainability": artifact["explainability"],
            "capabilities": artifact["capabilities"],
            "registry_version": registry_version,
        }

    def latest_artifact_path(self) -> str:
        paths = sorted(self.artifact_dir.glob(f"{MODEL_TYPE}_*.json"), key=lambda path: path.stat().st_mtime, reverse=True)
        return str(paths[0]) if paths else ""

    def _existing_shadow_sample_ids(self, *, artifact_path: str) -> set[str]:
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT sample_id
                FROM open_quality_shadow_audit
                WHERE model_type=? AND artifact_path=?
                """,
                (MODEL_TYPE, artifact_path),
            ).fetchall()
            return {
                str(row["sample_id"] or "")
                for row in rows
                if str(row["sample_id"] or "")
            }
        finally:
            conn.close()

    def score_open_context(
        self,
        context: dict[str, Any],
        *,
        artifact_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Score a pre-order context using the exact PIT v2 training schema."""
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "error": "dependency_missing", "detail": dep_error}
        import joblib
        import pandas as pd

        path = Path(str(artifact_path or self.latest_artifact_path()))
        if not path.exists():
            return {"ok": False, "error": "artifact_missing"}
        artifact = json.loads(path.read_text(encoding="utf-8"))
        if str(artifact.get("feature_schema_version") or "") != FEATURE_SCHEMA_VERSION:
            return {"ok": False, "error": "artifact_feature_schema_not_pit_v2"}
        permission = validate_model_artifact(
            artifact, model_type=MODEL_TYPE, db_path=self.db_path,
            context={"mode": "demo_influence_candidate", "operation": "open_quality_live_score"},
        )
        if not permission.get("ok"):
            return {"ok": False, "error": "model_permission_violation", "permission": permission}
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing"}
        bundle = joblib.load(model_file)
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        features = _features_from_sample({"features_json": json.dumps(context or {}, ensure_ascii=False)})
        frame = pd.DataFrame([[features.get(name, 0.0) for name in feature_names]], columns=feature_names)
        quality_score = float(bundle["model"].predict_proba(frame)[:, 1][0])
        return {
            "ok": True,
            "schema_version": "open_quality_live_score.v2",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "quality_score": round(quality_score, 8),
            "risk_score": round(1.0 - quality_score, 8),
            "features": features,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": str(path),
        }

    def score_samples(
        self,
        *,
        artifact_path: str | Path | None = None,
        limit: int = 100,
        mode: str = "shadow",
        skip_existing: bool = False,
    ) -> dict[str, Any]:
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "error": "dependency_missing", "detail": dep_error}
        import joblib
        import pandas as pd

        path = Path(str(artifact_path or self.latest_artifact_path()))
        if not path.exists():
            return {"ok": False, "error": "artifact_missing", "artifact_path": str(path)}
        artifact = json.loads(path.read_text(encoding="utf-8"))
        permission = validate_model_artifact(artifact, model_type=MODEL_TYPE, db_path=self.db_path, context={"mode": mode, "operation": "open_quality_score_samples"})
        if not permission.get("ok"):
            return {"ok": False, "error": "model_permission_violation", "artifact_path": str(path), "permission": permission}
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing", "model_file": str(model_file)}
        bundle = joblib.load(model_file)
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        samples = self.load_samples(limit=limit)
        if not samples:
            return {"ok": False, "error": "no_samples"}
        if skip_existing:
            artifact_ref = str(artifact.get("artifact_path") or path)
            existing = self._existing_shadow_sample_ids(artifact_path=artifact_ref)
            samples = [
                sample for sample in samples
                if str(sample.get("sample_id") or "") not in existing
            ]
            if not samples:
                return {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_new_samples",
                    "model_type": MODEL_TYPE,
                    "model_version": str(artifact.get("model_version") or MODEL_VERSION),
                    "artifact_path": str(path),
                    "count": 0,
                    "items": [],
                    "capabilities": artifact.get("capabilities") or {},
                }
        x = pd.DataFrame([item["features"] for item in samples], columns=feature_names)
        probs = bundle["model"].predict_proba(x)[:, 1]
        items = [self._persist_inference(artifact, sample, float(prob), mode=mode) for sample, prob in zip(samples, probs)]
        return {"ok": True, "model_type": MODEL_TYPE, "model_version": str(artifact.get("model_version") or MODEL_VERSION), "artifact_path": str(path), "count": len(items), "items": items, "capabilities": artifact.get("capabilities") or {}}

    def _persist_inference(self, artifact: dict[str, Any], sample: dict[str, Any], quality_score: float, *, mode: str) -> dict[str, Any]:
        now = time.time()
        risk_score = max(0.0, min(1.0, 1.0 - float(quality_score)))
        prediction = 1 if quality_score >= 0.5 else 0
        result = {
            "schema_version": "open_quality_shadow_result.v1",
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "quality_score": round(float(quality_score), 8),
            "risk_score": round(risk_score, 8),
            "prediction": prediction,
            "prediction_label": "acceptable_open_quality" if prediction else "weak_open_quality",
            "advice": "review_only",
            "capabilities": artifact.get("capabilities") or {},
            "guardrails": list(artifact.get("guardrails") or []),
        }
        result["source_agent"] = "lightgbm_shadow_models"
        result["authority_verdict"] = AgentAuthorityRegistryService().evaluate(
            "lightgbm_shadow_models",
            "model_stage",
            "shadow_model_audit",
            requested_writes=["open_quality_shadow_audit"],
            status=mode,
            impact_level="shadow",
        )
        payload = {
            "sample_id": sample["sample_id"],
            "decision_id": sample["decision_id"],
            "trade_id": sample["trade_id"],
            "position_id": sample["position_id"],
            "features": sample["features"],
            "label": sample["label"],
            "rule_label": sample["rule_label"],
            "pnl": sample["pnl"],
            "outcome_label": sample["outcome_label"],
            "source_agent": "lightgbm_shadow_models",
            "authority_verdict": result["authority_verdict"],
        }
        inference_id = f"{MODEL_TYPE}:{sample['sample_id']}:{int(now * 1000)}"
        conn = self._conn()
        try:
            self._execute(
                conn,
                """
                INSERT INTO open_quality_shadow_audit
                (inference_id, model_type, model_version, artifact_path, sample_id,
                 decision_id, trade_id, position_id, mode, quality_score, risk_score,
                 prediction, payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    MODEL_TYPE,
                    str(artifact.get("model_version") or MODEL_VERSION),
                    str(artifact.get("artifact_path") or ""),
                    sample["sample_id"],
                    sample["decision_id"],
                    sample["trade_id"],
                    sample["position_id"],
                    mode,
                    float(quality_score),
                    float(risk_score),
                    int(prediction),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "inference_id": inference_id,
            "sample_id": sample["sample_id"],
            "decision_id": sample["decision_id"],
            "position_id": sample["position_id"],
            "quality_score": result["quality_score"],
            "risk_score": result["risk_score"],
            "prediction": prediction,
            "created_at": now,
        }

    def list_audits(self, *, limit: int = 100, position_id: str | None = None) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if position_id:
            clauses.append("position_id=?")
            params.append(str(position_id))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                f"""
                SELECT *
                FROM open_quality_shadow_audit
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            items = []
            for row in rows:
                items.append(
                    {
                        "inference_id": str(row["inference_id"] or ""),
                        "model_type": str(row["model_type"] or ""),
                        "model_version": str(row["model_version"] or ""),
                        "artifact_path": str(row["artifact_path"] or ""),
                        "sample_id": str(row["sample_id"] or ""),
                        "decision_id": str(row["decision_id"] or ""),
                        "trade_id": str(row["trade_id"] or ""),
                        "position_id": str(row["position_id"] or ""),
                        "mode": str(row["mode"] or ""),
                        "quality_score": _safe_float(row["quality_score"]),
                        "risk_score": _safe_float(row["risk_score"]),
                        "prediction": int(row["prediction"] or 0),
                        "payload": _loads(row["payload_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()
