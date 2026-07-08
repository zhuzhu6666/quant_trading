from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import DATA_DIR, STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context


MODEL_TYPE = "factor_governance_lightgbm"
MODEL_VERSION = "1.0"
FEATURE_NAMES = [
    "entry_contribution",
    "hold_contribution",
    "exit_contribution",
    "net_contribution",
    "confidence",
    "entry_quality",
    "hold_quality",
    "exit_quality",
    "regime_fit_score",
    "execution_quality",
    "pnl",
    "mae",
    "mfe",
    "is_loss",
    "is_bad_loss",
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


def _sample_from_row(row: Any, *, label: int | None = None, label_source: str = "current_factor_outcome") -> dict[str, Any]:
    item = dict(row)
    outcome = str(item.get("outcome_label") or "").lower()
    pnl = _safe_float(item.get("pnl"))
    net = _safe_float(item.get("net_contribution"))
    confidence = _safe_float(item.get("confidence"))
    target_label = _current_row_label(item) if label is None else int(label)
    features = {
        "entry_contribution": _safe_float(item.get("entry_contribution")),
        "hold_contribution": _safe_float(item.get("hold_contribution")),
        "exit_contribution": _safe_float(item.get("exit_contribution")),
        "net_contribution": net,
        "confidence": confidence,
        "entry_quality": _safe_float(item.get("entry_quality")),
        "hold_quality": _safe_float(item.get("hold_quality")),
        "exit_quality": _safe_float(item.get("exit_quality")),
        "regime_fit_score": _safe_float(item.get("regime_fit_score")),
        "execution_quality": _safe_float(item.get("execution_quality")),
        "pnl": pnl,
        "mae": _safe_float(item.get("mae")),
        "mfe": _safe_float(item.get("mfe")),
        "is_loss": 1.0 if pnl < 0 else 0.0,
        "is_bad_loss": 1.0 if outcome == "bad_loss" else 0.0,
    }
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
        self._ensure_tables()

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        if params is None:
            return conn.execute(self._sql(sql))
        return conn.execute(self._sql(sql), tuple(params))

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
                           r.pnl, r.mae, r.mfe, r.outcome_label, r.review_json, r.created_at
                    FROM factor_contribution_review f
                    JOIN trade_outcome_review r ON r.review_id = f.review_id
                    ORDER BY r.created_at DESC, f.id DESC
                    LIMIT ?
                ) recent_factors
                ORDER BY created_at ASC, id ASC
                """,
                (int(limit),),
            ).fetchall()
            row_items = [dict(row) for row in rows]
            row_items = [item for item in row_items if not _row_system_contaminated(item)]
            by_factor: dict[str, list[dict[str, Any]]] = {}
            for item in row_items:
                by_factor.setdefault(str(item.get("factor") or ""), []).append(item)
            next_label_by_key: dict[tuple[str, str], int] = {}
            for factor_rows in by_factor.values():
                ordered = sorted(factor_rows, key=lambda item: (_safe_float(item.get("created_at")), int(item.get("id") or 0)))
                for idx, item in enumerate(ordered[:-1]):
                    future = ordered[idx + 1]
                    next_label_by_key[(str(item.get("review_id") or ""), str(item.get("factor") or ""))] = _current_row_label(future)
            samples = []
            for item in row_items:
                key = (str(item.get("review_id") or ""), str(item.get("factor") or ""))
                if key not in next_label_by_key:
                    continue
                samples.append(
                    _sample_from_row(
                        item,
                        label=next_label_by_key[key],
                        label_source="next_same_factor_outcome",
                    )
                )
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
        if len(samples) < int(min_samples):
            return {
                "ok": False,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "sample_count": len(samples),
                "error": "insufficient_factor_contribution_samples",
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

        holdout_count = max(1, int(round(len(samples) * max(0.0, min(float(holdout_ratio), 0.8)))))
        holdout_count = min(holdout_count, len(samples) - 1)
        train_samples = samples[:-holdout_count]
        holdout_samples = samples[-holdout_count:]

        x_train = pd.DataFrame([item["features"] for item in train_samples], columns=FEATURE_NAMES)
        y_train = [int(item["label"]) for item in train_samples]
        x_holdout = pd.DataFrame([item["features"] for item in holdout_samples], columns=FEATURE_NAMES)
        y_holdout = [int(item["label"]) for item in holdout_samples]

        model = lgb.LGBMClassifier(
            objective="binary",
            n_estimators=140,
            learning_rate=0.04,
            num_leaves=15,
            min_child_samples=max(1, min(20, len(train_samples) // 4)),
            subsample=0.9,
            colsample_bytree=0.9,
            class_weight="balanced",
            random_state=42,
            n_jobs=1,
            verbosity=-1,
        )
        model.fit(x_train, y_train)
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
            "split": "time_ordered",
            "holdout_ratio": float(holdout_ratio),
            "train_count": len(train_samples),
            "holdout_count": len(holdout_samples),
            "label_distribution": {"negative": labels.count(0), "positive": labels.count(1)},
            "safe_for_live_trading": False,
        }
        now = time.time()
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        artifact_base = f"{MODEL_TYPE}_{int(now)}"
        model_path = self.artifact_dir / f"{artifact_base}.joblib"
        metadata_path = self.artifact_dir / f"{artifact_base}.json"
        joblib.dump({"model": model, "feature_names": FEATURE_NAMES}, model_path)
        artifact = {
            "schema_version": "factor_governance_lightgbm_artifact.v1",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "created_at": now,
            "artifact_path": str(metadata_path),
            "model_file": str(model_path),
            "model_file_sha256": _sha256(model_path),
            "feature_names": FEATURE_NAMES,
            "label": "next_same_factor_positive_contribution",
            "sample_window": {"limit": int(limit), "sample_count": len(samples)},
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
        limit: int = 200,
        mode: str = "shadow",
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
    ) -> dict[str, Any]:
        source_items = items if items is not None else self.list_audits(limit=500)["items"]
        grouped: dict[str, list[dict[str, Any]]] = {}
        for item in source_items:
            factor = str(item.get("factor") or "")
            if not factor:
                continue
            grouped.setdefault(factor, []).append(item)
        suggestions = []
        for factor, factor_items in sorted(grouped.items()):
            weak_items = [
                item for item in factor_items
                if _safe_float(item.get("weakness_score")) >= float(min_weakness_score)
            ]
            if not weak_items:
                continue
            avg_weakness = sum(_safe_float(item.get("weakness_score")) for item in weak_items) / max(len(weak_items), 1)
            confidence = min(0.92, max(0.55, avg_weakness * min(1.0, len(weak_items) / 5.0)))
            suggestion_id = "fgm_" + hashlib.sha1(
                f"{factor}:{len(weak_items)}:{round(avg_weakness, 4)}".encode("utf-8")
            ).hexdigest()[:16]
            suggestions.append(
                {
                    "suggestion_id": suggestion_id,
                    "scope_type": "factor",
                    "scope_key": factor,
                    "action": "review_factor_weight_or_template",
                    "confidence": round(confidence, 4),
                    "reason": "LightGBM shadow model detected repeated weak factor contribution samples",
                    "evidence": attach_policy_suggestion_agent_context(
                        {
                        "schema_version": "factor_governance_advisory.v1",
                        "model_type": MODEL_TYPE,
                        "sample_count": len(factor_items),
                        "weak_sample_count": len(weak_items),
                        "avg_weakness_score": round(avg_weakness, 6),
                        "min_weakness_score": float(min_weakness_score),
                        "latest_inference_ids": [str(item.get("inference_id") or "") for item in weak_items[:5]],
                        "advisory_only": True,
                        "approval_path": "governor_review_then_offline_replay",
                        },
                        source_agent="lightgbm_shadow_models",
                        scope_type="factor",
                        action="review_factor_weight_or_template",
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
