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


MODEL_TYPE = "position_quality_lightgbm"
MODEL_VERSION = "3.0"
FEATURE_SCHEMA_VERSION = "pit.v2.position_h30"
FEATURE_NAMES = [
    "current_pnl",
    "mfe",
    "mae",
    "giveback_ratio",
    "profit_capture_ratio",
    "time_in_profit",
    "holding_efficiency",
    "time_decay_score",
    "holding_seconds",
    "stop_loss_progress",
    "take_profit_progress",
    "holding_timeout_ratio",
    "completed_bars_after_entry",
    "hard_risk_active",
    "thesis_broken",
    "thesis_weakening",
    "regime_shift_confirmed",
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
        import lightgbm  # noqa: F401
        import joblib  # noqa: F401
        import pandas  # noqa: F401
        import sklearn  # noqa: F401
    except Exception as exc:
        return str(exc)
    return ""


def _label(row: dict[str, Any], payload: dict[str, Any]) -> int:
    outcome = str(row.get("outcome_label") or "").lower()
    pnl = _safe_float(row.get("pnl"))
    if outcome in {"good_win", "small_win", "win", "good_loss"}:
        return 1
    if outcome in {"bad_loss", "loss"}:
        return 0
    if outcome == "small_loss":
        return 0
    tags = {str(x).lower() for x in payload.get("failure_tags") or []}
    if "good_loss" in tags:
        return 1
    return 1 if pnl > 0 else 0


def _features_from_review(row: dict[str, Any]) -> dict[str, float]:
    payload = _loads(str(row.get("review_json") or "{}"), {})
    close_reason = str(payload.get("close_reason") or "").lower()
    thesis = str(payload.get("thesis_status_at_exit") or payload.get("thesis_status") or "").lower()
    regime_shift = str(payload.get("regime_shift_at_exit") or payload.get("regime_shift") or "").lower()
    features = {
        "mfe": _safe_float(row.get("mfe") if row.get("mfe") is not None else payload.get("mfe")),
        "mae": _safe_float(row.get("mae") if row.get("mae") is not None else payload.get("mae")),
        "giveback_ratio": _safe_float(payload.get("giveback_ratio")),
        "profit_capture_ratio": _safe_float(payload.get("profit_capture_ratio")),
        "time_in_profit": _safe_float(payload.get("time_in_profit") or payload.get("time_in_profit_seconds")),
        "holding_efficiency": _safe_float(payload.get("holding_efficiency")),
        "time_decay_score": _safe_float(payload.get("time_decay_score")),
        "holding_seconds": _safe_float(payload.get("holding_seconds")),
        "entry_quality": _safe_float(row.get("entry_quality") if row.get("entry_quality") is not None else payload.get("entry_quality")),
        "hold_quality": _safe_float(row.get("hold_quality") if row.get("hold_quality") is not None else payload.get("hold_quality")),
        "exit_quality": _safe_float(row.get("exit_quality") if row.get("exit_quality") is not None else payload.get("exit_quality")),
        "regime_fit_score": _safe_float(row.get("regime_fit_score") if row.get("regime_fit_score") is not None else payload.get("regime_fit_score")),
        "execution_quality": _safe_float(row.get("execution_quality") if row.get("execution_quality") is not None else payload.get("execution_quality")),
        "thesis_broken": 1.0 if thesis == "broken" else 0.0,
        "thesis_weakening": 1.0 if thesis == "weakening" else 0.0,
        "regime_shift_confirmed": 1.0 if regime_shift == "confirmed" else 0.0,
        "close_reason_thesis_broken": 1.0 if close_reason == "thesis_broken" else 0.0,
        "close_reason_broker_close": 1.0 if close_reason == "broker_close" else 0.0,
    }
    return {name: features.get(name, 0.0) for name in FEATURE_NAMES}


def _features_from_trace(row: dict[str, Any]) -> dict[str, float]:
    verdict = _loads(str(row.get("verdict_json") or "{}"), {})
    evidence = dict(verdict.get("evidence") or {})
    features = {
        "current_pnl": _safe_float(evidence.get("current_pnl")),
        "mfe": _safe_float(evidence.get("mfe")),
        "mae": _safe_float(evidence.get("mae")),
        "giveback_ratio": _safe_float(evidence.get("giveback_ratio")),
        "profit_capture_ratio": _safe_float(evidence.get("profit_capture_ratio")),
        "time_in_profit": _safe_float(evidence.get("time_in_profit")),
        "holding_efficiency": _safe_float(evidence.get("holding_efficiency")),
        "time_decay_score": _safe_float(evidence.get("time_decay_score")),
        "holding_seconds": _safe_float(evidence.get("holding_seconds")),
        "stop_loss_progress": _safe_float(evidence.get("stop_loss_progress")),
        "take_profit_progress": _safe_float(evidence.get("take_profit_progress")),
        "holding_timeout_ratio": _safe_float(evidence.get("holding_timeout_ratio")),
        "completed_bars_after_entry": _safe_float(
            evidence.get("completed_bars_after_entry"),
            math.floor(_safe_float(evidence.get("holding_seconds")) / 300.0),
        ),
        "hard_risk_active": 1.0 if bool(evidence.get("hard_risk_active")) else 0.0,
        "thesis_broken": 1.0 if str(evidence.get("thesis_status") or "").lower() == "broken" else 0.0,
        "thesis_weakening": 1.0 if str(evidence.get("thesis_status") or "").lower() == "weakening" else 0.0,
        "regime_shift_confirmed": 1.0 if str(evidence.get("regime_shift") or "").lower() == "confirmed" else 0.0,
    }
    return {name: features.get(name, 0.0) for name in FEATURE_NAMES}


def _sample_from_row(row: Any) -> dict[str, Any]:
    item = dict(row)
    payload = _loads(str(item.get("review_json") or "{}"), {})
    return {
        "sample_id": str(item.get("review_id") or item.get("position_id") or ""),
        "review_id": str(item.get("review_id") or ""),
        "trade_id": str(item.get("trade_id") or ""),
        "position_id": str(item.get("position_id") or ""),
        "created_at": _safe_float(item.get("created_at")),
        "pnl": _safe_float(item.get("pnl")),
        "outcome_label": str(item.get("outcome_label") or ""),
        "label": _label(item, payload),
        "features": _features_from_review(item),
    }


class PositionQualityLightGBMService:
    """LightGBM sidecar model for position quality scoring.

    The service is intentionally shadow-only. It trains from reviewed trades,
    stores artifacts, and logs every inference to state.db. It cannot place
    orders, close positions, or mutate live risk parameters.
    """

    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        artifact_dir: str | Path | None = None,
    ):
        self.db_path = Path(db_path)
        self.artifact_dir = Path(artifact_dir) if artifact_dir else DATA_DIR / "model_artifacts" / MODEL_TYPE
        self._live_bundle_cache: tuple[str, int, dict[str, Any], Any] | None = None
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
            self._execute(conn,
                """
                CREATE TABLE IF NOT EXISTS position_quality_shadow_audit (
                    inference_id TEXT PRIMARY KEY,
                    model_type TEXT NOT NULL,
                    model_version TEXT DEFAULT '',
                    artifact_path TEXT DEFAULT '',
                    review_id TEXT DEFAULT '',
                    trade_id TEXT DEFAULT '',
                    position_id TEXT DEFAULT '',
                    mode TEXT DEFAULT 'shadow',
                    hold_score REAL DEFAULT 0.0,
                    exit_risk_score REAL DEFAULT 0.0,
                    prediction INTEGER DEFAULT 0,
                    payload_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_created
                ON position_quality_shadow_audit(created_at)
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_position_quality_shadow_audit_position
                ON position_quality_shadow_audit(position_id, created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    def load_samples(
        self,
        *,
        limit: int = 4000,
        horizon_minutes: int = 30,
        pnl_tolerance: float = 0.25,
    ) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            rows = self._execute(conn,
                """
                SELECT * FROM (
                    SELECT t.trace_id, t.position_id, t.trade_id, t.event_ts,
                           t.verdict_json, t.context_json, t.template_id,
                           t.template_version, t.config_version, t.config_hash,
                           r.review_id, r.pnl, r.outcome_label, r.failure_tags_json,
                           r.review_json, r.created_at AS review_created_at
                    FROM position_supervisor_trace t
                    JOIN trade_outcome_review r ON CAST(r.position_id AS TEXT)=CAST(t.position_id AS TEXT)
                    WHERE t.stage='evaluated' AND COALESCE(t.trace_integrity, 'full')='full'
                    ORDER BY t.event_ts DESC
                    LIMIT ?
                ) recent_traces ORDER BY event_ts ASC
                """,
                (max(int(limit) * 4, int(limit)),),
            ).fetchall()
            items = [dict(row) for row in rows]
            latest_template_version = str(items[-1].get("template_version") or "") if items else ""
            lineage_items = [
                item for item in items
                if str(item.get("template_version") or "") == latest_template_version
                and str(item.get("config_hash") or "")
            ]
            by_position: dict[str, list[dict[str, Any]]] = {}
            for item in lineage_items:
                by_position.setdefault(str(item.get("position_id") or ""), []).append(item)
            samples: list[dict[str, Any]] = []
            horizon_seconds = max(5, int(horizon_minutes)) * 60.0
            for position_id, position_rows in by_position.items():
                ordered = sorted(position_rows, key=lambda item: _safe_float(item.get("event_ts")))
                if not position_id or not ordered:
                    continue
                first_ts = _safe_float(ordered[0].get("event_ts"))
                seen_buckets: set[int] = set()
                for item in ordered:
                    event_ts = _safe_float(item.get("event_ts"))
                    target_ts = event_ts + horizon_seconds
                    bucket = int(max(0.0, event_ts - first_ts) // horizon_seconds)
                    if bucket in seen_buckets:
                        continue
                    review_close_ts = _safe_float(item.get("review_created_at"))
                    target_pnl: float | None = None
                    target_source = ""
                    if review_close_ts > event_ts and review_close_ts <= target_ts:
                        target_pnl = _safe_float(item.get("pnl"))
                        target_source = "closed_before_horizon"
                    else:
                        future_row = next(
                            (
                                candidate for candidate in ordered
                                if target_ts <= _safe_float(candidate.get("event_ts")) <= target_ts + 900.0
                            ),
                            None,
                        )
                        if future_row is not None:
                            future_verdict = _loads(str(future_row.get("verdict_json") or "{}"), {})
                            future_evidence = dict(future_verdict.get("evidence") or {})
                            if "current_pnl" in future_evidence:
                                target_pnl = _safe_float(future_evidence.get("current_pnl"))
                                target_source = "trace_at_horizon"
                    if target_pnl is None:
                        continue
                    features = _features_from_trace(item)
                    current_pnl = _safe_float(features.get("current_pnl"))
                    pnl_delta = target_pnl - current_pnl
                    seen_buckets.add(bucket)
                    samples.append({
                        "sample_id": str(item.get("trace_id") or f"{position_id}:{bucket}"),
                        "review_id": str(item.get("review_id") or ""),
                        "trade_id": str(item.get("trade_id") or ""),
                        "position_id": position_id,
                        "created_at": event_ts,
                        "pnl": _safe_float(item.get("pnl")),
                        "outcome_label": str(item.get("outcome_label") or ""),
                        "label": 1 if pnl_delta >= -abs(float(pnl_tolerance)) else 0,
                        "label_source": f"fixed_horizon_{int(horizon_minutes)}m_pnl_delta",
                        "target_pnl": target_pnl,
                        "target_pnl_delta": pnl_delta,
                        "target_source": target_source,
                        "features": features,
                        "feature_schema_version": FEATURE_SCHEMA_VERSION,
                        "config_hash": str(item.get("config_hash") or ""),
                        "template_version": latest_template_version,
                    })
            samples = sorted(samples, key=lambda item: item["created_at"])[-int(limit):]
            self.last_data_quality = {
                "schema_version": "model_training_data_quality.v1",
                "candidate_trace_count": len(items),
                "lineage_trace_count": len(lineage_items),
                "selected_count": len(samples),
                "selected_position_count": len({item["position_id"] for item in samples}),
                "template_version": latest_template_version,
                "config_hashes": sorted({item["config_hash"] for item in samples}),
                "horizon_minutes": int(horizon_minutes),
                "pnl_tolerance": float(pnl_tolerance),
                "excluded_other_lineage_count": len(items) - len(lineage_items),
            }
            return samples
        finally:
            conn.close()

    def train(
        self,
        *,
        limit: int = 4000,
        holdout_ratio: float = 0.25,
        min_samples: int = 20,
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
        if len(samples) < int(min_samples):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "data_quality": dict(self.last_data_quality),
                "error": "insufficient_review_samples",
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

        ordered_positions: list[str] = []
        for item in samples:
            position_id = str(item.get("position_id") or "")
            if position_id and position_id not in ordered_positions:
                ordered_positions.append(position_id)
        holdout_group_count = max(1, int(round(len(ordered_positions) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_positions = set(ordered_positions[-holdout_group_count:])
        train_samples = [item for item in samples if str(item.get("position_id") or "") not in holdout_positions]
        holdout_samples = [item for item in samples if str(item.get("position_id") or "") in holdout_positions]
        if not train_samples or not holdout_samples:
            return {"ok": False, "model_type": MODEL_TYPE, "error": "grouped_time_split_empty"}

        x_train = pd.DataFrame([item["features"] for item in train_samples], columns=FEATURE_NAMES)
        y_train = [int(item["label"]) for item in train_samples]
        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]
        train_position_counts: dict[str, int] = {}
        for item in train_samples:
            key = str(item.get("position_id") or "")
            train_position_counts[key] = train_position_counts.get(key, 0) + 1
        train_weights = [
            1.0 / max(1, train_position_counts.get(str(item.get("position_id") or ""), 1))
            for item in train_samples
        ]

        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=120,
            learning_rate=0.05,
            num_leaves=15,
            min_child_samples=max(1, min(20, len(train_samples) // 4)),
            subsample=0.9,
            colsample_bytree=0.9,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
        model.fit(x_train, y_train, sample_weight=train_weights)
        train_prob = model.predict_proba(x_train)[:, 1]
        holdout_prob = model.predict_proba(x_holdout)[:, 1]

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

        feature_importance = [
            {"feature": name, "importance": int(value)}
            for name, value in sorted(
                zip(FEATURE_NAMES, model.feature_importances_),
                key=lambda item: (-int(item[1]), item[0]),
            )
        ]
        metrics = {
            "train": _metrics(y_train, train_prob),
            "holdout": _metrics(y_holdout, holdout_prob),
            "sample_count": len(samples),
            "feature_count": len(FEATURE_NAMES),
            "split": "time_ordered_grouped_purged",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "train_position_count": len({item["position_id"] for item in train_samples}),
            "holdout_position_count": len({item["position_id"] for item in holdout_samples}),
            "holdout_ratio": float(holdout_ratio),
            "train_count": len(train_samples),
            "holdout_count": len(holdout_samples),
            "label_distribution": {"negative": labels.count(0), "positive": labels.count(1)},
            "safe_for_live_trading": False,
            "data_quality": dict(self.last_data_quality),
            "label_contract": {
                "label": "hold_value_preserved_at_fixed_horizon",
                "horizon_minutes": int(self.last_data_quality.get("horizon_minutes") or 30),
                "pnl_tolerance": float(self.last_data_quality.get("pnl_tolerance") or 0.25),
                "position_balanced_training_weight": True,
            },
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, model_path)
        artifact = {
            "schema_version": "position_quality_lightgbm_artifact.v2",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "label": "hold_value_preserved_at_fixed_horizon",
            "sample_window": {"limit": int(limit), "sample_count": len(samples)},
            "training_lineage": dict(self.last_data_quality),
            "metrics": metrics,
            "explainability": {
                "feature_importance": feature_importance,
                "summary": "LightGBM shadow-only position quality model. Scores are advisory and logged. Holdout metrics include majority baseline and class recall to expose imbalance.",
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

    def score_position_context(
        self,
        position_context: dict[str, Any],
        *,
        artifact_path: str | Path | None = None,
    ) -> dict[str, Any]:
        """Score a live position as an advisory, risk-reducing-only input.

        The model remains unable to close or amend positions.  Callers may
        only use high exit risk to tighten an existing rule-based verdict.
        """
        dep_error = _dependency_error()
        if dep_error:
            return {"ok": False, "error": "dependency_missing", "detail": dep_error}
        import joblib
        import pandas as pd

        path = Path(str(artifact_path or self.latest_artifact_path()))
        if not path.exists():
            return {"ok": False, "error": "artifact_missing"}
        cache_key = (str(path), path.stat().st_mtime_ns)
        if self._live_bundle_cache and self._live_bundle_cache[:2] == cache_key:
            artifact = self._live_bundle_cache[2]
            bundle = self._live_bundle_cache[3]
        else:
            artifact = json.loads(path.read_text(encoding="utf-8"))
            bundle = None
        if str(artifact.get("feature_schema_version") or "") != FEATURE_SCHEMA_VERSION:
            return {"ok": False, "error": "artifact_feature_schema_not_pit_v2"}
        permission = validate_model_artifact(
            artifact, model_type=MODEL_TYPE, db_path=self.db_path,
            context={"mode": "advisory", "operation": "position_quality_live_advisory"},
        )
        if not permission.get("ok"):
            return {"ok": False, "error": "model_permission_violation", "permission": permission}
        model_file = Path(str(artifact.get("model_file") or ""))
        if not model_file.exists():
            return {"ok": False, "error": "model_file_missing"}
        if bundle is None:
            bundle = joblib.load(model_file)
            self._live_bundle_cache = (cache_key[0], cache_key[1], artifact, bundle)
        model = bundle["model"]
        feature_names = list(bundle.get("feature_names") or FEATURE_NAMES)
        risk = dict(position_context.get("risk") or {})
        temporal = dict(position_context.get("temporal_context") or {})
        thesis = str(risk.get("thesis_status") or "").lower()
        regime_shift = str(risk.get("regime_shift") or "").lower()
        features = {
            "mfe": _safe_float(risk.get("mfe")),
            "mae": _safe_float(risk.get("mae")),
            "giveback_ratio": _safe_float(risk.get("giveback_ratio")),
            "profit_capture_ratio": _safe_float(risk.get("profit_capture_ratio")),
            "time_in_profit": _safe_float(risk.get("time_in_profit") or risk.get("time_in_profit_seconds")),
            "holding_efficiency": _safe_float(risk.get("holding_efficiency")),
            "time_decay_score": _safe_float(risk.get("time_decay_score")),
            "holding_seconds": _safe_float(temporal.get("holding_seconds")),
            "thesis_broken": 1.0 if thesis == "broken" else 0.0,
            "thesis_weakening": 1.0 if thesis == "weakening" else 0.0,
            "regime_shift_confirmed": 1.0 if regime_shift == "confirmed" else 0.0,
        }
        frame = pd.DataFrame([[features.get(name, 0.0) for name in feature_names]], columns=feature_names)
        hold_score = float(model.predict_proba(frame)[:, 1][0])
        exit_risk = max(0.0, min(1.0, 1.0 - hold_score))
        return {
            "ok": True,
            "schema_version": "position_quality_live_advisory.v1",
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "hold_score": round(hold_score, 8),
            "exit_risk_score": round(exit_risk, 8),
            "risk_bucket": "high_exit_risk" if exit_risk >= 0.65 else "medium_exit_risk" if exit_risk >= 0.4 else "low_exit_risk",
            "features": features,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "advisory_only": True,
            "risk_reducing_only": True,
        }

    def score_samples(
        self,
        *,
        artifact_path: str | Path | None = None,
        limit: int = 100,
        mode: str = "shadow",
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
            context={"mode": mode, "operation": "position_quality_score_samples"},
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
        x = pd.DataFrame([item["features"] for item in samples], columns=feature_names)
        probs = model.predict_proba(x)[:, 1]
        items = []
        for sample, prob in zip(samples, probs):
            items.append(self._persist_inference(artifact, sample, float(prob), mode=mode))
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
        hold_score: float,
        *,
        mode: str,
    ) -> dict[str, Any]:
        now = time.time()
        exit_risk = max(0.0, min(1.0, 1.0 - float(hold_score)))
        prediction = 1 if hold_score >= 0.5 else 0
        risk_bucket = "high_exit_risk" if exit_risk >= 0.65 else "medium_exit_risk" if exit_risk >= 0.4 else "low_exit_risk"
        result = {
            "schema_version": "position_quality_shadow_result.v1",
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "hold_score": round(float(hold_score), 8),
            "exit_risk_score": round(exit_risk, 8),
            "prediction": prediction,
            "prediction_label": "acceptable_position_quality" if prediction else "weak_position_quality",
            "risk_bucket": risk_bucket,
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
            requested_writes=["position_quality_shadow_audit"],
            status=mode,
            impact_level="shadow",
        )
        payload = {
            "sample_id": sample["sample_id"],
            "review_id": sample["review_id"],
            "trade_id": sample["trade_id"],
            "position_id": sample["position_id"],
            "features": sample["features"],
            "label": sample["label"],
            "pnl": sample["pnl"],
            "outcome_label": sample["outcome_label"],
            "source_agent": "lightgbm_shadow_models",
            "authority_verdict": result["authority_verdict"],
        }
        inference_id = f"{MODEL_TYPE}:{sample['sample_id']}:{int(now * 1000)}"
        conn = self._conn()
        try:
            self._execute(conn,
                """
                INSERT INTO position_quality_shadow_audit
                (inference_id, model_type, model_version, artifact_path, review_id,
                 trade_id, position_id, mode, hold_score, exit_risk_score, prediction,
                 payload_json, result_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    inference_id,
                    MODEL_TYPE,
                    str(artifact.get("model_version") or MODEL_VERSION),
                    str(artifact.get("artifact_path") or ""),
                    sample["review_id"],
                    sample["trade_id"],
                    sample["position_id"],
                    mode,
                    float(hold_score),
                    float(exit_risk),
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
            "position_id": sample["position_id"],
            "hold_score": result["hold_score"],
            "exit_risk_score": result["exit_risk_score"],
            "prediction": prediction,
            "risk_bucket": risk_bucket,
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
            rows = self._execute(conn,
                f"""
                SELECT *
                FROM position_quality_shadow_audit
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
                        "mode": str(row["mode"] or ""),
                        "hold_score": _safe_float(row["hold_score"]),
                        "exit_risk_score": _safe_float(row["exit_risk_score"]),
                        "prediction": int(row["prediction"] or 0),
                        "payload": _loads(row["payload_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()
