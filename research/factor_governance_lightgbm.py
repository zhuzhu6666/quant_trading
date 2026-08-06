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
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context


MODEL_TYPE = "factor_governance_lightgbm"
MODEL_VERSION = "6.0"
FEATURE_SCHEMA_VERSION = "pit.v4.factor_regime_decision_lineage"
FEATURE_NAMES = [
    "current_entry_contribution",
    "current_net_contribution",
    "current_confidence",
    "rolling_sample_count",
    "rolling_positive_rate",
    "rolling_entry_contribution_avg",
    "rolling_net_contribution_avg",
    "rolling_confidence_avg",
    "rolling_entry_quality_avg",
    "rolling_hold_quality_avg",
    "rolling_exit_quality_avg",
    "rolling_pnl_avg",
    "rolling_mae_avg",
    "rolling_mfe_avg",
    "rolling_loss_rate",
    # v5.0 (pit.v3): regime 条件维度 —— regime_fit_score 此前被 SQL SELECT
    # 却从未进入特征集；现在模型能学到"因子在哪种市场状态下弱/强"。
    "current_regime_fit_score",
    "rolling_regime_fit_avg",
    "rolling_regime_fit_min",
    # v6.0 (pit.v4): 因子×regime 真条件绩效 —— 数据源升级为
    # decision_factor_snapshot JOIN decision_ledger(因子决策时点的真实
    # regime_id),按同 regime 历史聚合 positive_rate/pnl_avg/sample_count,
    # 替代交易级 regime_fit_score 的"全因子共享"局限。
    "same_regime_positive_rate",
    "same_regime_pnl_avg",
    "same_regime_sample_count",
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


def _current_row_label(item: dict[str, Any]) -> int:
    outcome = str(item.get("outcome_label") or "").lower()
    pnl = _safe_float(item.get("pnl"))
    net = _safe_float(item.get("net_contribution"))
    label = 1 if net > 0 and pnl >= 0 else 0
    if outcome in {"good_win", "small_win", "win", "good_loss"} and net >= 0:
        label = 1
    if outcome in {"bad_loss", "loss"} and net <= 0:
        label = 0
    return label


def _row_system_contaminated(item: dict[str, Any]) -> bool:
    notes = _loads(item.get("notes"), {})
    review = _loads(item.get("review_json"), {})
    system_issue = review.get("system_issue_context") if isinstance(review, dict) else {}
    return bool(
        (isinstance(notes, dict) and notes.get("system_contaminated"))
        or (
            isinstance(system_issue, dict)
            and system_issue.get("contaminates_learning")
        )
    )


def _rolling_factor_features(history: list[dict[str, Any]], *, window: int = 5) -> dict[str, float]:
    items = history[-max(2, int(window)):]
    current = items[-1]
    n = max(len(items), 1)
    regime_fits = [_safe_float(item.get("regime_fit_score")) for item in items]
    # v6.0 (pit.v4): 因子×regime 真条件绩效 —— 按"同 regime"聚合历史。
    # regime_id 来自 decision_ledger(因子决策时点的真实市场状态),
    # 每个因子只在自己的决策快照行上带 regime_id,不再是交易级共享值。
    current_regime = str(current.get("regime_id") or "")
    same_regime_items = (
        [item for item in items if str(item.get("regime_id") or "") == current_regime]
        if current_regime
        else []
    )
    sr_n = len(same_regime_items)
    if sr_n >= 3:
        same_regime_positive_rate = sum(_current_row_label(item) for item in same_regime_items) / sr_n
        same_regime_pnl_avg = sum(_safe_float(item.get("pnl")) for item in same_regime_items) / sr_n
    else:
        # 样本不足(<3)时退化为全局滚动值,不引入误导信号;
        # sample_count 字段保留实际值,模型可学到置信度。
        same_regime_positive_rate = sum(_current_row_label(item) for item in items) / n
        same_regime_pnl_avg = sum(_safe_float(item.get("pnl")) for item in items) / n
    return {
        "current_entry_contribution": _safe_float(current.get("entry_contribution")),
        "current_net_contribution": _safe_float(current.get("net_contribution")),
        "current_confidence": _safe_float(current.get("confidence")),
        "rolling_sample_count": float(n),
        "rolling_positive_rate": sum(_current_row_label(item) for item in items) / n,
        "rolling_entry_contribution_avg": sum(_safe_float(item.get("entry_contribution")) for item in items) / n,
        "rolling_net_contribution_avg": sum(_safe_float(item.get("net_contribution")) for item in items) / n,
        "rolling_confidence_avg": sum(_safe_float(item.get("confidence")) for item in items) / n,
        "rolling_entry_quality_avg": sum(_safe_float(item.get("entry_quality")) for item in items) / n,
        "rolling_hold_quality_avg": sum(_safe_float(item.get("hold_quality")) for item in items) / n,
        "rolling_exit_quality_avg": sum(_safe_float(item.get("exit_quality")) for item in items) / n,
        "rolling_pnl_avg": sum(_safe_float(item.get("pnl")) for item in items) / n,
        "rolling_mae_avg": sum(_safe_float(item.get("mae")) for item in items) / n,
        "rolling_mfe_avg": sum(_safe_float(item.get("mfe")) for item in items) / n,
        "rolling_loss_rate": sum(1 for item in items if _safe_float(item.get("pnl")) < 0.0) / n,
        # v5.0 (pit.v3): regime 条件维度。
        "current_regime_fit_score": _safe_float(current.get("regime_fit_score")),
        "rolling_regime_fit_avg": sum(regime_fits) / n,
        "rolling_regime_fit_min": min(regime_fits) if regime_fits else 0.0,
        # v6.0 (pit.v4): 因子×regime 真条件绩效。
        "same_regime_positive_rate": same_regime_positive_rate,
        "same_regime_pnl_avg": same_regime_pnl_avg,
        "same_regime_sample_count": float(sr_n),
    }


def _sample_from_row(
    row: Any,
    *,
    label: int | None = None,
    label_source: str = "current_factor_outcome",
    rolling_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    item = dict(row)
    outcome = str(item.get("outcome_label") or "").lower()
    pnl = _safe_float(item.get("pnl"))
    net = _safe_float(item.get("net_contribution"))
    confidence = _safe_float(item.get("confidence"))
    target_label = _current_row_label(item) if label is None else int(label)
    features = _rolling_factor_features(list(rolling_history or [item]))
    return {
        "sample_id": f"{item.get('review_id') or ''}:{item.get('factor') or ''}",
        "review_id": str(item.get("review_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "factor": str(item.get("factor") or ""),
        "created_at": _safe_float(item.get("created_at")),
        "pnl": pnl,
        "outcome_label": outcome,
        "label": target_label,
        "label_source": label_source,
        "features": {name: features.get(name, 0.0) for name in FEATURE_NAMES},
    }


class FactorGovernanceLightGBMService:
    """LightGBM sidecar model for factor governance.

    The model is shadow/advisory only. It scores reviewed factor contributions,
    writes every inference to audit storage, and can materialize proposed
    factor-scoped policy suggestions for human/governor review.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        artifact_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts" / MODEL_TYPE
        self.last_data_quality: dict[str, Any] = {}
        self._ensure_tables()

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

    def _ensure_tables(self) -> None:
        conn = self._conn()
        try:
            self._execute(conn,
                """
                CREATE TABLE IF NOT EXISTS factor_governance_shadow_audit (
                    inference_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_path TEXT DEFAULT '',
                    review_id TEXT DEFAULT '',
                    trade_id TEXT DEFAULT '',
                    position_id TEXT DEFAULT '',
                    factor TEXT DEFAULT '',
                    mode TEXT DEFAULT 'shadow',
                    positive_score REAL DEFAULT 0.0,
                    weakness_score REAL DEFAULT 0.0,
                    prediction INTEGER DEFAULT 0,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_factor_governance_audit_created
                ON factor_governance_shadow_audit(created_at)
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_factor_governance_audit_factor
                ON factor_governance_shadow_audit(factor, created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def load_samples(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = self._execute(conn,
                """
                SELECT *
                FROM (
                    SELECT f.id, f.review_id, f.trade_id, f.factor, f.entry_contribution,
                           f.hold_contribution, f.exit_contribution, f.net_contribution,
                           f.confidence, f.notes, r.position_id, r.entry_quality, r.hold_quality,
                           r.exit_quality, r.regime_fit_score, r.execution_quality,
                           r.pnl, r.mae, r.mfe, r.outcome_label, r.review_json, r.created_at,
                           dl.regime_id AS regime_id,
                           dl.regime_confidence AS regime_confidence,
                           dl.decision_ts AS decision_ts
                    FROM decision_factor_snapshot dfs
                    JOIN decision_ledger dl ON dl.decision_id = dfs.decision_id
                    JOIN trade_outcome_review r ON r.entry_decision_id = dfs.decision_id
                    JOIN factor_contribution_review f
                      ON f.review_id = r.review_id AND f.factor = dfs.factor
                    ORDER BY dl.decision_ts DESC, dfs.id DESC
                    LIMIT ?
                ) recent_factors
                ORDER BY decision_ts ASC, id ASC
                """,
                (int(limit),),
            ).fetchall()
            row_items = [dict(row) for row in rows]
            row_items = [item for item in row_items if not _row_system_contaminated(item)]
            review_row_counts: dict[str, int] = {}
            for item in row_items:
                review_id = str(item.get("review_id") or item.get("trade_id") or "")
                review_row_counts[review_id] = review_row_counts.get(review_id, 0) + 1
            # The old unbounded factor universe produced roughly 300 rows per
            # trade.  Current runtime selection is explicitly budgeted and
            # remains below 64 rows.  Do not mix these structural generations.
            for item in row_items:
                review_id = str(item.get("review_id") or item.get("trade_id") or "")
                notes = _loads(str(item.get("notes") or "{}"), {})
                explicit_generation = str(notes.get("factor_generation") or "")
                item["factor_generation"] = explicit_generation or (
                    "runtime_bounded_v1" if review_row_counts.get(review_id, 0) <= 64
                    else "legacy_unbounded"
                )
            latest_generation = str(row_items[-1].get("factor_generation") or "") if row_items else ""
            lineage_items = [
                item for item in row_items
                if str(item.get("factor_generation") or "") == latest_generation
            ]
            by_factor: dict[str, list[dict[str, Any]]] = {}
            for item in lineage_items:
                by_factor.setdefault(str(item.get("factor") or ""), []).append(item)
            samples = []
            for factor, factor_rows in by_factor.items():
                ordered = sorted(
                    factor_rows,
                    key=lambda item: (
                        _safe_float(item.get("decision_ts") or item.get("created_at")),
                        int(item.get("id") or 0),
                    ),
                )
                for idx, item in enumerate(ordered[:-1]):
                    if idx < 2:
                        continue
                    future = ordered[idx + 1]
                    samples.append(
                        _sample_from_row(
                            item,
                            label=_current_row_label(future),
                            label_source="next_same_factor_outcome_from_rolling_history",
                            rolling_history=ordered[max(0, idx - 4):idx + 1],
                        )
                    )
            samples.sort(key=lambda item: (item["created_at"], item["factor"]))
            selected_trades = {
                str(item.get("trade_id") or item.get("review_id") or "")
                for item in samples
                if str(item.get("trade_id") or item.get("review_id") or "")
            }
            factor_sample_counts = {
                factor: len(items) for factor, items in by_factor.items()
            }
            self.last_data_quality = {
                "schema_version": "model_training_data_quality.v1",
                "candidate_row_count": len(rows),
                "uncontaminated_row_count": len(row_items),
                "lineage_row_count": len(lineage_items),
                "selected_count": len(samples),
                "selected_distinct_trade_count": len(selected_trades),
                "factor_count": len(by_factor),
                "factor_generation": latest_generation,
                "generation_contract": "factor_universe_budget.v1",
                "excluded_other_generation_count": len(row_items) - len(lineage_items),
                "factors_below_10_samples": sum(count < 10 for count in factor_sample_counts.values()),
                "factors_below_20_samples": sum(count < 20 for count in factor_sample_counts.values()),
                "rolling_window": 5,
                "min_history": 3,
                "removed_constant_features": ["hold_contribution", "exit_contribution"],
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
        from sklearn.metrics import accuracy_score, balanced_accuracy_score, recall_score, roc_auc_score

        samples = self.load_samples(limit=limit)
        from backend.services.parity_replay import load_parity_learning_samples

        replay_samples = load_parity_learning_samples("factor")
        distinct_trade_ids = {
            str(item.get("trade_id") or item.get("review_id") or "")
            for item in samples
            if str(item.get("trade_id") or item.get("review_id") or "")
        }
        replay_trade_ids = {
            str(item.get("trade_id") or item.get("review_id") or "")
            for item in replay_samples
            if str(item.get("trade_id") or item.get("review_id") or "")
        }
        if (
            len(distinct_trade_ids) < 2
            or len(distinct_trade_ids | replay_trade_ids) < int(min_samples)
        ):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "distinct_trade_count": len(distinct_trade_ids),
                "replay_distinct_trade_count": len(replay_trade_ids),
                "data_quality": dict(self.last_data_quality),
                "error": "insufficient_distinct_factor_trades",
            }
        labels = [int(item["label"]) for item in samples]
        if len(set(labels)) < 2:
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "sample_count": len(samples),
                "positive_count": sum(labels),
                "error": "single_class_training_data",
            }

        ordered_trades: list[str] = []
        for item in samples:
            trade_id = str(item.get("trade_id") or item.get("review_id") or "")
            if trade_id and trade_id not in ordered_trades:
                ordered_trades.append(trade_id)
        holdout_groups = max(1, int(round(len(ordered_trades) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_trade_ids = set(ordered_trades[-holdout_groups:])
        train_samples = [item for item in samples if str(item.get("trade_id") or item.get("review_id") or "") not in holdout_trade_ids]
        holdout_samples = [item for item in samples if str(item.get("trade_id") or item.get("review_id") or "") in holdout_trade_ids]
        if not train_samples or not holdout_samples:
            return {"ok": False, "model_type": MODEL_TYPE, "error": "grouped_time_split_empty"}

        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]

        def _metrics(y_true: list[int], probs: Any) -> dict[str, Any]:
            preds = [1 if float(x) >= 0.5 else 0 for x in probs]
            positive_rate = sum(y_true) / max(len(y_true), 1)
            majority_label = 1 if positive_rate >= 0.5 else 0
            majority_preds = [majority_label] * len(y_true)
            auc = None
            if len(set(y_true)) > 1:
                try:
                    auc = round(float(roc_auc_score(y_true, probs)), 6)
                except Exception:
                    auc = None
            return {
                "count": len(y_true),
                "accuracy": round(float(accuracy_score(y_true, preds)), 6) if y_true else None,
                "balanced_accuracy": round(float(balanced_accuracy_score(y_true, preds)), 6) if y_true else None,
                "majority_baseline_accuracy": round(float(accuracy_score(y_true, majority_preds)), 6) if y_true else None,
                "auc": auc,
                "positive_rate": round(positive_rate, 6),
                "prediction_positive_rate": round(sum(preds) / max(len(preds), 1), 6),
                "negative_recall": round(float(recall_score(y_true, preds, pos_label=0, zero_division=0)), 6),
                "positive_recall": round(float(recall_score(y_true, preds, pos_label=1, zero_division=0)), 6),
                "majority_class": majority_label,
            }

        def _weights(items: list[dict[str, Any]], replay_scale: float = 1.0) -> list[float]:
            counts: dict[str, int] = {}
            for item in items:
                key = str(item.get("trade_id") or item.get("review_id") or "")
                counts[key] = counts.get(key, 0) + 1
            newest = max((_safe_float(item.get("created_at")) for item in items), default=0.0)
            return [
                (
                    math.exp(
                        -math.log(2.0)
                        * max(0.0, newest - _safe_float(item.get("created_at")))
                        / 86400.0
                        / 14.0
                    )
                    / max(1, counts.get(str(item.get("trade_id") or item.get("review_id") or ""), 1))
                    * replay_scale
                )
                for item in items
            ]

        def _fit(training: list[dict[str, Any]], weights: list[float]):
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

        real_weights = _weights(train_samples)
        baseline_model, baseline_x = _fit(train_samples, real_weights)
        baseline_holdout = _metrics(
            y_holdout, baseline_model.predict_proba(x_holdout)[:, 1]
        )
        replay_raw_weights = _weights(replay_samples)
        replay_scale = min(
            1.0,
            sum(real_weights) / max(sum(replay_raw_weights), 1e-12),
        )
        augmented_samples = train_samples + replay_samples
        augmented_model, augmented_x = _fit(
            augmented_samples,
            real_weights + [weight * replay_scale for weight in replay_raw_weights],
        )
        augmented_holdout = _metrics(
            y_holdout, augmented_model.predict_proba(x_holdout)[:, 1]
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
            for name, value in sorted(
                zip(FEATURE_NAMES, model.feature_importances_),
                key=lambda item: (-int(item[1]), item[0]),
            )
        ]
        metrics = {
            "train": _metrics([int(item["label"]) for item in selected_train], train_prob),
            "holdout": _metrics(y_holdout, holdout_prob),
            "sample_count": len(samples),
            "distinct_trade_count": len(ordered_trades),
            "replay_sample_count": len(replay_samples),
            "replay_distinct_trade_count": len(replay_trade_ids),
            "real_holdout_count": len(holdout_samples),
            "training_sources": {
                "real_train_samples": len(train_samples),
                "historical_replay_samples": len(replay_samples),
                "historical_replay_weight_scale": replay_scale,
                "selected": "real_plus_replay" if use_augmented else "real_baseline",
            },
            "augmentation_comparison": {
                "baseline_real_holdout": baseline_holdout,
                "augmented_real_holdout": augmented_holdout,
                "selected": "augmented" if use_augmented else "baseline",
            },
            "train_trade_count": len(set(ordered_trades) - holdout_trade_ids),
            "holdout_trade_count": len(holdout_trade_ids),
            "feature_count": len(FEATURE_NAMES),
            "split": "time_ordered_grouped_purged",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "holdout_ratio": float(holdout_ratio),
            "train_count": len(train_samples),
            "holdout_count": len(holdout_samples),
            "label_distribution": {"negative": labels.count(0), "positive": labels.count(1)},
            "safe_for_live_trading": False,
            "data_quality": dict(self.last_data_quality),
            "label_contract": {
                "label": "next_same_factor_outcome_from_rolling_history",
                "trade_balanced_training_weight": True,
                "recency_half_life_days": 14.0,
                "factor_generation": self.last_data_quality.get("factor_generation"),
            },
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, model_path)
        artifact = {
            "schema_version": "factor_governance_lightgbm_artifact.v2",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "label": "next_same_factor_positive_contribution_from_rolling_history",
            "sample_window": {"limit": int(limit), "sample_count": len(samples)},
            "training_lineage": dict(self.last_data_quality),
            "metrics": metrics,
            "explainability": {
                "feature_importance": feature_importance,
                "summary": "LightGBM shadow-only factor governance model. Labels use the next same-factor outcome, not the current row, to avoid same-row leakage. Scores are advisory and logged.",
            },
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_factor_weights": False,
                "can_change_risk_limits": False,
            },
            "guardrails": [
                "MUST NOT place orders",
                "MUST NOT close positions",
                "MUST NOT mutate factor_portfolio_weights",
                "MUST write audit records before suggestions are reviewed",
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
                    "holdout_auc": metrics["holdout"]["auc"],
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

    def _existing_shadow_sample_ids(self, *, artifact_path: str) -> set[str]:
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT inference_id
                FROM factor_governance_shadow_audit
                WHERE model_type=? AND artifact_path=?
                """,
                (MODEL_TYPE, artifact_path),
            ).fetchall()
            prefix = f"{MODEL_TYPE}:"
            sample_ids = set()
            for row in rows:
                inference_id = str(row["inference_id"] or "")
                if not inference_id.startswith(prefix):
                    continue
                sample_id = inference_id[len(prefix):].rsplit(":", 1)[0]
                if sample_id:
                    sample_ids.add(sample_id)
            return sample_ids
        finally:
            conn.close()

    def score_samples(
        self,
        *,
        artifact_path: str | Path | None = None,
        limit: int = 200,
        mode: str = "shadow",
        skip_existing: bool = False,
        materialize: bool = False,
        min_weakness_score: float = 0.65,
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
            context={"mode": mode, "operation": "factor_governance_score_samples"},
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
                    "suggestions": [],
                    "capabilities": {
                        "live_trading": False,
                        "advisory_only": True,
                        "shadow_only": True,
                    },
                }
        x = pd.DataFrame([item["features"] for item in samples], columns=feature_names)
        probs = model.predict_proba(x)[:, 1]
        items = []
        for sample, prob in zip(samples, probs):
            items.append(self._persist_inference(artifact, sample, float(prob), mode=mode))
        suggestions = self.build_advisories(
            items=items,
            materialize=materialize,
            min_weakness_score=min_weakness_score,
        )
        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": str(path),
            "count": len(items),
            "items": items,
            "suggestions": suggestions,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
        }

    def re_review_quarantined_factor(
        self,
        *,
        factor: str,
        artifact_path: str | Path | None = None,
        mode: str = "quarantine_review",
    ) -> dict[str, Any]:
        """Re-score one quarantined factor with the newest model artifact.

        Quarantined factors produce no new trade reviews, so the routine
        score_samples() sweep (bounded to the most recent rows) never reaches
        them and their model evidence freezes at the pre-quarantine verdict.
        This rebuilds the factor's rolling samples from its full historical
        review rows (bypassing the recent-rows limit) and runs the latest
        artifact over them, so the restore path can read a fresh verdict
        instead of a stale 0.97 weakness.

        Idempotent per (artifact, factor): skips when the latest artifact has
        already scored this factor in quarantine_review mode.  This is the
        automated replacement for a human re-evaluating a frozen factor.
        """
        dep_error = _dependency_error()
        if dep_error:
            return {
                "ok": False,
                "error": "dependency_missing",
                "detail": dep_error,
            }
        import joblib

        resolved_artifact = str(artifact_path or self.latest_artifact_path())
        if not resolved_artifact:
            return {
                "ok": False,
                "error": "artifact_missing",
                "artifact_path": "",
            }
        path = Path(resolved_artifact)
        if not path.exists():
            return {
                "ok": False,
                "error": "artifact_missing",
                "artifact_path": str(path),
            }
        artifact = json.loads(path.read_text(encoding="utf-8"))
        permission = validate_model_artifact(
            artifact,
            model_type=MODEL_TYPE,
            db_path=self.db_path,
            context={
                "mode": mode,
                "operation": "factor_governance_quarantine_review",
            },
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
            return {
                "ok": False,
                "error": "model_file_missing",
                "model_file": str(model_file),
            }
        bundle = joblib.load(model_file)
        model = bundle["model"]
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        artifact_ref = str(artifact.get("artifact_path") or str(path))
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT inference_id
                FROM factor_governance_shadow_audit
                WHERE factor=? AND artifact_path=? AND mode=?
                """,
                (factor, artifact_ref, mode),
            ).fetchall()
        finally:
            conn.close()
        if rows:
            return {
                "ok": True,
                "skipped": True,
                "reason": "already_reviewed_by_latest_artifact",
                "factor": factor,
                "artifact_path": artifact_ref,
            }
        samples = self._load_factor_review_samples(factor)
        if not samples:
            return {
                "ok": False,
                "error": "no_historical_review_samples",
                "factor": factor,
            }
        import pandas as pd

        x = pd.DataFrame(
            [item["features"] for item in samples],
            columns=feature_names,
        )
        probs = model.predict_proba(x)[:, 1]
        items = []
        for sample, prob in zip(samples, probs):
            items.append(
                self._persist_inference(artifact, sample, float(prob), mode=mode)
            )
        weakness = max(
            float(item.get("weakness_score") or 0.0) for item in items
        )
        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": artifact_ref,
            "factor": factor,
            "count": len(items),
            "weakness": weakness,
            "positive_score": min(
                float(item.get("positive_score") or 0.0) for item in items
            ),
            "inference_id": str(items[-1].get("inference_id") or ""),
            "items": items,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
        }

    def _load_factor_review_samples(
        self, factor: str, *, limit: int = 400
    ) -> list[dict[str, Any]]:
        """Load one factor's full historical review rows and rebuild rolling
        samples (same feature pipeline as load_samples, unbounded by the
        recent-rows limit so quarantined factors are reachable)."""
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT f.id, f.review_id, f.trade_id, f.factor, f.entry_contribution,
                       f.hold_contribution, f.exit_contribution, f.net_contribution,
                       f.confidence, f.notes, r.position_id, r.entry_quality, r.hold_quality,
                       r.exit_quality, r.regime_fit_score, r.execution_quality,
                       r.pnl, r.mae, r.mfe, r.outcome_label, r.review_json, r.created_at,
                       dl.regime_id AS regime_id,
                       dl.regime_confidence AS regime_confidence,
                       dl.decision_ts AS decision_ts
                FROM decision_factor_snapshot dfs
                JOIN decision_ledger dl ON dl.decision_id = dfs.decision_id
                JOIN trade_outcome_review r ON r.entry_decision_id = dfs.decision_id
                JOIN factor_contribution_review f
                  ON f.review_id = r.review_id AND f.factor = dfs.factor
                WHERE dfs.factor = ?
                ORDER BY dl.decision_ts ASC, dfs.id ASC
                LIMIT ?
                """,
                (factor, int(limit)),
            ).fetchall()
            row_items = [dict(row) for row in rows]
            row_items = [
                item for item in row_items if not _row_system_contaminated(item)
            ]
            ordered = sorted(
                row_items,
                key=lambda item: (
                    _safe_float(item.get("decision_ts") or item.get("created_at")),
                    int(item.get("id") or 0),
                ),
            )
            samples = []
            for idx, item in enumerate(ordered[:-1]):
                if idx < 2:
                    continue
                future = ordered[idx + 1]
                samples.append(
                    _sample_from_row(
                        item,
                        label=_current_row_label(future),
                        label_source="next_same_factor_outcome_from_rolling_history",
                        rolling_history=ordered[max(0, idx - 4):idx + 1],
                    )
                )
            samples.sort(key=lambda item: (item["created_at"], item["factor"]))
            return samples
        finally:
            conn.close()

    def _persist_inference(
        self,
        artifact: dict[str, Any],
        sample: dict[str, Any],
        positive_score: float,
        *,
        mode: str,
    ) -> dict[str, Any]:
        now = time.time()
        weakness = max(0.0, min(1.0, 1.0 - float(positive_score)))
        prediction = 1 if positive_score >= 0.5 else 0
        weakness_bucket = "high_factor_weakness" if weakness >= 0.65 else "medium_factor_weakness" if weakness >= 0.4 else "low_factor_weakness"
        result = {
            "schema_version": "factor_governance_shadow_result.v1",
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "positive_score": round(float(positive_score), 8),
            "weakness_score": round(weakness, 8),
            "prediction": prediction,
            "prediction_label": "positive_factor_contribution" if prediction else "weak_factor_contribution",
            "weakness_bucket": weakness_bucket,
            "advice": "review_only",
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
            },
            "guardrails": list(artifact.get("guardrails") or []),
        }
        result["source_agent"] = "lightgbm_shadow_models"
        result["authority_verdict"] = AgentAuthorityRegistryService().evaluate(
            "lightgbm_shadow_models",
            "model_stage",
            "shadow_model_audit",
            requested_writes=["factor_governance_shadow_audit"],
            status=mode,
            impact_level="shadow",
        )
        payload = {
            "sample_id": sample["sample_id"],
            "review_id": sample["review_id"],
            "trade_id": sample["trade_id"],
            "position_id": sample["position_id"],
            "factor": sample["factor"],
            "features": sample["features"],
            "label": sample["label"],
            "pnl": sample["pnl"],
            "outcome_label": sample["outcome_label"],
            "label_source": sample.get("label_source", ""),
            "source_agent": "lightgbm_shadow_models",
            "authority_verdict": result["authority_verdict"],
        }
        inference_id = f"{MODEL_TYPE}:{sample['sample_id']}:{int(now * 1000)}"
        conn = self._conn()
        try:
            self._execute(conn,
                """
                INSERT INTO factor_governance_shadow_audit
                (inference_id, model_type, model_version, artifact_path, review_id,
                 trade_id, position_id, factor, mode, positive_score, weakness_score,
                 prediction, payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    MODEL_TYPE,
                    str(artifact.get("model_version") or MODEL_VERSION),
                    str(artifact.get("artifact_path") or ""),
                    sample["review_id"],
                    sample["trade_id"],
                    sample["position_id"],
                    sample["factor"],
                    mode,
                    float(positive_score),
                    float(weakness),
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
            "review_id": sample["review_id"],
            "trade_id": sample["trade_id"],
            "position_id": sample["position_id"],
            "factor": sample["factor"],
            "positive_score": result["positive_score"],
            "weakness_score": result["weakness_score"],
            "prediction": prediction,
            "weakness_bucket": weakness_bucket,
            "created_at": now,
        }

    def build_advisories(
        self,
        *,
        items: list[dict[str, Any]] | None = None,
        materialize: bool = False,
        min_weakness_score: float = 0.65,
        governed_action: str = "review_factor_weight_or_template",
        min_weak_sample_count: int = 1,
        factor_allowlist: set[str] | None = None,
        evidence_context_by_factor: dict[str, dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        source_items = items if items is not None else self.list_audits(limit=500)["items"]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in source_items:
            factor = str(item.get("factor") or "")
            if not factor:
                continue
            if factor_allowlist is not None and factor not in factor_allowlist:
                continue
            grouped.setdefault(factor, []).append(item)
        suggestions = []
        for factor, factor_items in sorted(grouped.items()):
            weak_items = [
                item for item in factor_items
                if _safe_float(item.get("weakness_score")) >= float(min_weakness_score)
            ]
            if len(weak_items) < max(1, int(min_weak_sample_count)):
                continue
            avg_weakness = sum(_safe_float(item.get("weakness_score")) for item in weak_items) / max(len(weak_items), 1)
            confidence = min(0.92, max(0.55, avg_weakness * min(1.0, len(weak_items) / 5.0)))
            identity = f"{factor}:{len(weak_items)}:{round(avg_weakness, 4)}"
            if governed_action != "review_factor_weight_or_template" or min_weak_sample_count > 1:
                identity = ":".join(
                    [
                        factor,
                        governed_action,
                        ",".join(sorted(str(item.get("review_id") or item.get("inference_id") or "") for item in weak_items)),
                        str(round(avg_weakness, 4)),
                    ]
                )
            suggestion_id = "fgm_" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:16]
            evidence = {
                "schema_version": "factor_governance_advisory.v1",
                "model_type": MODEL_TYPE,
                "sample_count": len(factor_items),
                "weak_sample_count": len(weak_items),
                "avg_weakness_score": round(avg_weakness, 6),
                "min_weakness_score": float(min_weakness_score),
                "latest_inference_ids": [str(item.get("inference_id") or "") for item in weak_items[:5]],
                "advisory_only": True,
                "approval_path": "governor_review_then_offline_replay",
            }
            evidence.update(dict((evidence_context_by_factor or {}).get(factor) or {}))
            suggestions.append(
                {
                    "suggestion_id": suggestion_id,
                    "scope_type": "factor",
                    "scope_key": factor,
                    "action": governed_action,
                    "confidence": round(confidence, 4),
                    "reason": "LightGBM shadow model detected repeated weak factor contribution samples",
                    "evidence": attach_policy_suggestion_agent_context(
                        evidence,
                        source_agent="lightgbm_shadow_models",
                        scope_type="factor",
                        scope_key=factor,
                        action=governed_action,
                        requested_writes=[],
                        status="proposed",
                        impact_level="shadow",
                        db_path=self.db_path,
                    ),
                    "status": "proposed",
                    "advisory_only": True,
                }
            )
        if materialize and suggestions:
            self._materialize_suggestions(suggestions)
        return {
            "schema_version": "factor_governance_advisory_set.v1",
            "model_type": MODEL_TYPE,
            "advisory_only": True,
            "materialized": bool(materialize),
            "items": suggestions,
            "count": len(suggestions),
        }

    def materialize_demo_governance_advisories(
        self,
        *,
        limit: int = 5000,
        min_weakness_score: float = 0.85,
        min_weak_sample_count: int = 2,
        max_factors: int = 10,
    ) -> dict[str, Any]:
        """Bridge strong model evidence into the guarded demo governance queue.

        The LightGBM model remains advisory-only.  This method only creates a
        factor-scoped ``policy_suggestion`` with a concrete, whitelisted
        ``downweight`` action.  Approval and application remain owned by the
        existing governor, DecisionPolicy, RiskPolicyService, and weight
        mutation service.
        """
        from backend.services.factor_catalog import build_factor_catalog
        from backend.services.model_influence import ModelInfluenceService
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        influence = ModelInfluenceService(self.db_path)
        policy = influence.active_policy(MODEL_TYPE, cfg)

        catalog = build_factor_catalog(self.db_path)
        active = {
            str(item.get("factor_id") or ""): item
            for item in catalog
            if bool(item.get("used_in_score"))
            and bool(item.get("enabled", True))
            and bool(item.get("eligible_for_live", True))
            and str(item.get("lifecycle_status") or "ACTIVE").upper() not in {"DEAD", "QUARANTINE"}
            and str(item.get("role") or "") == "alpha"
        }
        stale_superseded = self._supersede_inactive_demo_suggestions(set(active))
        if not policy or "suggest_downweight" not in set(policy.get("allowed_effects") or []):
            return {
                "schema_version": "factor_governance_demo_bridge.v1",
                "enabled": False,
                "materialized": False,
                "count": 0,
                "eligible_active_factors": len(active),
                "stale_superseded": stale_superseded,
                "reason": "factor_model_influence_inactive",
            }
        audits = self.list_audits(limit=max(100, int(limit))).get("items") or []
        artifact_path = str(policy.get("artifact_path") or "")
        if artifact_path:
            audits = [item for item in audits if str(item.get("artifact_path") or "") == artifact_path]
        if not active or not audits:
            return {
                "schema_version": "factor_governance_demo_bridge.v1",
                "enabled": True,
                "materialized": False,
                "count": 0,
                "eligible_active_factors": len(active),
                "stale_superseded": stale_superseded,
                "reason": "no_active_factors_or_model_audits",
            }

        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in audits:
            factor = str(item.get("factor") or "")
            if factor in active:
                grouped.setdefault(factor, []).append(item)
        ranked: list[tuple[float, str]] = []
        for factor, items in grouped.items():
            weak = [
                item for item in items
                if _safe_float(item.get("weakness_score")) >= float(min_weakness_score)
            ]
            if len(weak) < max(1, int(min_weak_sample_count)):
                continue
            avg_weakness = sum(_safe_float(item.get("weakness_score")) for item in weak) / max(len(weak), 1)
            ranked.append((avg_weakness, factor))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        selected = {factor for _, factor in ranked[:max(1, int(max_factors))]}
        context = {
            factor: {
                "bridge_schema_version": "factor_governance_demo_bridge.v1",
                "bridge": {
                    "automatic_demo": True,
                    "demo_nursery": True,
                    "actor": "system:autonomous_learning.demo_nursery_model_governance",
                    "service": "FactorGovernanceLightGBMService.materialize_demo_governance_advisories",
                    "manual_only": False,
                },
                "model_advisory": True,
                "model_influence_active": True,
                "model_stage": str(policy.get("stage") or ""),
                "feature_schema_version": str(policy.get("feature_schema_version") or ""),
                "model_action": "review_factor_weight_or_template",
                "governed_action": "downweight",
                "active_factor_context": {
                    "used_in_score": True,
                    "role": "alpha",
                    "weight": float(active[factor].get("weight") or 0.0),
                    "health_score": float(active[factor].get("health_score") or 0.0),
                    "health_status": str(active[factor].get("health_status") or "UNKNOWN"),
                },
                "governance_consumer": "RuleEvolutionGovernor+FactorWeightChangeService",
                "direct_model_application": False,
                "downstream_gates_required": [
                    "demo_governor_review",
                    "DecisionPolicy",
                    "RiskPolicyService",
                    "runtime_overlay_snapshot",
                    "learning_application_effect",
                ],
            }
            for factor in selected
        }
        result = self.build_advisories(
            items=audits,
            materialize=False,
            min_weakness_score=min_weakness_score,
            governed_action="downweight",
            min_weak_sample_count=min_weak_sample_count,
            factor_allowlist=selected,
            evidence_context_by_factor=context,
        )
        suggestions = list(result.get("items") or [])
        if suggestions:
            self._materialize_suggestions(suggestions)
            for suggestion in suggestions:
                influence.audit(
                    model_type=MODEL_TYPE,
                    policy=policy,
                    subject_id=str(suggestion.get("scope_key") or ""),
                    rule_decision={"governance_required": True, "direct_weight_change": False},
                    model_result=dict(suggestion.get("evidence") or {}),
                    fused_decision={
                        "suggestion_id": suggestion.get("suggestion_id"),
                        "action": "downweight",
                        "status": "proposed",
                    },
                    applied=True,
                    reason="model_factor_downweight_suggestion",
                )
        return {
            **result,
            "schema_version": "factor_governance_demo_bridge.v1",
            "enabled": True,
            "materialized": bool(suggestions),
            "eligible_active_factors": len(active),
            "selected_factors": sorted(selected),
            "stale_superseded": stale_superseded,
            "min_weakness_score": float(min_weakness_score),
            "min_weak_sample_count": int(min_weak_sample_count),
        }

    def _supersede_inactive_demo_suggestions(self, active_factors: set[str]) -> int:
        """Close stale model bridges after their factor leaves the runtime score."""
        changed = 0
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                """
                SELECT suggestion_id, scope_key, evidence_json
                FROM policy_suggestion
                WHERE scope_type='factor'
                  AND action='downweight'
                  AND status IN ('proposed', 'approved')
                """,
            ).fetchall()
            now = time.time()
            for row in rows:
                evidence = _loads(row["evidence_json"], {})
                bridge = evidence.get("bridge") if isinstance(evidence, dict) else {}
                if not (
                    isinstance(evidence, dict)
                    and evidence.get("model_type") == MODEL_TYPE
                    and isinstance(bridge, dict)
                    and bridge.get("automatic_demo") is True
                    and bridge.get("demo_nursery") is True
                ):
                    continue
                if str(row["scope_key"] or "") in active_factors:
                    continue
                self._execute(
                    conn,
                    """
                    UPDATE policy_suggestion
                    SET status='superseded', reviewed_at=?,
                        review_note='superseded: factor is no longer active in runtime score'
                    WHERE suggestion_id=?
                    """,
                    (now, str(row["suggestion_id"] or "")),
                )
                changed += 1
            conn.commit()
            return changed
        finally:
            conn.close()

    def _materialize_suggestions(self, suggestions: list[dict[str, Any]]) -> None:
        conn = self._conn()
        try:
            now = time.time()
            for item in suggestions:
                self._execute(conn,
                    """
                    INSERT INTO policy_suggestion
                    (suggestion_id, scope_type, scope_key, action, confidence, reason,
                     evidence_json, status, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE(
                        (SELECT status FROM policy_suggestion WHERE suggestion_id=?),
                        'proposed'
                    ), COALESCE(
                        (SELECT created_at FROM policy_suggestion WHERE suggestion_id=?),
                        ?
                    ))
                    ON CONFLICT(suggestion_id) DO UPDATE SET
                        scope_type=excluded.scope_type,
                        scope_key=excluded.scope_key,
                        action=excluded.action,
                        confidence=excluded.confidence,
                        reason=excluded.reason,
                        evidence_json=excluded.evidence_json,
                        status=excluded.status,
                        created_at=excluded.created_at
                    """,
                    (
                        item["suggestion_id"],
                        item["scope_type"],
                        item["scope_key"],
                        item["action"],
                        float(item["confidence"]),
                        item["reason"],
                        json.dumps(item["evidence"], ensure_ascii=False, sort_keys=True),
                        item["suggestion_id"],
                        item["suggestion_id"],
                        now,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def list_audits(self, *, limit: int = 100, factor: str | None = None) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        if factor:
            clauses.append("factor=?")
            params.append(str(factor))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = self._execute(conn,
                f"""
                SELECT *
                FROM factor_governance_shadow_audit
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
                        "review_id": str(row["review_id"] or ""),
                        "trade_id": str(row["trade_id"] or ""),
                        "position_id": str(row["position_id"] or ""),
                        "factor": str(row["factor"] or ""),
                        "mode": str(row["mode"] or ""),
                        "positive_score": _safe_float(row["positive_score"]),
                        "weakness_score": _safe_float(row["weakness_score"]),
                        "prediction": int(row["prediction"] or 0),
                        "payload": _loads(row["payload_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()
