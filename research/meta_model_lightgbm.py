from __future__ import annotations

import hashlib
import json
import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR, STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.ledger.service import DecisionLedger
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact
from research.features.evidence_contract import stable_hash


MODEL_TYPE = "meta_model_lightgbm"
MODEL_VERSION = "4.0"
FEATURE_SCHEMA_VERSION = "pit.v2.meta_residual_rate"
POSTURE_LABELS = ["contract", "observe", "recover"]
RESIDUAL_LABELS = ["contract_more", "agree", "recover_more"]
FEATURE_NAMES = [
    "rolling_trade_count",
    "rolling_pnl_sum",
    "rolling_pnl_avg",
    "rolling_loss_rate",
    "rolling_bad_loss_rate",
    "rolling_win_rate",
    "rolling_mae_avg",
    "rolling_mfe_avg",
    "rolling_mfe_mae_ratio",
    "rolling_small_loss_rate",
    "rolling_thesis_broken_rate",
    "rolling_broker_close_rate",
    "rolling_profit_capture_avg",
    "rolling_giveback_avg",
    "rolling_holding_efficiency_avg",
    "risk_blocked_rate_per_hour",
    "risk_allowed_rate_per_hour",
    "supervisor_close_rate_per_hour",
    "supervisor_reduce_rate_per_hour",
    "supervisor_tighten_rate_per_hour",
    "amend_skipped_rate_per_hour",
    "amend_failed_rate_per_hour",
    "position_quality_weak_rate",
    "factor_governance_weak_rate",
    "counterfactual_premature_rate",
    "counterfactual_protection_tight_rate",
    "counterfactual_correct_stop_rate",
    "llm_error_rate",
    "permission_block_rate",
]


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
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


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _conn_is_pg(conn: Any) -> bool:
    return conn.__class__.__module__.split(".", 1)[0] == "psycopg"


def _sql(conn: Any, sql: str) -> str:
    return sql.replace("?", "%s") if _conn_is_pg(conn) else sql


def _execute(conn: Any, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), tuple(params))


def _table_exists(conn: Any, table: str) -> bool:
    return state_table_exists(conn, table)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
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


def _review_item(row: Any) -> dict[str, Any]:
    item = dict(row)
    payload = _loads(str(item.get("review_json") or "{}"), {})
    outcome = str(item.get("outcome_label") or "").lower()
    close_reason = str(payload.get("close_reason") or "").lower()
    return {
        "review_id": str(item.get("review_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "created_at": _safe_float(item.get("created_at")),
        "pnl": _safe_float(item.get("pnl")),
        "mae": _safe_float(item.get("mae") if item.get("mae") is not None else payload.get("mae")),
        "mfe": _safe_float(item.get("mfe") if item.get("mfe") is not None else payload.get("mfe")),
        "outcome_label": outcome,
        "close_reason": close_reason,
        "profit_capture_ratio": _safe_float(payload.get("profit_capture_ratio")),
        "giveback_ratio": _safe_float(payload.get("giveback_ratio")),
        "holding_efficiency": _safe_float(payload.get("holding_efficiency")),
    }


def _future_posture_label(item: dict[str, Any]) -> int:
    pnl = _safe_float(item.get("pnl"))
    outcome = str(item.get("outcome_label") or "").lower()
    if outcome in {"bad_loss", "loss"} or pnl <= -2.0:
        return 0
    if outcome in {"good_win", "win"} or pnl >= 2.0:
        return 2
    return 1


def _future_window_posture_label(future_items: list[dict[str, Any]]) -> int:
    if not future_items:
        return 1
    pnl_values = [_safe_float(item.get("pnl")) for item in future_items]
    n = len(future_items)
    pnl_sum = sum(pnl_values)
    bad_loss_rate = sum(1 for item in future_items if str(item.get("outcome_label")) == "bad_loss") / max(n, 1)
    loss_rate = sum(1 for value in pnl_values if value < 0.0) / max(n, 1)
    if pnl_sum <= -2.5 or bad_loss_rate >= 0.34 or loss_rate >= 0.67:
        return 0
    if pnl_sum >= 2.5 and loss_rate <= 0.34:
        return 2
    return 1


def _rolling_features(history: list[dict[str, Any]], window: int) -> dict[str, float]:
    items = history[-max(1, int(window)):]
    n = len(items)
    base_names = [
        name for name in FEATURE_NAMES
        if name.startswith("rolling_")
    ]
    if not items:
        return {name: 0.0 for name in base_names}
    pnl_values = [_safe_float(item.get("pnl")) for item in items]
    mae_values = [_safe_float(item.get("mae")) for item in items]
    mfe_values = [_safe_float(item.get("mfe")) for item in items]
    profit_capture = [_safe_float(item.get("profit_capture_ratio")) for item in items]
    giveback = [_safe_float(item.get("giveback_ratio")) for item in items]
    holding_efficiency = [_safe_float(item.get("holding_efficiency")) for item in items]
    mfe_avg = sum(mfe_values) / max(n, 1)
    mae_avg = sum(mae_values) / max(n, 1)
    features = {
        "rolling_trade_count": float(n),
        "rolling_pnl_sum": sum(pnl_values),
        "rolling_pnl_avg": sum(pnl_values) / max(n, 1),
        "rolling_loss_rate": sum(1 for x in pnl_values if x < 0.0) / max(n, 1),
        "rolling_bad_loss_rate": sum(1 for item in items if str(item.get("outcome_label")) == "bad_loss") / max(n, 1),
        "rolling_win_rate": sum(1 for x in pnl_values if x > 0.0) / max(n, 1),
        "rolling_mae_avg": mae_avg,
        "rolling_mfe_avg": mfe_avg,
        "rolling_mfe_mae_ratio": mfe_avg / max(abs(mae_avg), 1e-9),
        "rolling_small_loss_rate": sum(1 for item in items if str(item.get("outcome_label")) == "small_loss") / max(n, 1),
        "rolling_thesis_broken_rate": sum(1 for item in items if str(item.get("close_reason")) == "thesis_broken") / max(n, 1),
        "rolling_broker_close_rate": sum(1 for item in items if str(item.get("close_reason")) == "broker_close") / max(n, 1),
        "rolling_profit_capture_avg": sum(profit_capture) / max(n, 1),
        "rolling_giveback_avg": sum(giveback) / max(n, 1),
        "rolling_holding_efficiency_avg": sum(holding_efficiency) / max(n, 1),
    }
    return {name: _safe_float(features.get(name)) for name in base_names}


class MetaModelLightGBMService:
    """LightGBM sidecar for global meta posture.

    The model predicts advisory-only system posture from historical state
    summaries. It writes shadow inference and optional ledger advisory records,
    but never mutates live execution or risk policy.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        artifact_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts" / MODEL_TYPE
        self._ensure_table()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _conn(self):
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = __import__("sqlite3").Row
        return conn

    def _ensure_table(self) -> None:
        conn = self._conn()
        try:
            _execute(conn,
                """
                CREATE TABLE IF NOT EXISTS meta_model_shadow_audit (
                    inference_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_path TEXT DEFAULT '',
                    mode TEXT DEFAULT 'shadow',
                    posture TEXT DEFAULT '',
                    posture_score REAL DEFAULT 0.0,
                    contract_score REAL DEFAULT 0.0,
                    observe_score REAL DEFAULT 0.0,
                    recover_score REAL DEFAULT 0.0,
                    ledger_decision_id TEXT DEFAULT '',
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            _execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_meta_model_shadow_audit_created
                ON meta_model_shadow_audit(created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def load_reviews(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = _execute(conn,
                """
                SELECT *
                FROM (
                    SELECT review_id, trade_id, position_id, pnl, mae, mfe,
                           outcome_label, review_json, created_at
                    FROM trade_outcome_review
                    ORDER BY created_at DESC
                    LIMIT ?
                ) recent_reviews
                ORDER BY created_at ASC
                """,
                (int(limit),),
            ).fetchall()
            return [_review_item(row) for row in rows]
        finally:
            conn.close()

    def load_samples(self, *, limit: int = 2000, window: int = 12, horizon: int = 3) -> list[dict[str, Any]]:
        reviews = self.load_reviews(limit=limit)
        samples = []
        conn = self._conn()
        try:
            for idx in range(1, len(reviews)):
                history = reviews[:idx]
                target = reviews[idx]
                future = reviews[idx:idx + max(1, int(horizon))]
                future_posture_index = _future_window_posture_label(future)
                features = _rolling_features(history, window=window)
                features.update(self._window_features(conn, end_ts=target["created_at"], window_items=history[-max(1, int(window)):]))
                features = {name: _safe_float(features.get(name)) for name in FEATURE_NAMES}
                rule_posture = str(self._rule_decision_from_features(features).get("posture") or "observe")
                rule_posture_index = POSTURE_LABELS.index(rule_posture) if rule_posture in POSTURE_LABELS else 1
                residual_direction = max(-1, min(1, future_posture_index - rule_posture_index))
                residual_label = residual_direction + 1
                samples.append(
                    {
                        "sample_id": f"meta:{target['review_id'] or target['position_id']}",
                        "created_at": target["created_at"],
                        "target_review_id": target["review_id"],
                        "target_position_id": target["position_id"],
                        "target_pnl": target["pnl"],
                        "future_window": {
                            "horizon": int(horizon),
                            "count": len(future),
                            "pnl_sum": round(sum(_safe_float(item.get("pnl")) for item in future), 8),
                            "labels": [str(item.get("outcome_label") or "") for item in future],
                        },
                        "label": residual_label,
                        "label_name": RESIDUAL_LABELS[residual_label],
                        "future_posture_index": future_posture_index,
                        "future_posture": POSTURE_LABELS[future_posture_index],
                        "rule_posture_index": rule_posture_index,
                        "rule_posture": POSTURE_LABELS[rule_posture_index],
                        "features": features,
                        "traceability": {
                            "source_table": "trade_outcome_review",
                            "target_review_id": target["review_id"],
                            "target_position_id": target["position_id"],
                            "history_count": len(history),
                            "future_count": len(future),
                            "causal_level": "observational",
                            "target_contract": "rule_posture_residual.v1",
                        },
                    }
                )
        finally:
            conn.close()
        return samples

    def _window_features(
        self,
        conn: Any,
        *,
        end_ts: float,
        window_items: list[dict[str, Any]],
    ) -> dict[str, float]:
        # All event-volume features use the same fixed 24-hour observation
        # window and are normalized to hourly rates.  The previous variable
        # window made busier/newer periods look intrinsically riskier.
        start_ts = float(end_ts) - 86400.0
        observation_hours = 24.0
        features = {
            "risk_blocked_rate_per_hour": 0.0,
            "risk_allowed_rate_per_hour": 0.0,
            "supervisor_close_rate_per_hour": 0.0,
            "supervisor_reduce_rate_per_hour": 0.0,
            "supervisor_tighten_rate_per_hour": 0.0,
            "amend_skipped_rate_per_hour": 0.0,
            "amend_failed_rate_per_hour": 0.0,
            "position_quality_weak_rate": 0.0,
            "factor_governance_weak_rate": 0.0,
            "counterfactual_premature_rate": 0.0,
            "counterfactual_protection_tight_rate": 0.0,
            "counterfactual_correct_stop_rate": 0.0,
            "llm_error_rate": 0.0,
            "permission_block_rate": 0.0,
        }
        if _table_exists(conn, "decision_ledger"):
            rows = _execute(conn,
                """
                SELECT event_type, risk_state_json
                FROM decision_ledger
                WHERE created_at >= ? AND created_at <= ?
                """,
                (float(start_ts), float(end_ts)),
            ).fetchall()
            for row in rows:
                event_type = str(row["event_type"] or "")
                if event_type == "supervisor_close":
                    features["supervisor_close_rate_per_hour"] += 1.0
                elif event_type == "supervisor_reduce":
                    features["supervisor_reduce_rate_per_hour"] += 1.0
                elif event_type == "supervisor_tighten":
                    features["supervisor_tighten_rate_per_hour"] += 1.0
                risk_state = _loads(row["risk_state_json"], {})
                verdict = risk_state.get("policy_verdict") or risk_state.get("risk_verdict") or {}
                if verdict:
                    if verdict.get("allowed") is False:
                        features["risk_blocked_rate_per_hour"] += 1.0
                    elif verdict.get("allowed") is True:
                        features["risk_allowed_rate_per_hour"] += 1.0

        if _table_exists(conn, "position_lifecycle_event"):
            rows = _execute(conn,
                """
                SELECT event_type, COUNT(*) AS n
                FROM position_lifecycle_event
                WHERE event_ts >= ? AND event_ts <= ?
                  AND event_type IN ('amend_skipped', 'amend_failed')
                GROUP BY event_type
                """,
                (float(start_ts), float(end_ts)),
            ).fetchall()
            for row in rows:
                key = str(row["event_type"] or "")
                feature_key = f"{key}_rate_per_hour"
                if feature_key in features:
                    features[feature_key] = _safe_float(row["n"])

        for feature_key in (
            "risk_blocked_rate_per_hour", "risk_allowed_rate_per_hour",
            "supervisor_close_rate_per_hour", "supervisor_reduce_rate_per_hour",
            "supervisor_tighten_rate_per_hour", "amend_skipped_rate_per_hour",
            "amend_failed_rate_per_hour",
        ):
            features[feature_key] = features[feature_key] / observation_hours

        features["position_quality_weak_rate"] = self._shadow_weak_rate(
            conn,
            table="position_quality_shadow_audit",
            start_ts=start_ts,
            end_ts=end_ts,
        )
        features["factor_governance_weak_rate"] = self._shadow_weak_rate(
            conn,
            table="factor_governance_shadow_audit",
            start_ts=start_ts,
            end_ts=end_ts,
        )
        features.update(self._counterfactual_rates(conn, start_ts=start_ts, end_ts=end_ts))
        features["llm_error_rate"] = self._status_rate(
            conn,
            table="llm_advisory_audit",
            status_values={"error"},
            start_ts=start_ts,
            end_ts=end_ts,
        )
        features["permission_block_rate"] = self._status_rate(
            conn,
            table="model_permission_audit",
            status_values={"blocked"},
            start_ts=start_ts,
            end_ts=end_ts,
        )
        return features

    @staticmethod
    def _shadow_weak_rate(conn: Any, *, table: str, start_ts: float, end_ts: float) -> float:
        if not _table_exists(conn, table):
            return 0.0
        rows = _execute(conn,
            f"""
            SELECT prediction, result_json
            FROM {table}
            WHERE created_at >= ? AND created_at <= ?
            """,
            (float(start_ts), float(end_ts)),
        ).fetchall()
        if not rows:
            return 0.0
        weak = 0
        for row in rows:
            result = _loads(row["result_json"], {})
            label = str(result.get("prediction_label") or "").lower()
            if "weak" in label or "bad" in label or int(row["prediction"] or 0) == 0:
                weak += 1
        return weak / max(len(rows), 1)

    @staticmethod
    def _counterfactual_rates(conn: Any, *, start_ts: float, end_ts: float) -> dict[str, float]:
        out = {
            "counterfactual_premature_rate": 0.0,
            "counterfactual_protection_tight_rate": 0.0,
            "counterfactual_correct_stop_rate": 0.0,
        }
        if not _table_exists(conn, "supervisor_counterfactual_review"):
            return out
        rows = _execute(conn,
            """
            SELECT label
            FROM supervisor_counterfactual_review
            WHERE close_ts >= ? AND close_ts <= ?
            """,
            (float(start_ts), float(end_ts)),
        ).fetchall()
        if not rows:
            return out
        labels = [str(row["label"] or "") for row in rows]
        n = max(len(labels), 1)
        out["counterfactual_premature_rate"] = sum(1 for label in labels if label in {"premature_tighten", "noise_stopout"}) / n
        out["counterfactual_protection_tight_rate"] = sum(1 for label in labels if label == "protection_too_tight") / n
        out["counterfactual_correct_stop_rate"] = sum(1 for label in labels if label == "correct_stop") / n
        return out

    @staticmethod
    def _status_rate(
        conn: Any,
        *,
        table: str,
        status_values: set[str],
        start_ts: float,
        end_ts: float,
    ) -> float:
        if not _table_exists(conn, table):
            return 0.0
        rows = _execute(conn,
            f"""
            SELECT status
            FROM {table}
            WHERE created_at >= ? AND created_at <= ?
            """,
            (float(start_ts), float(end_ts)),
        ).fetchall()
        if not rows:
            return 0.0
        return sum(1 for row in rows if str(row["status"] or "") in status_values) / max(len(rows), 1)

    def train(
        self,
        *,
        limit: int = 2000,
        window: int = 12,
        horizon: int = 3,
        holdout_ratio: float = 0.25,
        min_samples: int = 30,
        walk_forward_folds: int = 3,
        max_generalization_gap: float = 0.25,
        register: bool = True,
        registry_db_path: str | None = None,
        symbol: str = "XAUUSD+",
        timeframe: str = "M5",
    ) -> dict[str, Any]:
        dep_error = _dependency_error()
        if dep_error:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "error": "dependency_missing",
                "detail": dep_error,
                "required": ["lightgbm", "scikit-learn", "joblib", "pandas"],
            }

        import joblib
        import lightgbm as lgb
        import pandas as pd
        from sklearn.metrics import accuracy_score, balanced_accuracy_score

        samples = self.load_samples(limit=limit, window=window, horizon=horizon)
        if len(samples) < int(min_samples):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "target_contract": "rule_posture_residual.v1",
                "error": "insufficient_meta_samples",
            }
        labels = [int(item["label"]) for item in samples]
        if len(set(labels)) < 2:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "sample_count": len(samples),
                "label_distribution": {RESIDUAL_LABELS[i]: labels.count(i) for i in range(len(RESIDUAL_LABELS))},
                "error": "single_class_training_data",
            }

        holdout_count = max(1, int(round(len(samples) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_count = min(holdout_count, len(samples) - 1)
        train_samples = samples[:-holdout_count]
        holdout_samples = samples[-holdout_count:]

        x_train = pd.DataFrame([item["features"] for item in train_samples], columns=FEATURE_NAMES)
        y_train = [int(item["label"]) for item in train_samples]
        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]
        actual_train = [int(item["future_posture_index"]) for item in train_samples]
        actual_holdout = [int(item["future_posture_index"]) for item in holdout_samples]

        def _new_model(sample_count: int):
            return lgb.LGBMClassifier(
                objective="multiclass",
                num_class=len(RESIDUAL_LABELS),
                n_estimators=160,
                learning_rate=0.04,
                num_leaves=15,
                min_child_samples=max(1, min(20, sample_count // 4)),
                subsample=0.9,
                colsample_bytree=0.9,
                class_weight="balanced",
                random_state=42,
                n_jobs=1,
                verbosity=-1,
            )

        model = _new_model(len(train_samples))
        model.fit(x_train, y_train)
        train_residual_pred = [int(value) for value in model.predict(x_train)]
        holdout_residual_pred = [int(value) for value in model.predict(x_holdout)]

        def _fused_postures(items: list[dict[str, Any]], residual_predictions: list[int]) -> list[int]:
            return [
                max(0, min(len(POSTURE_LABELS) - 1, int(item["rule_posture_index"]) + int(prediction) - 1))
                for item, prediction in zip(items, residual_predictions)
            ]

        train_pred = _fused_postures(train_samples, train_residual_pred)
        holdout_pred = _fused_postures(holdout_samples, holdout_residual_pred)
        rule_holdout_pred = [int(item["rule_posture_index"]) for item in holdout_samples]
        majority_label = Counter(actual_train).most_common(1)[0][0]
        majority_holdout_pred = [majority_label] * len(actual_holdout)

        def _label_distribution(values: list[int]) -> dict[str, int]:
            counts = Counter(int(value) for value in values)
            return {POSTURE_LABELS[idx]: int(counts.get(idx, 0)) for idx in range(len(POSTURE_LABELS))}

        def _holdout_metrics(preds: list[int]) -> dict[str, Any]:
            return {
                "accuracy": round(float(accuracy_score(actual_holdout, preds)), 6) if actual_holdout else None,
                "balanced_accuracy": round(float(balanced_accuracy_score(actual_holdout, preds)), 6) if actual_holdout else None,
                "prediction_distribution": _label_distribution(preds),
            }

        feature_importance = [
            {"feature": name, "importance": int(value)}
            for name, value in sorted(
                zip(FEATURE_NAMES, model.feature_importances_),
                key=lambda item: (-int(item[1]), item[0]),
            )
        ]
        label_distribution = {RESIDUAL_LABELS[i]: labels.count(i) for i in range(len(RESIDUAL_LABELS))}
        holdout_model = _holdout_metrics([int(value) for value in holdout_pred])
        holdout_rule = _holdout_metrics(rule_holdout_pred)
        holdout_majority = _holdout_metrics(majority_holdout_pred)
        model_accuracy = float(holdout_model["accuracy"] or 0.0)
        rule_accuracy = float(holdout_rule["accuracy"] or 0.0)
        majority_accuracy = float(holdout_majority["accuracy"] or 0.0)
        model_balanced_accuracy = float(holdout_model["balanced_accuracy"] or 0.0)
        rule_balanced_accuracy = float(holdout_rule["balanced_accuracy"] or 0.0)
        majority_balanced_accuracy = float(holdout_majority["balanced_accuracy"] or 0.0)
        requested_folds = max(2, min(int(walk_forward_folds or 3), 5))
        initial_train_count = max(10, min(len(train_samples) - 1, len(train_samples) // 2))
        validation_pool = max(0, len(train_samples) - initial_train_count)
        fold_size = max(1, validation_pool // requested_folds) if validation_pool else 0
        walk_forward_items: list[dict[str, Any]] = []
        if fold_size > 0:
            for fold_index in range(requested_folds):
                validation_start = initial_train_count + fold_index * fold_size
                validation_end = (
                    len(train_samples)
                    if fold_index == requested_folds - 1
                    else min(len(train_samples), validation_start + fold_size)
                )
                fold_train = train_samples[:validation_start]
                fold_validation = train_samples[validation_start:validation_end]
                fold_train_labels = [int(item["label"]) for item in fold_train]
                fold_validation_labels = [int(item["label"]) for item in fold_validation]
                fold_validation_actual = [int(item["future_posture_index"]) for item in fold_validation]
                if (
                    not fold_validation
                    or len(set(fold_train_labels)) < 2
                    or len(set(fold_validation_labels)) < 2
                ):
                    continue
                fold_model = _new_model(len(fold_train))
                fold_model.fit(
                    pd.DataFrame([item["features"] for item in fold_train], columns=FEATURE_NAMES),
                    fold_train_labels,
                )
                fold_residual_predictions = [
                    int(value)
                    for value in fold_model.predict(
                        pd.DataFrame([item["features"] for item in fold_validation], columns=FEATURE_NAMES)
                    )
                ]
                fold_predictions = _fused_postures(fold_validation, fold_residual_predictions)
                fold_majority_label = Counter(int(item["future_posture_index"]) for item in fold_train).most_common(1)[0][0]
                fold_majority_predictions = [fold_majority_label] * len(fold_validation_actual)
                walk_forward_items.append({
                    "fold": fold_index + 1,
                    "train_count": len(fold_train),
                    "validation_count": len(fold_validation),
                    "balanced_accuracy": round(
                        float(balanced_accuracy_score(fold_validation_actual, fold_predictions)),
                        6,
                    ),
                    "majority_baseline_balanced_accuracy": round(
                        float(balanced_accuracy_score(fold_validation_actual, fold_majority_predictions)),
                        6,
                    ),
                })
        walk_forward_mean = (
            sum(float(item["balanced_accuracy"]) for item in walk_forward_items) / len(walk_forward_items)
            if walk_forward_items else 0.0
        )
        walk_forward_baseline_mean = (
            sum(float(item["majority_baseline_balanced_accuracy"]) for item in walk_forward_items)
            / len(walk_forward_items)
            if walk_forward_items else 0.0
        )
        walk_forward_worst = min(
            (float(item["balanced_accuracy"]) for item in walk_forward_items),
            default=0.0,
        )
        walk_forward_ready = bool(
            len(walk_forward_items) >= 2
            and walk_forward_mean >= walk_forward_baseline_mean
            and walk_forward_worst >= 0.25
        )
        train_accuracy = float(accuracy_score(actual_train, train_pred))
        generalization_gap = max(0.0, train_accuracy - model_accuracy)
        generalization_ready = generalization_gap <= max(0.05, float(max_generalization_gap or 0.25))
        baseline_margin = 0.02
        train_future_distribution = Counter(actual_train)
        holdout_future_distribution = Counter(actual_holdout)
        max_future_posture_rate_delta = max(
            abs(
                train_future_distribution.get(index, 0) / max(len(actual_train), 1)
                - holdout_future_distribution.get(index, 0) / max(len(actual_holdout), 1)
            )
            for index in range(len(POSTURE_LABELS))
        )
        holdout_rule_distribution = Counter(rule_holdout_pred)
        holdout_rule_max_rate = max(holdout_rule_distribution.values(), default=0) / max(len(rule_holdout_pred), 1)
        distribution_ready = bool(
            max_future_posture_rate_delta <= 0.25
            and holdout_rule_max_rate <= 0.90
        )
        model_beats_baseline = (
            model_accuracy >= majority_accuracy + baseline_margin
            and model_balanced_accuracy >= majority_balanced_accuracy
            and walk_forward_ready
            and generalization_ready
            and distribution_ready
        )
        rule_beats_baseline = (
            rule_accuracy >= majority_accuracy + baseline_margin
            and rule_balanced_accuracy >= majority_balanced_accuracy
        )
        if model_beats_baseline and model_accuracy >= rule_accuracy:
            recommended_source = "model_shadow_candidate"
            readiness_status = "model_shadow_candidate"
            degradation_reason = ""
        elif rule_beats_baseline:
            recommended_source = "rule_sidecar_candidate"
            readiness_status = "rule_sidecar_candidate"
            degradation_reason = ""
        else:
            recommended_source = "simple_baseline_observer"
            readiness_status = "blocked_by_baseline"
            degradation_reason = "holdout_model_and_rule_do_not_beat_majority_baseline"
            if not generalization_ready:
                degradation_reason = "train_holdout_generalization_gap_exceeded"
            elif not walk_forward_ready:
                degradation_reason = "walk_forward_validation_not_stable"
            elif not distribution_ready:
                degradation_reason = "posture_distribution_drift_or_rule_collapse"
        metrics = {
            "train": {"count": len(y_train), "accuracy": round(train_accuracy, 6)},
            "split": "time_ordered",
            "holdout_ratio": float(holdout_ratio),
            "train_count": len(train_samples),
            "holdout_count": len(holdout_samples),
            "holdout": {
                "count": len(y_holdout),
                **holdout_model,
                "rule_accuracy": holdout_rule["accuracy"],
                "rule_balanced_accuracy": holdout_rule["balanced_accuracy"],
                "rule_prediction_distribution": holdout_rule["prediction_distribution"],
                "majority_baseline_accuracy": holdout_majority["accuracy"],
                "majority_baseline_balanced_accuracy": holdout_majority["balanced_accuracy"],
                "majority_baseline_distribution": holdout_majority["prediction_distribution"],
                "model_lift_vs_rule": round(float((holdout_model["accuracy"] or 0.0) - (holdout_rule["accuracy"] or 0.0)), 6),
                "model_lift_vs_majority": round(float((holdout_model["accuracy"] or 0.0) - (holdout_majority["accuracy"] or 0.0)), 6),
                "rule_lift_vs_majority": round(rule_accuracy - majority_accuracy, 6),
            },
            "walk_forward": {
                "split": "expanding_time_ordered",
                "requested_folds": requested_folds,
                "completed_folds": len(walk_forward_items),
                "mean_balanced_accuracy": round(walk_forward_mean, 6),
                "worst_balanced_accuracy": round(walk_forward_worst, 6),
                "majority_baseline_mean_balanced_accuracy": round(walk_forward_baseline_mean, 6),
                "ready": walk_forward_ready,
                "folds": walk_forward_items,
            },
            "distribution_stability": {
                "max_future_posture_rate_delta": round(max_future_posture_rate_delta, 6),
                "max_allowed_future_posture_rate_delta": 0.25,
                "holdout_rule_max_posture_rate": round(holdout_rule_max_rate, 6),
                "max_allowed_rule_posture_rate": 0.90,
                "ready": distribution_ready,
            },
            "governance_readiness": {
                "status": readiness_status,
                "model_ready_for_governance": bool(model_beats_baseline),
                "rule_ready_for_governance": bool(rule_beats_baseline),
                "baseline_margin": baseline_margin,
                "recommended_source": recommended_source,
                "degradation_reason": degradation_reason,
                "checks": {
                    "model_accuracy": round(model_accuracy, 6),
                    "model_balanced_accuracy": round(model_balanced_accuracy, 6),
                    "rule_accuracy": round(rule_accuracy, 6),
                    "rule_balanced_accuracy": round(rule_balanced_accuracy, 6),
                    "majority_baseline_accuracy": round(majority_accuracy, 6),
                    "majority_baseline_balanced_accuracy": round(majority_balanced_accuracy, 6),
                    "walk_forward_ready": walk_forward_ready,
                    "generalization_gap": round(generalization_gap, 6),
                    "max_generalization_gap": max(0.05, float(max_generalization_gap or 0.25)),
                    "generalization_ready": generalization_ready,
                    "distribution_ready": distribution_ready,
                },
            },
            "sample_count": len(samples),
            "feature_count": len(FEATURE_NAMES),
            "label_distribution": label_distribution,
            "future_posture_distribution": {
                POSTURE_LABELS[i]: [int(item["future_posture_index"]) for item in samples].count(i)
                for i in range(len(POSTURE_LABELS))
            },
            "target_contract": "rule_posture_residual.v1",
            "safe_for_live_trading": False,
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({
            "model": model,
            "feature_names": FEATURE_NAMES,
            "posture_labels": POSTURE_LABELS,
            "residual_labels": RESIDUAL_LABELS,
            "target_contract": "rule_posture_residual.v1",
        }, model_path)
        artifact = {
            "schema_version": "meta_model_lightgbm_artifact.v2",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "posture_labels": POSTURE_LABELS,
            "residual_labels": RESIDUAL_LABELS,
            "label": "future_meta_posture_residual_vs_rule",
            "sample_window": {
                "limit": int(limit),
                "sample_count": len(samples),
                "rolling_window": int(window),
                "label_horizon": int(horizon),
            },
            "metrics": metrics,
            "explainability": {
                "feature_importance": feature_importance,
                "summary": "LightGBM shadow-only meta posture model. Scores are advisory and logged.",
                "evidence_contract": {
                    "causal_level": "observational",
                    "source": "rolling aggregate of trade reviews, risk verdicts, shadow audits, counterfactual reviews, LLM audits, and permission audits",
                    "training_rule": "shadow-only aggregate posture model; not a single-trade causal proof",
                },
            },
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
                "can_increase_hard_risk_limits": False,
                "can_change_factor_weights": False,
                "can_bypass_risk_policy": False,
                "can_apply_policy_without_review": False,
            },
            "guardrails": [
                "MUST NOT place orders",
                "MUST NOT close positions",
                "MUST NOT change risk limits",
                "MUST NOT change factor weights",
                "MUST write advisory output to audit or ledger before review",
            ],
        }
        metadata_path.write_text(
            json.dumps(artifact, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        artifact["artifact_path"] = str(metadata_path)
        artifact["artifact_sha256"] = _sha256(metadata_path)

        registry_version = None
        if register:
            from research.model_registry import ModelRegistry

            registry_version = ModelRegistry(db_path=registry_db_path).register(
                MODEL_TYPE,
                artifact_path=str(metadata_path),
                params={
                    "model_version": MODEL_VERSION,
                    "label": artifact["label"],
                    "feature_names": FEATURE_NAMES,
                    "safe_for_live_trading": False,
                },
                metrics={
                    "sample_count": len(samples),
                    "feature_count": len(FEATURE_NAMES),
                    "holdout_accuracy": metrics["holdout"]["accuracy"],
                    "majority_baseline_accuracy": metrics["holdout"]["majority_baseline_accuracy"],
                    "governance_readiness_status": metrics["governance_readiness"]["status"],
                    "recommended_source": metrics["governance_readiness"]["recommended_source"],
                    "walk_forward_mean_balanced_accuracy": metrics["walk_forward"]["mean_balanced_accuracy"],
                    "walk_forward_ready": metrics["walk_forward"]["ready"],
                    "generalization_gap": metrics["governance_readiness"]["checks"]["generalization_gap"],
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
        paths = sorted(self.artifact_dir.glob(f"{MODEL_TYPE}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(paths[0]) if paths else ""

    def score_samples(
        self,
        *,
        artifact_path: str | Path | None = None,
        limit: int = 100,
        window: int = 12,
        horizon: int = 3,
        mode: str = "shadow",
        materialize_ledger: bool = False,
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
        permission = validate_model_artifact(
            artifact,
            model_type=MODEL_TYPE,
            db_path=self.db_path,
            context={"mode": mode, "operation": "meta_model_lightgbm_score_samples"},
        )
        if not permission.get("ok"):
            return {
                "ok": False,
                "error": "model_permission_violation",
                "artifact_path": str(path),
                "permission": permission,
            }
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing", "model_file": str(model_file)}
        bundle = joblib.load(model_file)
        model = bundle["model"]
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        samples = self.load_samples(limit=limit, window=window, horizon=horizon)
        if not samples:
            return {"ok": False, "error": "no_samples"}
        x = pd.DataFrame([item["features"] for item in samples], columns=feature_names)
        probs = model.predict_proba(x)
        items = []
        for sample, prob in zip(samples, probs):
            items.append(
                self._persist_inference(
                    artifact,
                    sample,
                    [float(x) for x in prob],
                    mode=mode,
                    materialize_ledger=materialize_ledger,
                )
            )
        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": str(path),
            "count": len(items),
            "items": items,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
        }

    def _persist_inference(
        self,
        artifact: dict[str, Any],
        sample: dict[str, Any],
        scores: list[float],
        *,
        mode: str,
        materialize_ledger: bool,
    ) -> dict[str, Any]:
        now = time.time()
        residual_scores = list(scores)[: len(RESIDUAL_LABELS)]
        while len(residual_scores) < len(RESIDUAL_LABELS):
            residual_scores.append(0.0)
        rule_idx = int(sample.get("rule_posture_index", 1))
        rule_idx = max(0, min(len(POSTURE_LABELS) - 1, rule_idx))
        posture_scores = [0.0] * len(POSTURE_LABELS)
        for residual_idx, probability in enumerate(residual_scores):
            final_idx = max(0, min(len(POSTURE_LABELS) - 1, rule_idx + residual_idx - 1))
            posture_scores[final_idx] += float(probability)
        best_residual_idx = max(range(len(RESIDUAL_LABELS)), key=lambda idx: residual_scores[idx])
        best_idx = max(range(len(POSTURE_LABELS)), key=lambda idx: posture_scores[idx])
        posture = POSTURE_LABELS[best_idx]
        posture_score = posture_scores[best_idx]
        result = {
            "schema_version": "meta_model_shadow_result.v1",
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "posture": posture,
            "posture_score": round(float(posture_score), 8),
            "scores": {label: round(float(posture_scores[idx]), 8) for idx, label in enumerate(POSTURE_LABELS)},
            "rule_posture": POSTURE_LABELS[rule_idx],
            "residual": RESIDUAL_LABELS[best_residual_idx],
            "residual_score": round(float(residual_scores[best_residual_idx]), 8),
            "residual_scores": {
                label: round(float(residual_scores[idx]), 8)
                for idx, label in enumerate(RESIDUAL_LABELS)
            },
            "target_contract": "rule_posture_residual.v1",
            "risk_budget_advice": self._risk_budget_advice(posture),
            "trade_frequency_advice": self._trade_frequency_advice(posture),
            "advice": "review_only",
            "advisory_only": True,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
            "guardrails": list(artifact.get("guardrails") or []),
            "traceability": {
                "sample_id": sample["sample_id"],
                "artifact_path": str(artifact.get("artifact_path") or ""),
                "features_sha256": stable_hash(sample.get("features") or {}),
                "input_traceability": sample.get("traceability") or {},
            },
        }
        result["source_agent"] = "lightgbm_shadow_models"
        result["authority_verdict"] = AgentAuthorityRegistryService().evaluate(
            "lightgbm_shadow_models",
            "model_stage",
            "shadow_model_audit",
            requested_writes=["meta_model_shadow_audit"],
            status=mode,
            impact_level="shadow",
        )
        payload = {
            "sample_id": sample["sample_id"],
            "target_review_id": sample["target_review_id"],
            "target_position_id": sample["target_position_id"],
            "target_pnl": sample["target_pnl"],
            "future_window": sample.get("future_window") or {},
            "label": sample["future_posture_index"],
            "label_name": sample["future_posture"],
            "residual_label": sample["label"],
            "residual_label_name": sample["label_name"],
            "rule_posture": sample["rule_posture"],
            "features": sample["features"],
            "traceability": {
                **(sample.get("traceability") or {}),
                "features_sha256": stable_hash(sample.get("features") or {}),
            },
            "source_agent": "lightgbm_shadow_models",
            "authority_verdict": result["authority_verdict"],
        }
        ledger_decision_id = ""
        if materialize_ledger:
            ledger_decision_id = DecisionLedger(str(self.db_path)).log_decision(
                event_type="meta_model_lightgbm_advisory",
                symbol="XAUUSD+",
                timeframe="M5",
                decision_ts=now,
                portfolio_state={"source_sample": payload},
                risk_state={"model_type": MODEL_TYPE, "advisory_only": True},
                action_score=float(posture_score),
                action_reason=posture,
                action_json={
                    "schema_version": "meta_model_lightgbm_advisory_ledger.v1",
                    "result": result,
                    "payload": payload,
                    "advisory_only": True,
                },
            )
        inference_id = f"{MODEL_TYPE}:{sample['sample_id']}:{int(now * 1000)}"
        conn = self._conn()
        try:
            _execute(conn,
                """
                INSERT INTO meta_model_shadow_audit
                (inference_id, model_type, model_version, artifact_path, mode,
                 posture, posture_score, contract_score, observe_score,
                 recover_score, ledger_decision_id, payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    MODEL_TYPE,
                    str(artifact.get("model_version") or MODEL_VERSION),
                    str(artifact.get("artifact_path") or ""),
                    str(mode),
                    posture,
                    float(posture_score),
                    float(posture_scores[0]),
                    float(posture_scores[1]),
                    float(posture_scores[2]),
                    ledger_decision_id,
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
            "posture": posture,
            "posture_score": result["posture_score"],
            "scores": result["scores"],
            "ledger_decision_id": ledger_decision_id,
            "created_at": now,
        }

    @staticmethod
    def _risk_budget_advice(posture: str) -> dict[str, Any]:
        if posture == "contract":
            return {"direction": "reduce", "suggested_delta_pct": -20.0}
        if posture == "recover":
            return {"direction": "hold_or_restore_review", "suggested_delta_pct": 0.0}
        return {"direction": "hold", "suggested_delta_pct": 0.0}

    @staticmethod
    def _trade_frequency_advice(posture: str) -> dict[str, Any]:
        if posture == "contract":
            return {"direction": "reduce", "reason": "meta model predicts elevated future risk"}
        if posture == "recover":
            return {"direction": "normal", "reason": "meta model does not predict elevated risk"}
        return {"direction": "hold", "reason": "meta model suggests observation"}

    def list_audits(self, *, limit: int = 100, posture: str | None = None) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if posture:
            clauses.append("posture=?")
            params.append(str(posture))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = _execute(conn,
                f"""
                SELECT *
                FROM meta_model_shadow_audit
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
                        "mode": str(row["mode"] or ""),
                        "posture": str(row["posture"] or ""),
                        "posture_score": _safe_float(row["posture_score"]),
                        "scores": {
                            "contract": _safe_float(row["contract_score"]),
                            "observe": _safe_float(row["observe_score"]),
                            "recover": _safe_float(row["recover_score"]),
                        },
                        "ledger_decision_id": str(row["ledger_decision_id"] or ""),
                        "payload": _loads(row["payload_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()

    def build_shadow_report(
        self,
        *,
        limit: int = 200,
        posture: str | None = None,
        include_samples: bool = True,
    ) -> dict[str, Any]:
        audits = self.list_audits(limit=limit, posture=posture)
        items = audits["items"]
        confusion = {
            actual: {predicted: 0 for predicted in POSTURE_LABELS}
            for actual in POSTURE_LABELS
        }
        posture_distribution = {label: 0 for label in POSTURE_LABELS}
        label_distribution = {label: 0 for label in POSTURE_LABELS}
        score_sums = {label: 0.0 for label in POSTURE_LABELS}
        score_count = 0
        evaluated: list[dict[str, Any]] = []
        mistakes: list[dict[str, Any]] = []
        rule_compared = 0
        rule_correct = 0
        model_correct_for_rule_set = 0
        rule_agree = 0
        rule_distribution = {label: 0 for label in POSTURE_LABELS}
        rule_disagreements: list[dict[str, Any]] = []

        for item in items:
            payload = item.get("payload") or {}
            result = item.get("result") or {}
            features = payload.get("features") or {}
            predicted = str(item.get("posture") or result.get("posture") or "")
            actual = str(payload.get("label_name") or "")
            if predicted in posture_distribution:
                posture_distribution[predicted] += 1
                for label, value in (item.get("scores") or {}).items():
                    if label in score_sums:
                        score_sums[label] += _safe_float(value)
                score_count += 1
            if actual in label_distribution:
                label_distribution[actual] += 1
            if predicted not in POSTURE_LABELS or actual not in POSTURE_LABELS:
                continue

            confusion[actual][predicted] += 1
            is_correct = predicted == actual
            row_summary = {
                "inference_id": item["inference_id"],
                "created_at": item["created_at"],
                "sample_id": str(payload.get("sample_id") or ""),
                "target_review_id": str(payload.get("target_review_id") or ""),
                "target_position_id": str(payload.get("target_position_id") or ""),
                "target_pnl": _safe_float(payload.get("target_pnl")),
                "actual": actual,
                "predicted": predicted,
                "posture_score": _safe_float(item.get("posture_score")),
                "scores": item.get("scores") or {},
                "future_window": payload.get("future_window") or {},
                "top_signal_features": self._top_signal_features(features),
            }
            evaluated.append(row_summary)
            if not is_correct and len(mistakes) < 20:
                mistakes.append(row_summary)

            rule_decision = self._rule_decision_from_features(features)
            rule_posture = str(rule_decision.get("posture") or "")
            if rule_posture in POSTURE_LABELS:
                rule_compared += 1
                rule_distribution[rule_posture] += 1
                if rule_posture == actual:
                    rule_correct += 1
                if predicted == actual:
                    model_correct_for_rule_set += 1
                if rule_posture == predicted:
                    rule_agree += 1
                elif len(rule_disagreements) < 20:
                    rule_disagreements.append(
                        {
                            **row_summary,
                            "rule_posture": rule_posture,
                            "rule_risk_score": _safe_float(rule_decision.get("risk_score")),
                            "rule_rationale": list(rule_decision.get("rationale") or []),
                        }
                    )

        evaluated_count = len(evaluated)
        correct = sum(confusion[label][label] for label in POSTURE_LABELS)
        report = {
            "ok": True,
            "schema_version": "meta_model_shadow_report.v1",
            "model_type": MODEL_TYPE,
            "generated_at": time.time(),
            "audit_count": len(items),
            "evaluated_count": evaluated_count,
            "accuracy": round(correct / evaluated_count, 6) if evaluated_count else 0.0,
            "confusion_matrix": confusion,
            "posture_distribution": posture_distribution,
            "label_distribution": label_distribution,
            "average_scores": {
                label: round(score_sums[label] / max(score_count, 1), 8)
                for label in POSTURE_LABELS
            },
            "rule_comparison": {
                "compared_count": rule_compared,
                "agreement_rate": round(rule_agree / rule_compared, 6) if rule_compared else 0.0,
                "rule_accuracy": round(rule_correct / rule_compared, 6) if rule_compared else 0.0,
                "model_accuracy_on_compared": round(model_correct_for_rule_set / rule_compared, 6) if rule_compared else 0.0,
                "rule_distribution": rule_distribution,
                "disagreements": rule_disagreements if include_samples else [],
            },
            "mistakes": mistakes if include_samples else [],
            "artifact_summary": self._latest_artifact_summary(items),
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
            },
        }
        if include_samples:
            report["samples"] = evaluated[:50]
        return report

    @staticmethod
    def _top_signal_features(features: dict[str, Any], *, limit: int = 8) -> list[dict[str, Any]]:
        rows = [
            {"feature": name, "value": round(_safe_float(value), 8)}
            for name, value in (features or {}).items()
            if abs(_safe_float(value)) > 1e-9
        ]
        return sorted(rows, key=lambda item: (-abs(float(item["value"])), item["feature"]))[:limit]

    def _rule_decision_from_features(self, features: dict[str, Any]) -> dict[str, Any]:
        from research.meta_model_sidecar import MetaModelSidecar

        weak_factor_count = 1 if _safe_float(features.get("factor_governance_weak_rate")) >= 0.45 else 0
        system_health = "normal"
        if _safe_float(features.get("permission_block_rate")) >= 0.5:
            system_health = "critical"
        elif _safe_float(features.get("llm_error_rate")) >= 0.5:
            system_health = "degraded"
        context = {
            "schema_version": "meta_shadow_report_rule_context.v1",
            "risk": {
                "blocked_verdict_count_24h": int(round(
                    _safe_float(features.get("risk_blocked_rate_per_hour")) * 24.0
                )),
            },
            "factor": {
                "health": {
                    "weak_count": weak_factor_count,
                },
            },
            "learning": {
                "rolling": {
                    "trade_count": _safe_int(features.get("rolling_trade_count")),
                    "pnl_sum": _safe_float(features.get("rolling_pnl_sum")),
                    "pnl_avg": _safe_float(features.get("rolling_pnl_avg")),
                    "loss_rate": _safe_float(features.get("rolling_loss_rate")),
                    "bad_loss_rate": _safe_float(features.get("rolling_bad_loss_rate")),
                    "win_rate": _safe_float(features.get("rolling_win_rate")),
                    "mfe_mae_ratio": _safe_float(features.get("rolling_mfe_mae_ratio")),
                    "thesis_broken_rate": _safe_float(features.get("rolling_thesis_broken_rate")),
                    "broker_close_rate": _safe_float(features.get("rolling_broker_close_rate")),
                    "profit_capture_avg": _safe_float(features.get("rolling_profit_capture_avg")),
                    "giveback_avg": _safe_float(features.get("rolling_giveback_avg")),
                },
                "counterfactual": {
                    "premature_rate": _safe_float(features.get("counterfactual_premature_rate")),
                    "protection_tight_rate": _safe_float(features.get("counterfactual_protection_tight_rate")),
                    "correct_stop_rate": _safe_float(features.get("counterfactual_correct_stop_rate")),
                },
                "position_quality_shadow": {
                    "weak_rate": _safe_float(features.get("position_quality_weak_rate")),
                },
                "factor_governance_shadow": {
                    "weak_rate": _safe_float(features.get("factor_governance_weak_rate")),
                },
            },
            "market": {},
            "system": {
                "health": system_health,
            },
        }
        return MetaModelSidecar(self.db_path).decide(context)

    def _latest_artifact_summary(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        path = ""
        for item in items:
            candidate = str(item.get("artifact_path") or "")
            if candidate:
                path = candidate
                break
        if not path:
            path = self.latest_artifact_path()
        if not path:
            return {}
        artifact_path = Path(path)
        if not artifact_path.exists():
            return {"artifact_path": str(artifact_path), "missing": True}
        try:
            artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return {"artifact_path": str(artifact_path), "error": str(exc)}
        return {
            "artifact_path": str(artifact_path),
            "model_version": str(artifact.get("model_version") or ""),
            "sample_window": dict(artifact.get("sample_window") or {}),
            "metrics": dict(artifact.get("metrics") or {}),
            "top_features": list((artifact.get("explainability") or {}).get("feature_importance") or [])[:12],
        }
