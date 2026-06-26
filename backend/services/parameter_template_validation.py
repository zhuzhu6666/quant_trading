from __future__ import annotations

import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from alpha.evaluation.evaluation_context import EvaluationContext
from alpha.evaluation.purged_walkforward import PurgedWalkForward
from alpha.streaming_factor_engine import StreamingFactorEngine
from backend.core.db import DATA_DIR, STATE_DB, STATE_DB_DDL, connect_sqlite
from backend.jobs.progress import ProgressCB
from backend.services.backtest_runner import _load_bars
from backend.services.backtest_service import run_backtest
from backend.services.parameter_templates import ParameterTemplateService
from research.learning.governor import RuleEvolutionGovernor


class ParameterTemplateValidationService:
    def __init__(self, db_path: str | None = None):
        self.db_path = Path(db_path or str(STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.report_dir = DATA_DIR / "parameter_template_validation_reports"
        self.report_dir.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        conn = connect_sqlite(self.db_path)
        try:
            conn.executescript(STATE_DB_DDL)
            conn.commit()
        finally:
            conn.close()

    def _log_lifecycle_event(
        self,
        *,
        factor_id: str,
        event: str,
        status: str,
        description: str,
        reason: str = "",
        score: float = 0.0,
    ) -> None:
        now = time.time()
        conn = connect_sqlite(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO lifecycle_events
                (timestamp, event, factor, source, description, score, status, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    event,
                    factor_id,
                    "parameter_template",
                    description,
                    float(score or 0.0),
                    status,
                    reason,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _new_id(prefix: str) -> str:
        return f"{prefix}_{uuid.uuid4().hex[:16]}"

    def list_release_candidates(
        self,
        *,
        factor_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses = ["1=1"]
        params: list[Any] = []
        if factor_id:
            clauses.append("factor_id=?")
            params.append(factor_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        sql = f"""
            SELECT *
            FROM parameter_template_release_candidate
            WHERE {' AND '.join(clauses)}
            ORDER BY created_at DESC
            LIMIT ?
        """
        params.append(int(limit))
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(sql, tuple(params)).fetchall()
        finally:
            conn.close()
        return [self._parse_release_candidate_row(row) for row in rows]

    def get_release_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        conn = connect_sqlite(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT *
                FROM parameter_template_release_candidate
                WHERE candidate_id=?
                """,
                (candidate_id,),
            ).fetchone()
        finally:
            conn.close()
        return self._parse_release_candidate_row(row) if row else None

    def register_release_candidate(
        self,
        *,
        factor_id: str,
        template_id: str,
        regime_key: str,
        boundary: dict[str, Any],
        walk_forward: dict[str, Any],
        validation_report_path: str,
        recommendation_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        candidate_id = self._new_id("ptrc")
        summary = {
            "walk_forward_passed": bool(walk_forward.get("passed")),
            "candidate_avg_ic": float((walk_forward.get("candidate_summary") or {}).get("avg_ic") or 0.0),
            "baseline_avg_ic": float((walk_forward.get("baseline_summary") or {}).get("avg_ic") or 0.0),
            "candidate_avg_directional_accuracy": float(
                (walk_forward.get("candidate_summary") or {}).get("avg_directional_accuracy") or 0.0
            ),
            "baseline_avg_directional_accuracy": float(
                (walk_forward.get("baseline_summary") or {}).get("avg_directional_accuracy") or 0.0
            ),
            "fold_count": int((walk_forward.get("config") or {}).get("n_folds") or 0),
            "recommendation_source": dict(recommendation_context or {}),
        }
        conn = connect_sqlite(self.db_path)
        try:
            conn.execute(
                """
                INSERT INTO parameter_template_release_candidate
                (candidate_id, factor_id, template_id, regime_key, status,
                 boundary_json, validation_summary_json, validation_report_path,
                 created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending_review', ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    factor_id,
                    template_id,
                    regime_key,
                    json.dumps(boundary, ensure_ascii=False, default=str),
                    json.dumps(summary, ensure_ascii=False, default=str),
                    validation_report_path,
                    now,
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        item = {
            "candidate_id": candidate_id,
            "factor_id": factor_id,
            "template_id": template_id,
            "regime_key": regime_key,
            "status": "pending_review",
            "boundary": boundary,
            "validation_summary": summary,
            "validation_report_path": validation_report_path,
            "created_at": now,
            "updated_at": now,
        }
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_registered",
            status="pending_review",
            description=f"registered release candidate {candidate_id} for {template_id}",
            reason=(
                f"offline_deep_validation_passed:{(recommendation_context or {}).get('recommendation_id', '')}"
                if recommendation_context else
                "offline_deep_validation_passed"
            ),
            score=float(summary.get("candidate_avg_ic") or 0.0),
        )
        return item

    def review_release_candidate(
        self,
        *,
        candidate_id: str,
        status: str,
        note: str = "",
    ) -> dict[str, Any]:
        if status not in {"approved", "rejected"}:
            raise ValueError(f"unsupported review status: {status}")
        current = self.get_release_candidate(candidate_id)
        if not current:
            raise ValueError(f"candidate not found: {candidate_id}")
        if current["status"] not in {"pending_review", "approved", "rejected"}:
            raise ValueError(f"candidate status not reviewable: {current['status']}")
        now = time.time()
        summary = dict(current.get("validation_summary") or {})
        summary["review"] = {
            "status": status,
            "note": note,
            "reviewed_at": now,
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status=status,
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=str(updated.get("factor_id") or ""),
            event="parameter_template_candidate_reviewed",
            status=status,
            description=f"release candidate {candidate_id} reviewed as {status}",
            reason=note or f"candidate_{status}",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        return updated

    def deploy_release_candidate(
        self,
        *,
        candidate_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self.get_release_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"candidate not found: {candidate_id}")
        if candidate["status"] not in {"approved", "deployed"}:
            raise ValueError(f"candidate not approved for release: {candidate['status']}")
        template_service = ParameterTemplateService(str(self.db_path))
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_id = str(candidate.get("template_id") or "")
        active_before = template_service.get_active_template(factor_id=factor_id, regime_key=regime_key)
        old_template_id = str((active_before or {}).get("template_id") or "")
        suggestion = template_service.create_switch_suggestion(
            factor_id=factor_id,
            template_id=template_id,
            regime_key=regime_key,
            note=f"release_candidate:{candidate_id}",
        )
        RuleEvolutionGovernor(str(self.db_path)).set_status(
            suggestion["suggestion_id"],
            "approved",
            f"approved by release candidate {candidate_id}",
        )
        release_result = template_service.activate_template(
            factor_id=factor_id,
            template_id=template_id,
            regime_key=regime_key,
            suggestion_id=suggestion["suggestion_id"],
            note=note or f"deploy release candidate {candidate_id}",
            allow_offline_deep=True,
        )
        if release_result.get("blocked"):
            return {
                "ok": False,
                "blocked": True,
                "candidate": candidate,
                "release_result": release_result,
            }
        now = time.time()
        summary = dict(candidate.get("validation_summary") or {})
        summary["deployment"] = {
            "status": "deployed",
            "note": note,
            "deployed_at": now,
            "old_template_id": old_template_id,
            "new_template_id": template_id,
            "switch_id": release_result.get("switch_id", ""),
            "suggestion_id": suggestion["suggestion_id"],
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status="deployed",
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_deployed",
            status="deployed",
            description=f"deployed release candidate {candidate_id} to {template_id}",
            reason=note or "gray_release_deployed",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        return {
            "ok": True,
            "candidate": updated,
            "release_result": release_result,
        }

    def rollback_release_candidate(
        self,
        *,
        candidate_id: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self.get_release_candidate(candidate_id)
        if not candidate:
            raise ValueError(f"candidate not found: {candidate_id}")
        summary = dict(candidate.get("validation_summary") or {})
        deployment = dict(summary.get("deployment") or {})
        if candidate["status"] != "deployed":
            raise ValueError(f"candidate not deployed: {candidate['status']}")
        old_template_id = str(deployment.get("old_template_id") or "")
        if not old_template_id:
            raise ValueError("candidate has no old_template_id for rollback")
        factor_id = str(candidate.get("factor_id") or "")
        regime_key = str(candidate.get("regime_key") or "")
        template_service = ParameterTemplateService(str(self.db_path))
        rollback_result = template_service.activate_template(
            factor_id=factor_id,
            template_id=old_template_id,
            regime_key=regime_key,
            note=note or f"rollback release candidate {candidate_id}",
            allow_offline_deep=True,
        )
        if rollback_result.get("blocked"):
            return {
                "ok": False,
                "blocked": True,
                "candidate": candidate,
                "rollback_result": rollback_result,
            }
        now = time.time()
        summary["rollback"] = {
            "status": "rolled_back",
            "note": note,
            "rolled_back_at": now,
            "restored_template_id": old_template_id,
            "switch_id": rollback_result.get("switch_id", ""),
        }
        self._update_release_candidate(
            candidate_id=candidate_id,
            status="rolled_back",
            validation_summary=summary,
            updated_at=now,
        )
        updated = self.get_release_candidate(candidate_id)
        assert updated is not None
        self._log_lifecycle_event(
            factor_id=factor_id,
            event="parameter_template_candidate_rolled_back",
            status="rolled_back",
            description=f"rolled back release candidate {candidate_id} to {old_template_id}",
            reason=note or "gray_release_rolled_back",
            score=float((updated.get("validation_summary") or {}).get("candidate_avg_ic") or 0.0),
        )
        return {
            "ok": True,
            "candidate": updated,
            "rollback_result": rollback_result,
        }

    def _update_release_candidate(
        self,
        *,
        candidate_id: str,
        status: str,
        validation_summary: dict[str, Any],
        updated_at: float,
    ) -> None:
        conn = connect_sqlite(self.db_path)
        try:
            conn.execute(
                """
                UPDATE parameter_template_release_candidate
                SET status=?, validation_summary_json=?, updated_at=?
                WHERE candidate_id=?
                """,
                (
                    status,
                    json.dumps(validation_summary, ensure_ascii=False, default=str),
                    updated_at,
                    candidate_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _parse_release_candidate_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "candidate_id": str(row["candidate_id"] or ""),
            "factor_id": str(row["factor_id"] or ""),
            "template_id": str(row["template_id"] or ""),
            "regime_key": str(row["regime_key"] or ""),
            "status": str(row["status"] or ""),
            "boundary": json.loads(row["boundary_json"] or "{}"),
            "validation_summary": json.loads(row["validation_summary_json"] or "{}"),
            "validation_report_path": str(row["validation_report_path"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "updated_at": float(row["updated_at"] or 0.0),
        }


def build_offline_validation_plan(boundary: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "stage": "boundary_check",
            "status": "completed",
            "kind": "governance_guardrail",
            "note": "offline_deep required before runtime switch",
        },
        {
            "stage": "backtest_sweep",
            "status": "queued",
            "kind": "backtest",
            "note": "run parameter-template candidate against existing backtest sweep entry",
        },
        {
            "stage": "walk_forward_review",
            "status": "queued",
            "kind": "walk_forward",
            "note": "attach out-of-sample fold evidence before approval",
        },
        {
            "stage": "gray_release_review",
            "status": "queued",
            "kind": "gray_release",
            "note": "materialize a pending_review release candidate after offline evidence passes",
        },
    ]


def _sanitize_series(values: Any) -> np.ndarray:
    arr = np.asarray(values, dtype=float)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    arr[np.isinf(arr)] = np.nan
    return arr


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    mask = np.isfinite(a) & np.isfinite(b)
    if int(mask.sum()) < 3:
        return None
    av = a[mask]
    bv = b[mask]
    if np.nanstd(av) <= 1e-12 or np.nanstd(bv) <= 1e-12:
        return None
    value = float(np.corrcoef(av, bv)[0, 1])
    if math.isnan(value) or math.isinf(value):
        return None
    return value


def _directional_accuracy(signal: np.ndarray, fwd_returns: np.ndarray) -> float | None:
    mask = np.isfinite(signal) & np.isfinite(fwd_returns) & (signal != 0) & (fwd_returns != 0)
    if int(mask.sum()) < 3:
        return None
    pred = np.sign(signal[mask])
    actual = np.sign(fwd_returns[mask])
    return float(np.mean(pred == actual))


def _mean(values: list[float | None]) -> float | None:
    clean = [float(v) for v in values if isinstance(v, (int, float)) and math.isfinite(float(v))]
    if not clean:
        return None
    return float(sum(clean) / len(clean))


def _fit_walk_forward_config(
    *,
    n_total: int,
    n_folds: int,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
) -> dict[str, int]:
    fitted_folds = max(1, int(n_folds))
    fitted_test = max(10, int(test_bars))
    fitted_train = max(StreamingFactorEngine.MIN_BARS, int(train_bars))
    purge = max(0, int(purge_bars))
    embargo = max(0, int(embargo_bars))

    max_train = n_total - purge - embargo - fitted_test * fitted_folds
    if max_train < fitted_train:
        fitted_train = max(StreamingFactorEngine.MIN_BARS, max_train)

    max_folds = max(1, (n_total - fitted_train - purge - embargo) // max(fitted_test, 1))
    fitted_folds = max(1, min(fitted_folds, max_folds))

    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_test = max(
            10,
            (n_total - fitted_train - purge - embargo) // max(fitted_folds, 1),
        )
    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_folds = 1
        fitted_test = max(10, n_total - fitted_train - purge - embargo)
    if fitted_train + purge + embargo + fitted_test * fitted_folds > n_total:
        fitted_train = max(
            StreamingFactorEngine.MIN_BARS,
            n_total - purge - embargo - fitted_test * fitted_folds,
        )
    return {
        "n_folds": max(1, fitted_folds),
        "train_bars": max(StreamingFactorEngine.MIN_BARS, fitted_train),
        "test_bars": max(10, fitted_test),
        "purge_bars": purge,
        "embargo_bars": embargo,
    }


def _evaluate_factor_template(
    *,
    factor_id: str,
    base_df: pd.DataFrame,
    candidate_overrides: dict[str, Any],
    baseline_overrides: dict[str, Any],
    n_folds: int,
    train_bars: int,
    test_bars: int,
    purge_bars: int,
    embargo_bars: int,
) -> dict[str, Any]:
    fitted = _fit_walk_forward_config(
        n_total=len(base_df),
        n_folds=n_folds,
        train_bars=train_bars,
        test_bars=test_bars,
        purge_bars=purge_bars,
        embargo_bars=embargo_bars,
    )
    eval_ctx = EvaluationContext(
        train_bars=fitted["train_bars"],
        test_bars=fitted["test_bars"],
        purge_bars=fitted["purge_bars"],
        embargo_bars=fitted["embargo_bars"],
    )
    folds = list(PurgedWalkForward(eval_ctx, n_folds=fitted["n_folds"]).folds(n_total=len(base_df)))
    candidate_engine = StreamingFactorEngine(
        max_buffer=max(len(base_df), StreamingFactorEngine.MIN_BARS + 1),
        factor_runtime_config={factor_id: {"parameter_overrides": dict(candidate_overrides or {})}},
    )
    baseline_engine = StreamingFactorEngine(
        max_buffer=max(len(base_df), StreamingFactorEngine.MIN_BARS + 1),
        factor_runtime_config={factor_id: {"parameter_overrides": dict(baseline_overrides or {})}},
    )
    candidate_series = _sanitize_series(candidate_engine._compute_factor_series(factor_id, base_df))
    baseline_series = _sanitize_series(baseline_engine._compute_factor_series(factor_id, base_df))
    close = base_df["close"].to_numpy(dtype=float)
    fwd_returns = np.append((close[1:] - close[:-1]) / close[:-1], np.nan)

    fold_items: list[dict[str, Any]] = []
    candidate_ic_values: list[float | None] = []
    baseline_ic_values: list[float | None] = []
    candidate_da_values: list[float | None] = []
    baseline_da_values: list[float | None] = []
    for fold in folds:
        test_idx = fold.test_indices
        cand_test = candidate_series[test_idx]
        base_test = baseline_series[test_idx]
        ret_test = fwd_returns[test_idx]
        candidate_ic = _corr(cand_test, ret_test)
        baseline_ic = _corr(base_test, ret_test)
        candidate_da = _directional_accuracy(cand_test, ret_test)
        baseline_da = _directional_accuracy(base_test, ret_test)
        candidate_ic_values.append(candidate_ic)
        baseline_ic_values.append(baseline_ic)
        candidate_da_values.append(candidate_da)
        baseline_da_values.append(baseline_da)
        fold_items.append(
            {
                "fold_id": int(fold.fold_id),
                "test_size": int(len(test_idx)),
                "candidate_ic": candidate_ic,
                "baseline_ic": baseline_ic,
                "candidate_directional_accuracy": candidate_da,
                "baseline_directional_accuracy": baseline_da,
            }
        )

    candidate_summary = {
        "avg_ic": _mean(candidate_ic_values),
        "avg_directional_accuracy": _mean(candidate_da_values),
    }
    baseline_summary = {
        "avg_ic": _mean(baseline_ic_values),
        "avg_directional_accuracy": _mean(baseline_da_values),
    }
    candidate_avg_ic = candidate_summary["avg_ic"] if candidate_summary["avg_ic"] is not None else -1.0
    baseline_avg_ic = baseline_summary["avg_ic"] if baseline_summary["avg_ic"] is not None else -1.0
    passed = candidate_avg_ic >= baseline_avg_ic - 1e-9
    return {
        "passed": passed,
        "config": {
            **fitted,
        },
        "candidate_summary": candidate_summary,
        "baseline_summary": baseline_summary,
        "folds": fold_items,
    }


def _write_validation_report(
    *,
    report_dir: Path,
    factor_id: str,
    template_id: str,
    payload: dict[str, Any],
) -> str:
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"parameter_template_validation_{factor_id}_{template_id.replace(':', '_')}_{ts}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return str(path)


def run_parameter_template_offline_validation(
    params: dict[str, Any],
    progress_cb: ProgressCB,
) -> dict[str, Any]:
    service = ParameterTemplateService()
    validation_service = ParameterTemplateValidationService()
    factor_id = str(params.get("factor_id") or "")
    template_id = str(params.get("template_id") or "")
    regime_key = str(params.get("regime_key") or "")
    boundary = service.assess_template_change(
        factor_id=factor_id,
        target_template_id=template_id,
        regime_key=regime_key,
    )
    plan = build_offline_validation_plan(boundary)
    if boundary.get("recommended_scope") != "offline_deep":
        progress_cb("skipped", 100, "template fits online_light; offline validation not required")
        return {
            "ok": False,
            "skipped": True,
            "message": "template fits online_light; use governed apply-switch flow instead",
            "boundary": boundary,
            "validation_plan": plan,
        }

    progress_cb("planning", 5, f"planning offline validation for {factor_id}")
    backtest_params = {
        "symbol": params.get("symbol", "XAUUSD+"),
        "timeframe": params.get("timeframe", "M15"),
        "risk_per_trade_pct": params.get("risk_per_trade_pct"),
        "enable_circuit": bool(params.get("enable_circuit", False)),
    }
    backtest_result = run_backtest(backtest_params, progress_cb)
    plan[1]["status"] = "completed"

    progress_cb("walk_forward", 93, f"running purged walk-forward for {factor_id}")
    target_template = boundary.get("target_template") or {}
    current_template = boundary.get("current_template") or {}
    base_df = _load_bars(
        str(backtest_params.get("symbol") or "XAUUSD+"),
        str(backtest_params.get("timeframe") or "M15"),
    )
    walk_forward = _evaluate_factor_template(
        factor_id=factor_id,
        base_df=base_df,
        candidate_overrides=target_template.get("parameters") or {},
        baseline_overrides=current_template.get("parameters") or {},
        n_folds=max(2, int(params.get("walk_forward_folds") or 3)),
        train_bars=max(80, int(params.get("walk_forward_train_bars") or 180)),
        test_bars=max(20, int(params.get("walk_forward_test_bars") or 40)),
        purge_bars=max(0, int(params.get("walk_forward_purge_bars") or 5)),
        embargo_bars=max(0, int(params.get("walk_forward_embargo_bars") or 5)),
    )
    plan[2]["status"] = "completed"

    report_payload = {
        "schema_version": "parameter_template_validation.v1",
        "factor_id": factor_id,
        "template_id": template_id,
        "regime_key": regime_key,
        "boundary": boundary,
        "recommendation_context": dict(params.get("recommendation_context") or {}),
        "backtest": backtest_result,
        "walk_forward": walk_forward,
        "validation_plan": plan,
        "created_at": time.time(),
    }
    report_path = _write_validation_report(
        report_dir=validation_service.report_dir,
        factor_id=factor_id,
        template_id=template_id,
        payload=report_payload,
    )
    release_candidate = validation_service.register_release_candidate(
        factor_id=factor_id,
        template_id=template_id,
        regime_key=regime_key,
        boundary=boundary,
        walk_forward=walk_forward,
        validation_report_path=report_path,
        recommendation_context=dict(params.get("recommendation_context") or {}),
    )
    plan[3]["status"] = "completed"
    return {
        "ok": True,
        "mode": "offline_deep",
        "factor_id": factor_id,
        "template_id": template_id,
        "regime_key": regime_key,
        "boundary": boundary,
        "validation_plan": plan,
        "backtest": backtest_result,
        "walk_forward": walk_forward,
        "release_candidate": release_candidate,
        "report_path": report_path,
        "note": "offline_deep now emits walk-forward evidence and a pending gray-release candidate",
    }

