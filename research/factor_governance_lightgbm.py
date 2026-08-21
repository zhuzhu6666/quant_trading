from __future__ import annotations

import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any

from backend.core.db import (
    DATA_DIR,
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.agent_authority import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact
from backend.services.policy_suggestion_context import attach_policy_suggestion_agent_context
from backend.services.review_contract import (
    review_execution_evidence_is_trainable,
    review_has_system_contamination,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    decision_row,
    iter_decision_factor_snapshots,
    iter_review_rows_desc,
)


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
    # v6.0 (pit.v4): 因子×regime 真条件绩效 —— 数据源读取 canonical
    # decision payload 的 factor_snapshots 与 regime_id,按同 regime
    # 历史聚合 positive_rate/pnl_avg/sample_count,
    # 替代交易级 regime_fit_score 的"全因子共享"局限。
    "same_regime_positive_rate",
    "same_regime_pnl_avg",
    "same_regime_sample_count",
]


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _canonical_review_payload(row: dict[str, Any]) -> dict[str, Any]:
    payload = _loads(row.get("review_json"), {})
    return payload if isinstance(payload, dict) else {}


def _canonical_factor_contributions(row: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Return factor outcome details embedded in a canonical review payload."""

    review = _canonical_review_payload(row)
    raw = review.get("factor_contributions")
    if not isinstance(raw, (dict, list)):
        raw = review.get("contributions")
    if isinstance(raw, dict):
        items = [{"factor": factor, "value": value} for factor, value in raw.items()]
    elif isinstance(raw, list):
        items = [item for item in raw if isinstance(item, dict)]
    else:
        items = []
    result: dict[str, dict[str, Any]] = {}
    for item in items:
        factor = str(item.get("factor") or item.get("name") or "")
        if not factor:
            continue
        value = item.get("value")
        detail = value if isinstance(value, dict) else item
        notes = detail.get("notes", detail.get("note", {}))
        note_payload = notes if isinstance(notes, dict) else _loads(notes, {})
        result[factor] = {
            "entry_contribution": _safe_float(detail.get("entry_contribution")),
            "hold_contribution": _safe_float(detail.get("hold_contribution")),
            "exit_contribution": _safe_float(detail.get("exit_contribution")),
            "net_contribution": _safe_float(
                detail.get("net_contribution", detail.get("net", detail.get("value", value)))
            ),
            "confidence": _safe_float(detail.get("confidence")),
            "notes": json.dumps(note_payload, ensure_ascii=False, default=str)
            if isinstance(note_payload, (dict, list))
            else str(notes or ""),
        }
    return result


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


def _stable_sha256(value: Any) -> str:
    """Hash a JSON-compatible value without depending on dict ordering."""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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
    failure_tags = _loads(item.get("failure_tags_json"), [])
    review_for_checks = {
        **review,
        "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
    }
    system_issue = review.get("system_issue_context") if isinstance(review, dict) else {}
    return bool(
        (isinstance(notes, dict) and notes.get("system_contaminated"))
        or (
            isinstance(system_issue, dict)
            and system_issue.get("contaminates_learning")
        )
        or review_has_system_contamination(review_for_checks)
    )


def _row_execution_evidence_complete(item: dict[str, Any]) -> bool:
    review = _loads(item.get("review_json"), {})
    failure_tags = _loads(item.get("failure_tags_json"), [])
    return review_execution_evidence_is_trainable(
        {
            **review,
            "failure_tags": failure_tags if isinstance(failure_tags, list) else [],
        }
    )


def _rolling_factor_features(history: list[dict[str, Any]], *, window: int = 5) -> dict[str, float]:
    items = history[-max(2, int(window)):]
    current = items[-1]
    n = max(len(items), 1)
    regime_fits = [_safe_float(item.get("regime_fit_score")) for item in items]
    # v6.0 (pit.v4): 因子×regime 真条件绩效 —— 按"同 regime"聚合历史。
    # regime_id 来自 canonical decision payload(因子决策时点的真实市场状态),
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

    @staticmethod
    def _canonical_factor_rows(
        conn: Any,
        *,
        factor: str = "",
        limit: int = 0,
    ) -> list[dict[str, Any]]:
        """Build factor training rows from canonical review/decision payloads."""

        if not canonical_ready(conn):
            return []
        rows: list[dict[str, Any]] = []
        for review in iter_review_rows_desc(conn, limit=0):
            review_id = str(review.get("review_id") or "")
            decision_id = str(review.get("entry_decision_id") or "")
            if not review_id or not decision_id:
                continue
            decision = decision_row(conn, decision_id)
            if not decision:
                continue
            contributions = _canonical_factor_contributions(review)
            if not contributions:
                continue
            review_payload = _canonical_review_payload(review)
            lineage = review_payload.get("factor_training_lineage") or {}
            snapshots = iter_decision_factor_snapshots(conn, decision_id)
            for snapshot in snapshots:
                factor_name = str(snapshot.get("factor") or "")
                if not factor_name or (factor and factor_name != str(factor)):
                    continue
                if factor_name not in contributions:
                    continue
                contribution = dict(contributions[factor_name])
                entry_contribution = _safe_float(contribution.get("entry_contribution"))
                if entry_contribution == 0.0:
                    entry_contribution = _safe_float(snapshot.get("contribution_score"))
                note_payload = _loads(contribution.get("notes"), {})
                if not isinstance(note_payload, dict):
                    note_payload = {}
                note_payload.setdefault("factor_generation", str(lineage.get("generation") or ""))
                rows.append(
                    {
                        "id": len(rows),
                        "review_id": review_id,
                        "trade_id": str(review.get("trade_id") or ""),
                        "factor": factor_name,
                        "entry_contribution": entry_contribution,
                        "hold_contribution": _safe_float(contribution.get("hold_contribution")),
                        "exit_contribution": _safe_float(contribution.get("exit_contribution")),
                        "net_contribution": _safe_float(contribution.get("net_contribution")),
                        "confidence": _safe_float(contribution.get("confidence")),
                        "notes": json.dumps(note_payload, ensure_ascii=False, default=str),
                        "position_id": str(review.get("position_id") or ""),
                        "entry_quality": review.get("entry_quality"),
                        "hold_quality": review.get("hold_quality"),
                        "exit_quality": review.get("exit_quality"),
                        "regime_fit_score": review.get("regime_fit_score"),
                        "execution_quality": review.get("execution_quality"),
                        "pnl": review.get("pnl"),
                        "mae": review.get("mae"),
                        "mfe": review.get("mfe"),
                        "outcome_label": str(review.get("outcome_label") or ""),
                        "failure_tags_json": review.get("failure_tags_json") or "[]",
                        "review_json": review_payload,
                        "created_at": review.get("created_at"),
                        "regime_id": str(decision.get("regime_id") or ""),
                        "regime_confidence": decision.get("regime_confidence"),
                        "decision_ts": decision.get("decision_ts"),
                        "factor_generation": str(lineage.get("generation") or ""),
                    }
                )
        rows.sort(
            key=lambda item: (
                -_safe_float(item.get("decision_ts") or item.get("created_at")),
                str(item.get("review_id") or ""),
                str(item.get("factor") or ""),
            )
        )
        return rows[: int(limit)] if limit and int(limit) > 0 else rows

    def load_samples(self, *, limit: int = 2000) -> list[dict[str, Any]]:
        conn = self._conn()
        try:
            # Canonical reviews carry the outcome contribution payload and
            # canonical decisions carry the factor snapshots.  Keep the
            # chronological training order after applying the recent-row cap.
            rows = list(reversed(self._canonical_factor_rows(conn, limit=int(limit))))
            row_items = [dict(row) for row in rows]
            system_clean_count = sum(1 for item in row_items if not _row_system_contaminated(item))
            row_items = [
                item
                for item in row_items
                if not _row_system_contaminated(item)
                and _row_execution_evidence_complete(item)
            ]
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
            factor_sample_counts: dict[str, int] = {}
            for sample in samples:
                factor = str(sample.get("factor") or "")
                if factor:
                    factor_sample_counts[factor] = factor_sample_counts.get(factor, 0) + 1
            factors_below_20 = sorted(
                factor for factor, count in factor_sample_counts.items() if count < 20
            )
            lineage_fingerprint = [
                {
                    "id": int(item.get("id") or 0),
                    "review_id": str(item.get("review_id") or ""),
                    "trade_id": str(item.get("trade_id") or ""),
                    "factor": str(item.get("factor") or ""),
                    "decision_ts": _safe_float(item.get("decision_ts") or item.get("created_at")),
                    "factor_generation": str(item.get("factor_generation") or ""),
                }
                for item in sorted(
                    lineage_items,
                    key=lambda value: (
                        _safe_float(value.get("decision_ts") or value.get("created_at")),
                        int(value.get("id") or 0),
                    ),
                )
            ]
            self.last_data_quality = {
                "schema_version": "model_training_data_quality.v1",
                "candidate_row_count": len(rows),
                "uncontaminated_row_count": len(row_items),
                "system_clean_row_count": system_clean_count,
                "excluded_system_contaminated_count": len(rows) - system_clean_count,
                "excluded_execution_incomplete_count": system_clean_count - len(row_items),
                "lineage_row_count": len(lineage_items),
                "selected_count": len(samples),
                "selected_distinct_trade_count": len(selected_trades),
                "factor_count": len(by_factor),
                "factor_generation": latest_generation,
                "generation_contract": "factor_universe_budget.v1",
                "excluded_other_generation_count": len(row_items) - len(lineage_items),
                "factors_below_10_samples": sum(count < 10 for count in factor_sample_counts.values()),
                "factors_below_20_samples": sum(count < 20 for count in factor_sample_counts.values()),
                "factors_below_20_sample_ids": factors_below_20,
                "factor_sample_counts": dict(sorted(factor_sample_counts.items())),
                "sample_watermark": max(
                    (_safe_float(item.get("created_at")) for item in samples),
                    default=0.0,
                ),
                "lineage_hash": _stable_sha256({
                    "factor_generation": latest_generation,
                    "rows": lineage_fingerprint,
                }),
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
                "status": "failed",
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "error": "dependency_missing",
                "detail": dep_error,
                "reason_codes": ["dependency_missing"],
                "required": ["lightgbm", "scikit-learn", "joblib", "pandas"],
            }

        import joblib
        import lightgbm as lgb
        import pandas as pd
        from sklearn.metrics import accuracy_score, balanced_accuracy_score, recall_score, roc_auc_score

        samples = self.load_samples(limit=limit)
        if not samples:
            return {
                "ok": False,
                "status": "skipped",
                "skipped": True,
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": 0,
                "distinct_trade_count": 0,
                "replay_distinct_trade_count": 0,
                "data_quality": dict(self.last_data_quality),
                "reason": "no_new_matured_samples",
                "reason_codes": ["no_new_matured_samples"],
            }
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
        if len(distinct_trade_ids) < max(2, int(min_samples)):
            return {
                "ok": False,
                "status": "blocked",
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "feature_schema_version": FEATURE_SCHEMA_VERSION,
                "sample_count": len(samples),
                "distinct_trade_count": len(distinct_trade_ids),
                "replay_distinct_trade_count": len(replay_trade_ids),
                "data_quality": dict(self.last_data_quality),
                "error": "insufficient_distinct_factor_trades",
                "reason_codes": ["insufficient_distinct_factor_trades"],
            }
        labels = [int(item["label"]) for item in samples]
        if len(set(labels)) < 2:
            return {
                "ok": False,
                "status": "blocked",
                "model_type": MODEL_TYPE,
                "model_version": MODEL_VERSION,
                "sample_count": len(samples),
                "positive_count": sum(labels),
                "error": "single_class_training_data",
                "reason_codes": ["single_class_training_data"],
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
            return {
                "ok": False,
                "status": "blocked",
                "model_type": MODEL_TYPE,
                "error": "grouped_time_split_empty",
                "reason_codes": ["grouped_time_split_empty"],
            }

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
        label_contract = {
            "label": "next_same_factor_outcome_from_rolling_history",
            "trade_balanced_training_weight": True,
            "recency_half_life_days": 14.0,
            "factor_generation": self.last_data_quality.get("factor_generation"),
        }
        training_data_quality = dict(self.last_data_quality)
        training_data_quality.update(
            {
                "real_distinct_trade_count": len(distinct_trade_ids),
                "real_holdout_trade_count": len(holdout_trade_ids),
                "replay_distinct_trade_count": len(replay_trade_ids),
                "sample_maturity_watermark": self.last_data_quality.get(
                    "sample_watermark", 0.0
                ),
                "label_contract_hash": _stable_sha256(label_contract),
            }
        )
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
            "data_quality": training_data_quality,
            "label_contract": label_contract,
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
            "training_lineage": dict(training_data_quality),
            "metrics": metrics,
            "lineage": {
                "factor_generation": str(
                    self.last_data_quality.get("factor_generation") or ""
                ),
                "lineage_hash": str(self.last_data_quality.get("lineage_hash") or ""),
                "label_contract_hash": str(
                    training_data_quality.get("label_contract_hash") or ""
                ),
                "sample_maturity_watermark": float(
                    training_data_quality.get("sample_maturity_watermark") or 0.0
                ),
            },
            "coverage": {
                "factor_sample_counts": dict(
                    training_data_quality.get("factor_sample_counts") or {}
                ),
                "factors_below_20_samples": list(
                    training_data_quality.get("factors_below_20_sample_ids") or []
                ),
                "factor_count": len(
                    training_data_quality.get("factor_sample_counts") or {}
                ),
            },
            "walk_forward": {
                "schema_version": "walk_forward.v1",
                "status": "observed",
                "window_count": 1,
                "windows": [
                    {
                        "window_id": "time_ordered_holdout",
                        "train_trade_count": len(
                            set(ordered_trades) - holdout_trade_ids
                        ),
                        "holdout_trade_count": len(holdout_trade_ids),
                        "metrics": dict(augmented_holdout if use_augmented else baseline_holdout),
                    }
                ],
            },
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
        try:
            from backend.services.model_influence_governance import ModelInfluenceGovernanceService

            quality_gate = ModelInfluenceGovernanceService(self.db_path).evaluate_artifact(
                metadata_path
            )
        except Exception as exc:
            quality_gate = {
                "schema_version": "model_promotion_gate.v1",
                "passed": False,
                "reason": "promotion_gate_evaluation_error",
                "error": f"{type(exc).__name__}: {exc}",
                "checks": [],
                "failed_checks": ["promotion_gate_evaluation_error"],
            }
        artifact["quality_gate"] = {
            "schema_version": str(quality_gate.get("schema_version") or "model_promotion_gate.v1"),
            "passed": bool(quality_gate.get("passed")),
            "reason": str(quality_gate.get("reason") or "promotion_gate_failed"),
            "reason_codes": list(
                quality_gate.get("reason_codes")
                or quality_gate.get("failed_checks")
                or []
            ),
            "failed_checks": list(quality_gate.get("failed_checks") or []),
            "checks": list(quality_gate.get("checks") or []),
            "generation": str(self.last_data_quality.get("factor_generation") or ""),
            "lineage_hash": str(self.last_data_quality.get("lineage_hash") or ""),
        }
        artifact["metrics"]["data_quality"].update(
            {
                "quality_gate": {
                    "passed": bool(quality_gate.get("passed")),
                    "reason": str(
                        quality_gate.get("reason") or "promotion_gate_failed"
                    ),
                    "reason_codes": list(
                        quality_gate.get("reason_codes")
                        or quality_gate.get("failed_checks")
                        or []
                    ),
                },
                "real_distinct_trade_count": int(
                    quality_gate.get("real_distinct_trade_count")
                    or len(distinct_trade_ids)
                ),
                "real_holdout_trade_count": int(
                    quality_gate.get("real_holdout_trade_count")
                    or len(holdout_trade_ids)
                ),
                "replay_distinct_trade_count": int(
                    quality_gate.get("replay_distinct_trade_count")
                    or len(replay_trade_ids)
                ),
            }
        )
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
            "status": "trained",
            "model_type": MODEL_TYPE,
            "model_version": MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "artifact_path": str(metadata_path),
            "artifact_sha256": artifact["artifact_sha256"],
            "model_file": str(model_path),
            "metrics": metrics,
            "quality_gate": artifact["quality_gate"],
            "explainability": artifact["explainability"],
            "capabilities": artifact["capabilities"],
            "registry_version": registry_version,
        }

    def latest_artifact_path(self) -> str:
        paths = sorted(self.artifact_dir.glob(f"{MODEL_TYPE}_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return str(paths[0]) if paths else ""

    def _promotion_gate(self, artifact_path: str | Path) -> dict[str, Any]:
        """Read the canonical model gate without creating a second gate owner."""

        from backend.services.model_influence_governance import ModelInfluenceGovernanceService

        return ModelInfluenceGovernanceService(self.db_path).evaluate_artifact(artifact_path)

    def _existing_shadow_sample_ids(self, *, artifact_path: str) -> set[str]:
        conn = self._conn()
        try:
            rows = self._execute(
                conn,
                f"""
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
        artifact["artifact_sha256"] = _sha256(path)
        promotion_gate = self._promotion_gate(path)
        mutation_eligible = bool(promotion_gate.get("passed"))
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
            items.append(
                self._persist_inference(
                    artifact,
                    sample,
                    float(prob),
                    mode=mode,
                    promotion_gate=promotion_gate,
                )
            )
        suggestions = self.build_advisories(
            items=items,
            materialize=materialize and mutation_eligible,
            min_weakness_score=min_weakness_score,
            min_factor_sample_count=20,
        )
        return {
            "ok": True,
            "model_type": MODEL_TYPE,
            "model_version": str(artifact.get("model_version") or MODEL_VERSION),
            "artifact_path": str(path),
            "count": len(items),
            "items": items,
            "suggestions": suggestions,
            "promotion_gate": promotion_gate,
            "mutation_eligible": mutation_eligible,
            "materialization_blocked_reason": (
                "blocked_by_model_quality_gate"
                if materialize and not mutation_eligible
                else ""
            ),
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
        artifact["artifact_sha256"] = _sha256(path)
        promotion_gate = self._promotion_gate(path)
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
                self._persist_inference(
                    artifact,
                    sample,
                    float(prob),
                    mode=mode,
                    promotion_gate=promotion_gate,
                )
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
            "promotion_gate": promotion_gate,
            "mutation_eligible": bool(promotion_gate.get("passed")),
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
            rows = self._canonical_factor_rows(conn, factor=factor, limit=int(limit))
            row_items = [dict(row) for row in rows]
            row_items = [
                item
                for item in row_items
                if not _row_system_contaminated(item)
                and _row_execution_evidence_complete(item)
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
        promotion_gate: dict[str, Any] | None = None,
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
        gate = dict(promotion_gate or artifact.get("quality_gate") or {})
        result["promotion_gate"] = {
            "passed": bool(gate.get("passed")),
            "reason": str(gate.get("reason") or "promotion_gate_unknown"),
            "failed_checks": list(gate.get("failed_checks") or []),
        }
        result["mutation_eligible"] = bool(gate.get("passed"))
        result["artifact_sha256"] = str(artifact.get("artifact_sha256") or "")
        result["factor_generation"] = str(
            (artifact.get("training_lineage") or {}).get("factor_generation")
            or (artifact.get("quality_gate") or {}).get("generation")
            or ""
        )
        result["lineage_hash"] = str(
            (artifact.get("training_lineage") or {}).get("lineage_hash")
            or (artifact.get("quality_gate") or {}).get("lineage_hash")
            or ""
        )
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
            "artifact_sha256": result["artifact_sha256"],
            "promotion_gate": result["promotion_gate"],
            "mutation_eligible": result["mutation_eligible"],
            "factor_generation": result["factor_generation"],
            "lineage_hash": result["lineage_hash"],
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
        min_factor_sample_count: int = 20,
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
        low_coverage_factors: dict[str, int] = {}
        for factor, factor_items in sorted(grouped.items()):
            if materialize and len(factor_items) < max(1, int(min_factor_sample_count)):
                low_coverage_factors[factor] = len(factor_items)
                continue
            weak_items = [
                item for item in factor_items
                if _safe_float(item.get("weakness_score")) >= float(min_weakness_score)
            ]
            if len(weak_items) < max(1, int(min_weak_sample_count)):
                continue
            avg_weakness = sum(_safe_float(item.get("weakness_score")) for item in weak_items) / max(len(weak_items), 1)
            confidence = min(0.92, max(0.55, avg_weakness * min(1.0, len(weak_items) / 5.0)))
            review_ids = sorted(
                {
                    str(item.get("review_id") or "")
                    for item in factor_items
                    if str(item.get("review_id") or "")
                }
            )
            counter_items = [
                item
                for item in factor_items
                if _safe_float(item.get("weakness_score")) < float(min_weakness_score)
            ]
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
                "review_ids": review_ids,
                "review_reference_ids": review_ids,
                "review_id": review_ids[0] if review_ids else "",
                "counter_evidence_refs": {
                    "schema_version": "factor_governance_counter_evidence_refs.v1",
                    "source": "factor_governance_shadow_audit",
                    "review_ids": sorted(
                        {
                            str(item.get("review_id") or "")
                            for item in counter_items
                            if str(item.get("review_id") or "")
                        }
                    ),
                    "inference_ids": [
                        str(item.get("inference_id") or "")
                        for item in counter_items
                        if str(item.get("inference_id") or "")
                    ],
                    "observed_count": len(counter_items),
                    "status": "observed" if counter_items else "pending_candidate_review",
                    "required_before_bridge": True,
                },
                "advisory_only": True,
                "approval_path": "governor_review_then_offline_replay",
            }
            audit_result = next(
                (
                    dict(item.get("result") or {})
                    for item in factor_items
                    if isinstance(item.get("result"), dict)
                    and item.get("result")
                ),
                {},
            )
            if audit_result:
                evidence.setdefault(
                    "promotion_gate",
                    dict(audit_result.get("promotion_gate") or {}),
                )
                for name in (
                    "mutation_eligible",
                    "artifact_sha256",
                    "factor_generation",
                    "lineage_hash",
                ):
                    if name in audit_result:
                        evidence.setdefault(name, audit_result.get(name))
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
        materialization_blocked_reason = ""
        materialization: dict[str, Any] = {
            "candidate_count": 0,
            "blocked_count": 0,
            "blocked_reasons": {},
            "items": [],
        }
        if materialize and suggestions:
            model_gate_ready = all(
                bool(((item.get("evidence") or {}).get("promotion_gate") or {}).get("passed"))
                and bool((item.get("evidence") or {}).get("mutation_eligible"))
                for item in suggestions
            )
            if not model_gate_ready:
                materialize = False
                materialization_blocked_reason = "blocked_by_model_quality_gate"
        if materialize and suggestions:
            materialization = self._materialize_suggestions(suggestions)
            if not materialization.get("candidate_count"):
                materialize = False
                materialization_blocked_reason = str(
                    materialization.get("blocked_reason")
                    or "no_governance_candidate_materialized"
                )
        return {
            "schema_version": "factor_governance_advisory_set.v1",
            "model_type": MODEL_TYPE,
            "advisory_only": True,
            "materialized": bool(materialize),
            "materialization_blocked_reason": materialization_blocked_reason,
            "candidate_count": int(materialization.get("candidate_count") or 0),
            "materialization": materialization,
            "low_coverage_factors": low_coverage_factors,
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
        min_factor_sample_count: int = 20,
    ) -> dict[str, Any]:
        """Bridge strong model evidence into the guarded demo governance queue.

        The LightGBM model remains advisory-only.  This method only creates a
        factor-scoped governance candidate with a concrete, whitelisted
        ``downweight`` action.  Candidate review, counter-evidence, the
        existing policy bridge, DecisionPolicy, RiskPolicyService, and weight
        mutation service remain downstream authorities.
        """
        from backend.services.factor_catalog import build_factor_catalog
        from backend.services.model_influence import ModelInfluenceService
        from backend.services.model_influence_governance import ModelInfluenceGovernanceService
        from config.runtime_config import shared as runtime_config

        cfg = runtime_config()
        influence = ModelInfluenceService(self.db_path)
        configured_policy = influence.policy_for(MODEL_TYPE, cfg)
        configured_artifact_path = str(configured_policy.get("artifact_path") or "")
        artifact_path = configured_artifact_path or self.latest_artifact_path()
        promotion_gate = (
            ModelInfluenceGovernanceService(self.db_path).evaluate_artifact(artifact_path)
            if artifact_path
            else {
                "schema_version": "model_promotion_gate.v1",
                "passed": False,
                "reason": "artifact_missing",
                "failed_checks": ["artifact_missing"],
                "checks": [],
            }
        )
        if not promotion_gate.get("passed"):
            return {
                "schema_version": "factor_governance_demo_bridge.v1",
                "enabled": False,
                "materialized": False,
                "count": 0,
                "reason": "blocked_by_model_quality_gate",
                "artifact_path": artifact_path,
                "promotion_gate": promotion_gate,
                "mutation_eligible": False,
            }
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
        low_coverage: dict[str, int] = {}
        for item in audits:
            factor = str(item.get("factor") or "")
            if factor in active:
                grouped.setdefault(factor, []).append(item)
        ranked: list[tuple[float, str]] = []
        for factor, items in grouped.items():
            if len(items) < max(1, int(min_factor_sample_count)):
                low_coverage[factor] = len(items)
                continue
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
        gate_data_quality = dict(
            (promotion_gate.get("metrics") or {}).get("data_quality") or {}
        )
        model_version = str(
            policy.get("model_version")
            or next(
                (
                    str(item.get("model_version") or "")
                    for item in audits
                    if str(item.get("model_version") or "")
                ),
                "",
            )
        )
        factor_generation = str(
            gate_data_quality.get("factor_generation")
            or promotion_gate.get("generation")
            or ""
        )
        lineage_hash = str(
            gate_data_quality.get("lineage_hash")
            or promotion_gate.get("lineage_hash")
            or ""
        )
        label_contract_hash = str(
            gate_data_quality.get("label_contract_hash") or ""
        )
        context = {
            factor: {
                "bridge_schema_version": "factor_governance_demo_bridge.v1",
                "bridge": {
                    "automatic_demo": True,
                    "demo_nursery": str(getattr(cfg, "autonomy_mode", "")) == "demo_nursery",
                    "autonomy_mode": str(getattr(cfg, "autonomy_mode", "") or ""),
                    "actor": "system:autonomous_learning.demo_nursery_model_governance",
                    "service": "FactorGovernanceLightGBMService.materialize_demo_governance_advisories",
                    "manual_only": False,
                },
                "model_advisory": True,
                "model_influence_active": True,
                "mutation_eligible": True,
                "model_version": model_version,
                "model_stage": str(policy.get("stage") or ""),
                "artifact_path": artifact_path,
                "artifact_sha256": str(promotion_gate.get("artifact_sha256") or ""),
                "factor_generation": factor_generation,
                "lineage_hash": lineage_hash,
                "label_contract_hash": label_contract_hash,
                "v16_command_id": "",
                "mutation_id": "",
                "application_id": "",
                "promotion_gate": {
                    "passed": True,
                    "reason": str(promotion_gate.get("reason") or "promotion_gate_passed"),
                    "reason_codes": list(
                        promotion_gate.get("reason_codes")
                        or promotion_gate.get("failed_checks")
                        or []
                    ),
                    "failed_checks": list(promotion_gate.get("failed_checks") or []),
                },
                "training_lineage": gate_data_quality,
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
            materialize=True,
            min_weakness_score=min_weakness_score,
            governed_action="downweight",
            min_weak_sample_count=min_weak_sample_count,
            factor_allowlist=selected,
            evidence_context_by_factor=context,
        )
        suggestions = list(result.get("items") or [])
        if suggestions:
            audit_policy = {
                **policy,
                "artifact_sha256": str(
                    policy.get("artifact_sha256")
                    or promotion_gate.get("artifact_sha256")
                    or ""
                ),
                "model_version": model_version,
            }
            for suggestion in suggestions:
                evidence = dict(suggestion.get("evidence") or {})
                evidence.setdefault("review_id", "")
                evidence.setdefault("candidate_review_id", "")
                evidence.setdefault("v16_command_id", "")
                evidence.setdefault("mutation_id", "")
                evidence.setdefault("application_id", "")
                evidence.setdefault("application_state", "candidate_only")
                suggestion["evidence"] = evidence
            for suggestion in suggestions:
                if not str((suggestion.get("evidence") or {}).get("candidate_id") or ""):
                    continue
                influence.audit(
                    model_type=MODEL_TYPE,
                    policy=audit_policy,
                    subject_id=str(suggestion.get("scope_key") or ""),
                    rule_decision={"governance_required": True, "direct_weight_change": False},
                    model_result=dict(suggestion.get("evidence") or {}),
                    fused_decision={
                        "suggestion_id": suggestion.get("suggestion_id"),
                        "candidate_id": (suggestion.get("evidence") or {}).get("candidate_id", ""),
                        "review_id": (suggestion.get("evidence") or {}).get("candidate_review_id", ""),
                        "v16_command_id": (suggestion.get("evidence") or {}).get("v16_command_id", ""),
                        "mutation_id": (suggestion.get("evidence") or {}).get("mutation_id", ""),
                        "application_id": (suggestion.get("evidence") or {}).get("application_id", ""),
                        "action": "downweight",
                        "status": "candidate_only",
                    },
                    applied=False,
                    reason="model_factor_downweight_candidate_materialized",
                )
        return {
            **result,
            "schema_version": "factor_governance_demo_bridge.v1",
            "enabled": True,
            "materialized": bool(result.get("candidate_count") or 0),
            "eligible_active_factors": len(active),
            "selected_factors": sorted(selected),
            "stale_superseded": stale_superseded,
            "promotion_gate": promotion_gate,
            "mutation_eligible": True,
            "low_coverage_factors": low_coverage,
            "min_factor_sample_count": int(min_factor_sample_count),
            "min_weakness_score": float(min_weakness_score),
            "min_weak_sample_count": int(min_weak_sample_count),
        }

    def _supersede_inactive_demo_suggestions(self, active_factors: set[str]) -> int:
        """Close stale model bridges after their factor leaves the runtime score."""
        from config.runtime_config import DEMO_AUTONOMY_MODES

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
                    and (
                        bridge.get("demo_nursery") is True
                        or str(bridge.get("autonomy_mode") or "").strip().lower()
                        in DEMO_AUTONOMY_MODES
                    )
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

    def _materialize_suggestions(
        self, suggestions: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Materialize model output into the existing candidate lane only.

        ``lightgbm_shadow_models`` is advisory-only and is not registered as a
        policy writer.  The candidate is attributed to the existing
        ``factor_pruning_governance`` authority so that the already deployed
        counter-evidence, candidate-review, and policy-suggestion bridge remain
        the only downstream mutation path.
        """
        from alpha.decision_policy import DecisionPolicy
        from backend.services.brain_governance_candidates import (
            BrainGovernanceCandidateService,
            ensure_brain_governance_candidate_table,
        )
        from risk.policy_service import RiskPolicyService

        ensure_brain_governance_candidate_table(self.db_path)
        candidate_service = BrainGovernanceCandidateService(self.db_path)
        now = time.time()
        candidates: list[dict[str, Any]] = []
        blocked: list[dict[str, Any]] = []

        def block(item: dict[str, Any], reason: str, **extra: Any) -> None:
            blocked.append(
                {
                    "status": "blocked_model_candidate",
                    "suggestion_id": str(item.get("suggestion_id") or ""),
                    "factor": str(item.get("scope_key") or ""),
                    "reason": reason,
                    **extra,
                }
            )

        for item in suggestions:
            evidence = dict(item.get("evidence") or {})
            factor = str(item.get("scope_key") or "")
            if str(item.get("action") or "") != "downweight":
                block(item, "unsupported_model_governance_action")
                continue
            gate = dict(evidence.get("promotion_gate") or {})
            missing: list[str] = []
            if gate.get("passed") is not True:
                missing.append("model_quality_gate")
            if evidence.get("mutation_eligible") is not True:
                missing.append("mutation_eligibility")
            for name in (
                "artifact_sha256",
                "factor_generation",
                "lineage_hash",
                "label_contract_hash",
            ):
                if not str(evidence.get(name) or ""):
                    missing.append(name)
            if str(evidence.get("factor_generation") or "") != "runtime_bounded_v1":
                missing.append("current_factor_generation")
            review_ids = [
                str(value)
                for value in (evidence.get("review_reference_ids") or evidence.get("review_ids") or [])
                if str(value)
            ]
            if not review_ids:
                missing.append("review_reference")
            counter_refs = evidence.get("counter_evidence_refs")
            if not isinstance(counter_refs, dict) or not counter_refs.get("required_before_bridge"):
                missing.append("counter_evidence_contract")
            active_context = dict(evidence.get("active_factor_context") or {})
            if active_context.get("used_in_score") is not True or str(active_context.get("role") or "") != "alpha":
                missing.append("active_alpha_factor")
            if int(evidence.get("sample_count") or 0) < 20:
                missing.append("factor_sample_count")
            if int(evidence.get("weak_sample_count") or 0) < 2:
                missing.append("weak_sample_count")
            if missing:
                block(item, "missing_model_candidate_contract", missing=sorted(set(missing)))
                continue

            current_weight = _safe_float(active_context.get("weight"))
            if not factor or current_weight <= 0.0:
                block(item, "missing_current_runtime_weight")
                continue
            target_weight = max(0.0, min(current_weight * 0.89, current_weight * 0.95))
            risk_result = RiskPolicyService.shared().evaluate(
                "update_weight",
                {
                    "required_mode": "autonomous_governance",
                    "session": {"drawdown_pct": 0.0},
                    "evidence": {"factor_governance_model_candidate": evidence},
                    "suggestion_status": "candidate",
                    "autonomous_apply": False,
                    "factor": factor,
                    "current_weight": current_weight,
                    "target_weight": target_weight,
                },
            )
            risk_verdict = (
                dict(risk_result.to_dict())
                if hasattr(risk_result, "to_dict")
                else dict(risk_result or {})
            )
            if not bool(risk_verdict.get("allowed")):
                block(item, "risk_policy_not_allowed", risk_verdict=risk_verdict)
                continue

            decision = DecisionPolicy().decide(
                awe_patches={factor: {"weight": target_weight, "reason": "factor_model_candidate"}},
                weight_policy_weights={factor: target_weight},
                shadow_perfs={},
                factor_configs={factor: {"enabled": True, "role": "alpha"}},
                current_weights={factor: current_weight},
            ).get(factor)
            decision_preview = {
                "schema_version": "factor_model_decision_policy_preview.v1",
                "required": True,
                "decision": decision.to_api() if decision else {},
                "applied": False,
                "owner": "FactorWeightChangeService",
            }
            if not decision_preview["decision"]:
                block(item, "missing_decision_policy_preview")
                continue

            model_evidence = dict(evidence)
            suggestion_id = str(item.get("suggestion_id") or "")
            candidate_id = f"factor_model:{suggestion_id}"
            model_evidence["source_advisory_id"] = suggestion_id
            model_evidence["candidate_id"] = candidate_id
            model_evidence["candidate_only"] = True
            expected_effect = {
                "schema_version": "factor_governance_model_expected_effect.v1",
                "candidate_only": True,
                "applied": False,
                "current_weight": current_weight,
                "suggested_target_weight": target_weight,
                "estimated_weight_delta": round(target_weight - current_weight, 8),
                "reasons": [
                    {
                        "code": "recent_live_decision_participation",
                        "decision_review_count": len(review_ids),
                        "source": "factor_governance_shadow_audit",
                    },
                    {
                        "code": "model_quality_gate_passed",
                        "artifact_sha256": str(evidence.get("artifact_sha256") or ""),
                    },
                    {
                        "code": "model_counter_evidence_required",
                        "required_before_bridge": True,
                    },
                ],
                "source_presence": {
                    "artifact_sha256": bool(evidence.get("artifact_sha256")),
                    "factor_generation": str(evidence.get("factor_generation") or "") == "runtime_bounded_v1",
                    "lineage_hash": bool(evidence.get("lineage_hash")),
                    "label_contract_hash": bool(evidence.get("label_contract_hash")),
                    "model_quality_gate": gate.get("passed") is True,
                    "factor_sample_coverage": int(evidence.get("sample_count") or 0) >= 20,
                    "counter_evidence": bool(counter_refs),
                },
                "model_evidence": {
                    "artifact_sha256": str(evidence.get("artifact_sha256") or ""),
                    "model_version": str(evidence.get("model_version") or ""),
                    "factor_generation": str(evidence.get("factor_generation") or ""),
                    "lineage_hash": str(evidence.get("lineage_hash") or ""),
                    "label_contract_hash": str(evidence.get("label_contract_hash") or ""),
                },
                "application_state": "candidate_only",
            }
            evidence_refs = {
                "schema_version": "factor_governance_model_candidate_evidence_refs.v1",
                "model_evidence": model_evidence,
                "artifact_sha256": str(evidence.get("artifact_sha256") or ""),
                "model_version": str(evidence.get("model_version") or ""),
                "factor_generation": str(evidence.get("factor_generation") or ""),
                "lineage_hash": str(evidence.get("lineage_hash") or ""),
                "label_contract_hash": str(evidence.get("label_contract_hash") or ""),
                "review_reference_ids": review_ids,
                "counter_evidence_refs": counter_refs,
            }
            candidate = candidate_service.create_candidate(
                candidate_id=candidate_id,
                source_agent="factor_pruning_governance",
                source_kind="factor_governance_model_candidate",
                source_ref_type="factor_governance_shadow_advisory",
                source_ref_id=suggestion_id,
                proposal_stage="brain_candidate",
                capability_scope="factor_catalog_runtime_governance",
                scope_type="factor",
                scope_key=factor,
                action="downweight",
                confidence=_safe_float(item.get("confidence"), 0.55),
                evidence_score=min(
                    0.99,
                    max(0.90, _safe_float(evidence.get("avg_weakness_score"), 0.90)),
                ),
                risk_class="medium",
                max_impact="medium_impact",
                expected_effect=expected_effect,
                evidence_refs=evidence_refs,
                counter_evidence_refs={
                    "schema_version": "factor_governance_model_counter_evidence_refs.v1",
                    "model_counter_evidence": counter_refs,
                    "required_before_bridge": True,
                },
                risk_verdict=risk_verdict,
                decision_policy=decision_preview,
                rollback_plan={
                    "schema_version": "factor_governance_model_rollback_plan.v1",
                    "candidate_lane_only": True,
                    "runtime_mutation": False,
                    "restore_weight": current_weight,
                    "requires_application_effect": True,
                    "effect_missing_blocks_active_promotion": True,
                },
                lineage={
                    "schema_version": "factor_governance_model_candidate_lineage.v1",
                    "phase": "factor_governance_model_candidate_materialization",
                    "source_agent": "lightgbm_shadow_models",
                    "source_advisory_id": suggestion_id,
                    "artifact_sha256": str(evidence.get("artifact_sha256") or ""),
                    "model_version": str(evidence.get("model_version") or ""),
                    "factor_generation": str(evidence.get("factor_generation") or ""),
                    "lineage_hash": str(evidence.get("lineage_hash") or ""),
                    "label_contract_hash": str(evidence.get("label_contract_hash") or ""),
                    "mapped_action": {
                        "policy_action": "downweight",
                        "risk_action": "update_weight",
                        "current_weight": current_weight,
                        "target_weight": target_weight,
                    },
                    "bridge": {
                        "policy_suggestion_direct_write": False,
                        "candidate_review_required": True,
                        "counter_evidence_required": True,
                        "v16_coordinator_application_effect_required": True,
                    },
                },
                now=now,
                persist=True,
            )
            evidence["candidate_id"] = str(candidate.get("candidate_id") or candidate_id)
            evidence["candidate_stage"] = str(candidate.get("proposal_stage") or "brain_candidate")
            evidence["candidate_review_required"] = True
            evidence["candidate_materialization_status"] = "candidate_only"
            evidence["candidate_review_id"] = ""
            evidence["v16_command_id"] = ""
            evidence["mutation_id"] = ""
            evidence["application_id"] = ""
            item["evidence"] = evidence
            candidates.append(
                {
                    "status": "candidate_materialized",
                    "candidate_id": str(candidate.get("candidate_id") or candidate_id),
                    "suggestion_id": suggestion_id,
                    "factor": factor,
                    "proposal_stage": str(candidate.get("proposal_stage") or "brain_candidate"),
                }
            )

        blocked_reasons: dict[str, int] = {}
        for item in blocked:
            reason = str(item.get("reason") or "blocked")
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
        return {
            "schema_version": "factor_governance_model_candidate_materialization.v1",
            "candidate_count": len(candidates),
            "blocked_count": len(blocked),
            "blocked_reasons": dict(sorted(blocked_reasons.items())),
            "blocked_reason": next(iter(sorted(blocked_reasons)), ""),
            "items": candidates + blocked,
        }

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
