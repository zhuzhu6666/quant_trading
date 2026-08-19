from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.canonical_v2_reader import (
    canonical_ready,
    decision_row,
    iter_decision_rows,
    iter_order_rows,
    iter_position_rows,
    iter_review_rows,
    review_row,
)
from backend.services.evolution_ledger import current_runtime_config_snapshot
from backend.services.fact_envelope import observed_epoch
from backend.services.review_contract import review_has_system_contamination
from backend.services.state_payload_archive import load_json_payload, load_supervisor_trace_archive


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_REPLAY_ARTIFACT_DIR = PROJECT_ROOT / "data" / "replay_reports"


def _dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


from backend.core.db_helpers import (
    conn_is_pg as _conn_is_pg,
    load_json as _loads,
    pg_sql as _sql,
)


def _hash(value: Any) -> str:
    return hashlib.sha256(_dumps(value).encode("utf-8")).hexdigest()


def _hash_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _use_pg(db_path: str | Path = STATE_DB) -> bool:
    return is_state_db_path(db_path)


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    conn = get_state_pg_conn(read_only=read_only) if _use_pg(db_path) else connect_sqlite(db_path, read_only=read_only)
    if not _use_pg(db_path):
        conn.row_factory = __import__("sqlite3").Row
    return conn


def _execute(conn, sql: str, params: Any = None):
    rendered = _sql(conn, sql)
    if _conn_is_pg(conn) and is_state_schema_write_sql(rendered):
        return validate_runtime_state_schema(conn, rendered)
    if params is None:
        return conn.execute(rendered)
    return conn.execute(rendered, params)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _code_version() -> str:
    head = PROJECT_ROOT / ".git" / "HEAD"
    try:
        raw = head.read_text(encoding="utf-8").strip()
        if raw.startswith("ref:"):
            ref_path = PROJECT_ROOT / ".git" / raw.split(" ", 1)[1].strip()
            return ref_path.read_text(encoding="utf-8").strip()[:40]
        return raw[:40]
    except Exception:
        return "unknown"


def ensure_replay_report_table(db_path: str | Path = STATE_DB) -> None:
    conn = _connect(db_path)
    try:
        _execute(
            conn,
            """
            CREATE TABLE IF NOT EXISTS replay_report (
                replay_run_id TEXT PRIMARY KEY,
                scope_json TEXT NOT NULL DEFAULT '{}',
                input_dataset_hash TEXT DEFAULT '',
                runtime_config_hash TEXT DEFAULT '',
                code_version TEXT DEFAULT '',
                decision_count INTEGER DEFAULT 0,
                matched_live_count INTEGER DEFAULT 0,
                mismatch_count INTEGER DEFAULT 0,
                metric_summary_json TEXT NOT NULL DEFAULT '{}',
                replay_error TEXT DEFAULT '',
                evidence_grade TEXT DEFAULT '',
                artifact_path TEXT DEFAULT '',
                artifact_hash TEXT DEFAULT '',
                status TEXT DEFAULT 'completed',
                created_at REAL NOT NULL DEFAULT 0.0
            )
            """,
        )
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_replay_report_created ON replay_report(created_at)")
        _execute(conn, "CREATE INDEX IF NOT EXISTS idx_replay_report_grade ON replay_report(evidence_grade, created_at)")
        conn.commit()
    finally:
        conn.close()


def _extract_gate(action: dict[str, Any], portfolio: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        action.get("gate_result"),
        action.get("execution_gate"),
        action.get("gate"),
        portfolio.get("gate_result"),
        portfolio.get("execution_gate"),
    ]
    for item in candidates:
        if isinstance(item, dict):
            return dict(item)
    if "gate_passed" in action or "gate_reason" in action:
        return {
            "passed": bool(action.get("gate_passed")),
            "reason": str(action.get("gate_reason") or ""),
        }
    return {}


def _extract_risk(action: dict[str, Any], risk_state: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    state_verdict = risk_state.get("policy_verdict") if isinstance(risk_state.get("policy_verdict"), dict) else {}
    action_verdict = action.get("risk_verdict") if isinstance(action.get("risk_verdict"), dict) else {}
    return dict(state_verdict or {}), dict(action_verdict or {})


def _verdict_signature(verdict: dict[str, Any]) -> tuple[Any, str]:
    return verdict.get("allowed"), str(verdict.get("reason") or "")


def _timeframe_seconds(timeframe: str) -> int:
    tf = str(timeframe or "M5").strip().upper()
    if not tf:
        return 300
    unit = tf[0]
    try:
        value = int(tf[1:] or "1")
    except Exception:
        value = 5
    if unit == "S":
        return max(1, value)
    if unit == "M":
        return max(1, value) * 60
    if unit == "H":
        return max(1, value) * 3600
    if unit == "D":
        return max(1, value) * 86400
    return 300


def _epoch(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        import pandas as pd

        ts = pd.Timestamp(value)
        if ts.tzinfo is not None:
            ts = ts.tz_convert("UTC")
        return float(ts.timestamp())
    except Exception:
        return 0.0


def _bar_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "time": round(_epoch(row.get("time")), 3),
        "open": round(_safe_float(row.get("open")), 6),
        "high": round(_safe_float(row.get("high")), 6),
        "low": round(_safe_float(row.get("low")), 6),
        "close": round(_safe_float(row.get("close")), 6),
        "volume": round(_safe_float(row.get("volume")), 6),
    }


def _gate_signature(gate: dict[str, Any]) -> tuple[bool | None, str]:
    if not gate:
        return None, ""
    return bool(gate.get("passed")), str(gate.get("reason") or "")


class ReplayHarnessService:
    """V15 replay harness v1.

    The first harness is deliberately audit-only: it verifies that live ledger
    rows contain replayable factor, gate, and RiskPolicyService verdict anchors.
    It does not mutate runtime config, weights, orders, or broker state.
    """

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        artifact_dir: str | Path = DEFAULT_REPLAY_ARTIFACT_DIR,
    ):
        self.db_path = db_path
        self.artifact_dir = Path(artifact_dir)

    def run_factor_gate_risk_replay(
        self,
        *,
        lookback_days: float = 7.0,
        limit: int = 500,
        replay_run_id: str = "",
    ) -> dict[str, Any]:
        ensure_replay_report_table(self.db_path)
        started_at = time.time()
        scope = {
            "schema_version": "replay_scope.v1",
            "kind": "factor_gate_risk",
            "lookback_days": float(lookback_days),
            "limit": int(limit),
            "read_only": True,
            "risk_policy_boundary": "verifies_existing_RiskPolicyService_verdicts",
        }
        run_id = str(replay_run_id or f"replay_{uuid.uuid4().hex[:16]}")
        error = ""
        report: dict[str, Any]
        try:
            rows = self._load_decisions(since_ts=started_at - max(0.0, float(lookback_days)) * 86400.0, limit=limit)
            report = self._build_report(run_id=run_id, scope=scope, rows=rows, created_at=started_at, replay_error="")
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            report = self._build_report(run_id=run_id, scope=scope, rows=[], created_at=started_at, replay_error=error)
        report = self._attach_artifact(report)
        self._persist_report(report)
        return report

    def run_bar_replay_evidence(
        self,
        *,
        lookback_days: float = 7.0,
        limit: int = 200,
        warmup_bars: int = 80,
        post_bars: int = 1,
        replay_run_id: str = "",
    ) -> dict[str, Any]:
        ensure_replay_report_table(self.db_path)
        started_at = time.time()
        scope = {
            "schema_version": "replay_scope.v1",
            "kind": "bar_replay_evidence",
            "lookback_days": float(lookback_days),
            "limit": int(limit),
            "warmup_bars": int(warmup_bars),
            "post_bars": int(post_bars),
            "read_only": True,
            "risk_policy_boundary": "verifies_existing_RiskPolicyService_verdicts",
            "phase": "v15_phase1",
        }
        run_id = str(replay_run_id or f"bar_replay_{uuid.uuid4().hex[:16]}")
        try:
            rows = self._load_decisions(since_ts=started_at - max(0.0, float(lookback_days)) * 86400.0, limit=limit)
            report = self._build_report(run_id=run_id, scope=scope, rows=rows, created_at=started_at, replay_error="")
            bar_eval = self._evaluate_bar_windows(rows, warmup_bars=max(1, int(warmup_bars)), post_bars=max(0, int(post_bars)))
            frame_eval = self._evaluate_factor_frames(bar_eval["windows"])
            recompute_eval = self._evaluate_gate_risk_recompute(rows, bar_eval["windows"])
            lifecycle_eval = self._evaluate_lifecycle_replay(rows)
            outcome_learning = self._trade_outcome_learning_preview(rows)
            metrics = {
                **dict(report.get("metric_summary") or {}),
                "bar_replay": bar_eval["metrics"],
                "factor_frame_replay": frame_eval["metrics"],
                "execution_gate_recompute": recompute_eval["gate_metrics"],
                "risk_policy_recompute": recompute_eval["risk_metrics"],
                "order_lifecycle_replay": lifecycle_eval["order_metrics"],
                "order_outcome_causality_replay": lifecycle_eval["order_outcome_metrics"],
                "broker_fill_slippage_replay": lifecycle_eval["slippage_metrics"],
                "position_lifecycle_replay": lifecycle_eval["position_metrics"],
                "supervisor_action_replay": lifecycle_eval["supervisor_metrics"],
                "supervisor_counterfactual_replay": lifecycle_eval["counterfactual_metrics"],
                "risk_policy_subaction_replay": lifecycle_eval["risk_subaction_metrics"],
                "bar_window_preview": self._bar_window_preview(bar_eval["windows"]),
                "trade_outcome_learning_preview": outcome_learning,
            }
            report = {
                **report,
                "input_dataset_hash": _hash(
                    {
                        "decision_anchor_hash": report.get("input_dataset_hash"),
                        "bar_window_hash": bar_eval["metrics"].get("bar_window_hash"),
                        "factor_frame_hash": frame_eval["metrics"].get("factor_frame_hash"),
                        "execution_gate_recompute_hash": recompute_eval["gate_metrics"].get("recompute_hash"),
                        "risk_policy_recompute_hash": recompute_eval["risk_metrics"].get("recompute_hash"),
                        "order_lifecycle_hash": lifecycle_eval["order_metrics"].get("lifecycle_hash"),
                        "order_outcome_causality_hash": lifecycle_eval["order_outcome_metrics"].get("causality_hash"),
                        "broker_fill_slippage_hash": lifecycle_eval["slippage_metrics"].get("slippage_hash"),
                        "position_lifecycle_hash": lifecycle_eval["position_metrics"].get("lifecycle_hash"),
                        "supervisor_action_hash": lifecycle_eval["supervisor_metrics"].get("supervisor_hash"),
                        "supervisor_counterfactual_hash": lifecycle_eval["counterfactual_metrics"].get("counterfactual_hash"),
                        "risk_policy_subaction_hash": lifecycle_eval["risk_subaction_metrics"].get("recompute_hash"),
                    }
                ),
                "matched_live_count": min(_safe_int(report.get("matched_live_count")), _safe_int(bar_eval["metrics"].get("aligned_decision_count"))),
                "mismatch_count": max(
                    _safe_int(report.get("mismatch_count")),
                    _safe_int(bar_eval["metrics"].get("bar_window_mismatch_count")),
                    _safe_int(frame_eval["metrics"].get("factor_frame_mismatch_count")),
                    _safe_int(recompute_eval["gate_metrics"].get("disagreement_count")),
                    _safe_int(recompute_eval["risk_metrics"].get("disagreement_count")),
                    _safe_int(lifecycle_eval["order_metrics"].get("missing_expected_event_count")),
                    _safe_int(lifecycle_eval["order_outcome_metrics"].get("causality_issue_count")),
                    _safe_int(lifecycle_eval["position_metrics"].get("missing_expected_event_count")),
                    _safe_int(lifecycle_eval["supervisor_metrics"].get("trace_integrity_issue_count")),
                    _safe_int(lifecycle_eval["risk_subaction_metrics"].get("disagreement_count")),
                    _safe_int(lifecycle_eval["risk_subaction_metrics"].get("error_count")),
                ),
                "metric_summary": metrics,
                "evidence_grade": self._p1_replay_grade(
                    str(report.get("evidence_grade") or ""),
                    bar_eval["metrics"],
                    frame_eval["metrics"],
                    recompute_eval["gate_metrics"],
                    recompute_eval["risk_metrics"],
                    len(rows),
                ),
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            report = self._build_report(run_id=run_id, scope=scope, rows=[], created_at=started_at, replay_error=error)
        report = self._attach_artifact(report)
        self._persist_report(report)
        return report

    def run_bar_replay_freshness(
        self,
        *,
        lookback_days: float = 1.0,
        limit: int = 20,
        warmup_bars: int = 20,
        post_bars: int = 1,
        window_sample_limit: int = 5,
        replay_run_id: str = "",
    ) -> dict[str, Any]:
        """Run a light nursery replay freshness check without full recompute."""
        ensure_replay_report_table(self.db_path)
        started_at = time.time()
        scope = {
            "schema_version": "replay_scope.v1",
            "kind": "bar_replay_freshness",
            "lookback_days": float(lookback_days),
            "limit": int(limit),
            "warmup_bars": int(warmup_bars),
            "post_bars": int(post_bars),
            "window_sample_limit": int(window_sample_limit),
            "read_only": True,
            "risk_policy_boundary": "does_not_recompute_or_mutate_RiskPolicyService",
            "phase": "demo_nursery_freshness",
        }
        run_id = str(replay_run_id or f"bar_freshness_{uuid.uuid4().hex[:16]}")
        try:
            rows = self._load_decisions(since_ts=started_at - max(0.0, float(lookback_days)) * 86400.0, limit=limit)
            samples: list[dict[str, Any]] = []
            bar_load_errors: list[dict[str, Any]] = []
            bar_loaded = 0
            for row in rows[: max(0, min(int(window_sample_limit), 20))]:
                decision_ts = _safe_float(row.get("decision_ts") or row.get("created_at"))
                symbol = str(row.get("symbol") or "XAUUSD+")
                timeframe = str(row.get("timeframe") or "M1")
                try:
                    bars = self._load_bar_window(
                        symbol=symbol,
                        timeframe=timeframe,
                        decision_ts=decision_ts,
                        warmup_bars=max(1, int(warmup_bars)),
                        post_bars=max(0, int(post_bars)),
                    )
                    loaded_count = len(bars)
                    if loaded_count:
                        bar_loaded += 1
                    samples.append(
                        {
                            "decision_id": str(row.get("decision_id") or ""),
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "bar_count": loaded_count,
                        }
                    )
                except Exception as exc:
                    bar_load_errors.append(
                        {
                            "decision_id": str(row.get("decision_id") or ""),
                            "symbol": symbol,
                            "timeframe": timeframe,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    if len(bar_load_errors) >= 5:
                        break
            replay_error = ""
            if bar_load_errors:
                replay_error = f"bar_window_load_error: {bar_load_errors[0].get('error')}"
            report = self._build_report(run_id=run_id, scope=scope, rows=rows, created_at=started_at, replay_error=replay_error)
            metrics = dict(report.get("metric_summary") or {})
            metrics["nursery_freshness"] = {
                "schema_version": "replay_nursery_freshness_metrics.v1",
                "decision_count": len(rows),
                "sampled_window_count": len(samples),
                "bar_loaded_window_count": bar_loaded,
                "bar_load_error_count": len(bar_load_errors),
                "samples": samples,
                "bar_load_errors": bar_load_errors,
                "full_recompute": False,
            }
            if rows and not replay_error:
                report = {
                    **report,
                    "metric_summary": metrics,
                    "evidence_grade": "B" if bar_loaded else "C",
                    "status": "completed",
                }
            else:
                report = {**report, "metric_summary": metrics}
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            report = self._build_report(run_id=run_id, scope=scope, rows=[], created_at=started_at, replay_error=error)
        report = self._attach_artifact(report)
        self._persist_report(report)
        return report

    def run_bar_window_preview(
        self,
        *,
        lookback_days: float = 1.0,
        limit: int = 1,
        warmup_bars: int = 40,
        post_bars: int = 24,
        decision_id: str = "",
        replay_run_id: str = "",
    ) -> dict[str, Any]:
        """Fast, read-only bar-window preview for the operator UI.

        This intentionally does not persist a replay_report and does not run
        factor-frame, risk recompute, lifecycle, broker, or supervisor replay.
        Full audit evidence remains under run_bar_replay_evidence().
        """
        started_at = time.time()
        scope = {
            "schema_version": "replay_scope.v1",
            "kind": "bar_window_preview",
            "lookback_days": float(lookback_days),
            "limit": int(limit),
            "warmup_bars": int(warmup_bars),
            "post_bars": int(post_bars),
            "decision_id": str(decision_id or ""),
            "read_only": True,
            "risk_policy_boundary": "preview_only_no_RiskPolicyService_mutation",
            "phase": "v15_phase1_ui",
        }
        run_id = str(replay_run_id or f"bar_preview_{uuid.uuid4().hex[:16]}")
        try:
            rows = self._load_preview_decisions(
                since_ts=started_at - max(0.0, float(lookback_days)) * 86400.0,
                limit=limit,
                decision_id=decision_id,
            )
            bar_eval = self._evaluate_bar_windows(rows, warmup_bars=max(1, int(warmup_bars)), post_bars=max(0, int(post_bars)))
            bar_metrics = bar_eval["metrics"]
            metrics = {
                "bar_replay": bar_metrics,
                "bar_window_preview": self._bar_window_preview(bar_eval["windows"], max_windows=3, max_bars=max(10, int(warmup_bars) + int(post_bars) + 4)),
                "trade_outcome_learning_preview": self._trade_outcome_learning_preview(rows),
            }
            decision_count = len(rows)
            mismatch_count = _safe_int(bar_metrics.get("bar_window_mismatch_count"))
            return {
                "replay_run_id": run_id,
                "scope": scope,
                "input_dataset_hash": _hash(
                    {
                        "decision_ids": [str(row.get("decision_id") or "") for row in rows],
                        "bar_window_hash": bar_metrics.get("bar_window_hash"),
                    }
                ),
                "runtime_config_hash": str(current_runtime_config_snapshot(db_path=self.db_path).get("config_hash") or ""),
                "code_version": _code_version(),
                "decision_count": decision_count,
                "matched_live_count": _safe_int(bar_metrics.get("aligned_decision_count")),
                "mismatch_count": mismatch_count,
                "metric_summary": metrics,
                "replay_error": "",
                "evidence_grade": "A" if decision_count and mismatch_count == 0 else "C" if decision_count else "missing",
                "artifact_path": "",
                "artifact_hash": str(bar_metrics.get("bar_window_hash") or ""),
                "status": "completed",
                "created_at": started_at,
            }
        except Exception as exc:
            return {
                "replay_run_id": run_id,
                "scope": scope,
                "input_dataset_hash": "",
                "runtime_config_hash": "",
                "code_version": _code_version(),
                "decision_count": 0,
                "matched_live_count": 0,
                "mismatch_count": 0,
                "metric_summary": {},
                "replay_error": f"{type(exc).__name__}: {exc}",
                "evidence_grade": "failed",
                "artifact_path": "",
                "artifact_hash": "",
                "status": "failed",
                "created_at": started_at,
            }

    def list_bar_preview_decisions(
        self,
        *,
        lookback_days: float = 7.0,
        limit: int = 30,
        offset: int = 0,
    ) -> dict[str, Any]:
        started_at = time.time()
        since_ts = started_at - max(0.0, float(lookback_days)) * 86400.0
        rows = self._load_preview_decision_candidates(
            since_ts=since_ts,
            limit=max(1, int(limit)),
            offset=max(0, int(offset)),
        )
        outcome_learning = self._trade_outcome_learning_preview(rows, max_items=len(rows) if rows else 1)
        outcome_by_decision = {
            str(item.get("decision_id") or ""): item
            for item in outcome_learning.get("items", [])
        }
        items: list[dict[str, Any]] = []
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            linked = dict(outcome_by_decision.get(decision_id) or {})
            outcome = dict(linked.get("outcome") or {})
            learning = dict(linked.get("learning") or {})
            direction = self._decision_direction(row)
            entry_ts = round(_safe_float(row.get("decision_ts")), 3)
            raw_exit_ts = _safe_float(outcome.get("close_ts"))
            outcome_status = str(outcome.get("status") or "")
            exit_ts = round(raw_exit_ts, 3) if outcome_status == "closed" and raw_exit_ts > entry_ts else None
            action_score = round(_safe_float(row.get("action_score")), 6)
            action_reason = str(row.get("action_reason") or "")
            outcome_result = str(outcome.get("result") or "")
            outcome_label = str(outcome.get("outcome_label") or "")
            pnl = _safe_float(outcome.get("pnl"))
            close_reason = str(outcome.get("close_reason") or "")
            items.append(
                {
                    "decision_id": decision_id,
                    "trade_id": str(row.get("trade_id") or ""),
                    "position_id": str(row.get("position_id") or ""),
                    "event_type": str(row.get("event_type") or ""),
                    "symbol": str(row.get("symbol") or ""),
                    "timeframe": str(row.get("timeframe") or ""),
                    "direction": direction,
                    "direction_label": self._direction_label(direction),
                    "decision_ts": entry_ts,
                    "entry_ts": entry_ts,
                    "exit_ts": exit_ts,
                    "exit_decision_id": str(outcome.get("exit_decision_id") or "") or None,
                    "close_reason": close_reason or None,
                    "holding_seconds": round(exit_ts - entry_ts, 3) if exit_ts is not None else None,
                    "action_score": action_score,
                    "action_reason": action_reason,
                    "outcome_status": outcome_status,
                    "outcome_result": outcome_result,
                    "pnl": pnl,
                    "outcome_label": outcome_label,
                    "system_view": {
                        "direction": direction,
                        "direction_label": self._direction_label(direction),
                        "score": action_score,
                        "action_reason": action_reason or None,
                        "outcome_status": outcome_status or None,
                        "outcome_result": outcome_result or None,
                        "outcome_label": outcome_label or None,
                        "pnl": pnl if outcome_status == "closed" else None,
                        "close_reason": close_reason or None,
                        "summary": str(outcome.get("summary") or "") or None,
                    },
                    "learning_status": str(learning.get("status") or ""),
                    "sample_count": _safe_int(learning.get("sample_count")),
                    "matured_sample_count": _safe_int(learning.get("matured_sample_count")),
                }
            )
        return {
            "schema_version": "bar_preview_decision_choices.v1",
            "lookback_days": float(lookback_days),
            "limit": int(limit),
            "offset": int(offset),
            "item_count": len(items),
            "items": items,
        }

    def latest_report(self) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            if not state_table_exists(conn, "replay_report"):
                return {"ok": False, "status": "missing_table"}
            row = _execute(
                conn,
                """
                SELECT *
                FROM replay_report
                ORDER BY created_at DESC
                LIMIT 1
                """,
            ).fetchone()
            if not row:
                return {"ok": False, "status": "missing_report"}
            return self._row_to_report(dict(row))
        finally:
            conn.close()

    def status(self, *, stale_after_sec: float = 86400.0) -> dict[str, Any]:
        latest = self.latest_report()
        if not latest.get("replay_run_id"):
            return {
                "ok": False,
                "status": latest.get("status", "missing_report"),
                "schema_version": "replay_readiness.v1",
                "latest_report": latest,
                "stale": True,
                "stale_after_seconds": stale_after_sec,
            }
        age = max(0.0, time.time() - _safe_float(latest.get("created_at")))
        stale = age > stale_after_sec
        ok = not stale and not latest.get("replay_error") and str(latest.get("evidence_grade") or "") not in {"missing", "failed"}
        return {
            "ok": ok,
            "status": "fresh" if ok else "stale" if stale else "degraded",
            "schema_version": "replay_readiness.v1",
            "latest_report": latest,
            "age_seconds": round(age, 3),
            "stale": stale,
            "stale_after_seconds": stale_after_sec,
        }

    _DECISION_EVENT_TYPES = ("open", "skip", "order_failed")

    def _load_decisions(self, *, since_ts: float, limit: int) -> list[dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            return self._load_decisions_canonical(conn, since_ts=float(since_ts), limit=max(1, int(limit)))
        finally:
            conn.close()

    def _load_decisions_canonical(self, conn: Any, *, since_ts: float, limit: int) -> list[dict[str, Any]]:
        """Decision reads through canonical_v2 (legacy-shaped rows)."""
        has_factor_snapshot = state_table_exists(conn, "decision_factor_snapshot")
        decisions: list[dict[str, Any]] = []
        for row in iter_decision_rows(conn, limit=0):
            if _safe_float(row.get("decision_ts")) < since_ts:
                continue
            if str(row.get("event_type") or "") not in self._DECISION_EVENT_TYPES:
                continue
            decisions.append(row)
        decisions.sort(key=lambda item: _safe_float(item.get("decision_ts")))
        decisions = decisions[:limit]
        if decisions:
            for item in decisions:
                item["factor_snapshot_count"] = self._factor_snapshot_count(conn, str(item.get("decision_id") or ""))
                item["factor_snapshots"] = self._load_factor_snapshots(conn, str(item.get("decision_id") or ""))
        return decisions

    @staticmethod
    def _factor_snapshot_count(conn: Any, decision_id: str) -> int:
        try:
            from backend.services.canonical_v2_reader import (
                count_decision_factor_snapshots,
            )
            return count_decision_factor_snapshots(conn, decision_id)
        except Exception:
            row = _execute(
                conn,
                "SELECT COUNT(*) AS n FROM decision_factor_snapshot WHERE decision_id = ?",
                (decision_id,),
            ).fetchone()
            return _safe_int(row["n"] if row is not None else 0)

    @staticmethod
    def _load_factor_snapshots(conn: Any, decision_id: str) -> list[dict[str, Any]]:
        try:
            from backend.services.canonical_v2_reader import (
                iter_decision_factor_snapshots,
            )
            return iter_decision_factor_snapshots(conn, decision_id)
        except Exception:
            rows = _execute(
                conn,
                "SELECT factor, source, raw_value, normalized_value, direction,"
                " base_weight, policy_weight, shadow_score, health_score,"
                " gated, gated_reason, contribution_score,"
                " generation, artifact_hash, definition_fingerprint,"
                " runtime_selection_fingerprint, config_hash,"
                " lineage_status"
                " FROM decision_factor_snapshot"
                " WHERE decision_id = ?"
                " ORDER BY ABS(contribution_score) DESC, factor ASC",
                (decision_id,),
            ).fetchall()
            return [dict(row) for row in rows]

    def _load_preview_decisions(self, *, since_ts: float, limit: int, decision_id: str = "") -> list[dict[str, Any]]:
        selected_decision_id = str(decision_id or "").strip()
        if selected_decision_id:
            row = self._load_decision_by_id(selected_decision_id)
            return [row] if row else []
        return self._load_preview_decision_candidates(since_ts=since_ts, limit=limit, offset=0)

    def _load_decision_by_id(self, decision_id: str) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            row = decision_row(conn, str(decision_id))
            if row is None or str(row.get("event_type") or "") not in self._DECISION_EVENT_TYPES:
                return {}
            row = dict(row)
            row["factor_snapshot_count"] = 0
            return row
        finally:
            conn.close()

    def _load_preview_decision_candidates(self, *, since_ts: float, limit: int, offset: int = 0) -> list[dict[str, Any]]:
        conn = _connect(self.db_path, read_only=True)
        try:
            fetch_limit = max(50, (max(0, int(offset)) + max(1, int(limit))) * 5)
            candidates: list[dict[str, Any]] = []
            for row in iter_decision_rows(conn, limit=0):
                if _safe_float(row.get("decision_ts")) < float(since_ts):
                    continue
                if str(row.get("event_type") or "") not in self._DECISION_EVENT_TYPES:
                    continue
                row = dict(row)
                row["factor_snapshot_count"] = 0
                candidates.append(row)
            candidates.sort(key=lambda item: _safe_float(item.get("decision_ts")), reverse=True)
            decisions = candidates[:fetch_limit]
            ranked = sorted(decisions, key=self._preview_decision_rank)
            start = max(0, int(offset))
            return ranked[start : start + max(1, int(limit))]
        finally:
            conn.close()

    @staticmethod
    def _preview_decision_rank(item: dict[str, Any]) -> tuple[int, float]:
        event_type = str(item.get("event_type") or "")
        has_trade_ref = bool(str(item.get("trade_id") or "") or str(item.get("position_id") or ""))
        if event_type == "open" and has_trade_ref:
            priority = 0
        elif event_type == "open":
            priority = 1
        elif event_type == "order_failed":
            priority = 2
        else:
            priority = 3
        return (priority, -_safe_float(item.get("decision_ts")))

    def _load_bar_window(
        self,
        *,
        symbol: str,
        timeframe: str,
        decision_ts: float,
        warmup_bars: int,
        post_bars: int,
    ) -> list[dict[str, Any]]:
        from data.store import DataStore

        tf_sec = _timeframe_seconds(timeframe)
        start_ts = float(decision_ts) - float(tf_sec * (max(1, int(warmup_bars)) + 2))
        end_ts = float(decision_ts) + float(tf_sec * max(0, int(post_bars)))
        df = DataStore().load_bars(symbol, timeframe, start=start_ts, end=end_ts, limit=max(1, int(warmup_bars) + int(post_bars) + 4))
        if df is None or getattr(df, "empty", True):
            return []
        records: list[dict[str, Any]] = []
        frame = df.reset_index(drop=True) if hasattr(df, "reset_index") else df
        for item in frame.to_dict(orient="records"):
            records.append(_bar_record(dict(item)))
        return sorted(records, key=lambda x: _safe_float(x.get("time")))

    def _evaluate_bar_windows(self, rows: list[dict[str, Any]], *, warmup_bars: int, post_bars: int) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        windows: list[dict[str, Any]] = []
        aligned = 0
        missing = 0
        stale = 0
        target_count = max(1, int(warmup_bars) + int(post_bars))
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            symbol = str(row.get("symbol") or "XAUUSD+")
            timeframe = str(row.get("timeframe") or "M5")
            decision_ts = _safe_float(row.get("decision_ts"))
            tf_sec = _timeframe_seconds(timeframe)
            bars = self._load_bar_window(
                symbol=symbol,
                timeframe=timeframe,
                decision_ts=decision_ts,
                warmup_bars=warmup_bars,
                post_bars=post_bars,
            )
            before = [bar for bar in bars if _safe_float(bar.get("time")) <= decision_ts]
            after = [bar for bar in bars if _safe_float(bar.get("time")) > decision_ts]
            issues: list[str] = []
            aligned_bar: dict[str, Any] = {}
            alignment_age = 0.0
            if not bars:
                missing += 1
                issues.append("missing_bar_window")
            elif not before:
                missing += 1
                issues.append("no_bar_before_decision")
            else:
                aligned_bar = before[-1]
                alignment_age = max(0.0, decision_ts - _safe_float(aligned_bar.get("time")))
                if alignment_age > tf_sec * 2.0:
                    stale += 1
                    issues.append("stale_bar_alignment")
                else:
                    aligned += 1
            if len(bars) < min(target_count, max(1, int(warmup_bars))):
                issues.append("short_bar_window")
            if issues and len(examples) < 50:
                examples.append(
                    {
                        "decision_id": decision_id,
                        "symbol": symbol,
                        "timeframe": timeframe,
                        "decision_ts": decision_ts,
                        "bar_count": len(bars),
                        "before_count": len(before),
                        "after_count": len(after),
                        "alignment_age_seconds": round(alignment_age, 3),
                        "issues": issues,
                    }
                )
            fingerprints.append(
                {
                    "decision_id": decision_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "decision_ts": round(decision_ts, 3),
                    "bar_count": len(bars),
                    "aligned_bar": aligned_bar,
                    "window_hash": _hash(bars),
                }
            )
            windows.append(
                {
                    "decision_id": decision_id,
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "decision_ts": decision_ts,
                    "bars": bars,
                    "issues": issues,
                }
            )
        decision_count = len(rows)
        mismatch_count = decision_count - aligned
        coverage = aligned / decision_count if decision_count else 0.0
        metrics = {
            "schema_version": "bar_replay_metrics.v1",
            "mode": "decision_bar_window",
            "warmup_bars": int(warmup_bars),
            "post_bars": int(post_bars),
            "decision_count": decision_count,
            "aligned_decision_count": aligned,
            "missing_bar_window_count": missing,
            "stale_bar_alignment_count": stale,
            "bar_window_mismatch_count": mismatch_count,
            "bar_window_coverage": round(coverage, 6),
            "bar_window_hash": _hash(fingerprints),
            "mismatch_examples": examples,
            "next_step": "extend_order_lifecycle_broker_and_supervisor_replay",
        }
        return {"metrics": metrics, "fingerprints": fingerprints, "windows": windows}

    @staticmethod
    def _bar_window_preview(windows: list[dict[str, Any]], *, max_windows: int = 3, max_bars: int = 80) -> dict[str, Any]:
        preview: list[dict[str, Any]] = []
        for window in windows[: max(1, int(max_windows))]:
            bars = list(window.get("bars") or [])
            clean_bars = []
            for bar in bars[-max(1, int(max_bars)):]:
                record = _bar_record(dict(bar))
                if record["time"] > 0 and record["high"] >= record["low"]:
                    clean_bars.append(record)
            preview.append(
                {
                    "decision_id": str(window.get("decision_id") or ""),
                    "symbol": str(window.get("symbol") or "XAUUSD+"),
                    "timeframe": str(window.get("timeframe") or "M5"),
                    "decision_ts": round(_safe_float(window.get("decision_ts")), 3),
                    "bar_count": len(bars),
                    "shown_bar_count": len(clean_bars),
                    "issues": list(window.get("issues") or []),
                    "bars": clean_bars,
                }
            )
        return {
            "schema_version": "bar_window_preview.v1",
            "window_count": len(preview),
            "max_windows": max_windows,
            "max_bars": max_bars,
            "windows": preview,
        }

    def _trade_outcome_learning_preview(self, rows: list[dict[str, Any]], *, max_items: int = 3) -> dict[str, Any]:
        conn = _connect(self.db_path, read_only=True)
        try:
            has_reviews = state_table_exists(conn, "trade_outcome_review")
            has_lifecycle = state_table_exists(conn, "position_lifecycle_event")
            has_recovery = state_table_exists(conn, "recovery_position_state")
            has_samples = True  # canonical training_sample_row is the only sample store
            items = [
                self._trade_outcome_learning_item(
                    conn,
                    row,
                    has_reviews=has_reviews,
                    has_lifecycle=has_lifecycle,
                    has_recovery=has_recovery,
                    has_samples=has_samples,
                )
                for row in rows[: max(1, int(max_items))]
            ]
            closed_count = sum(1 for item in items if str(item.get("outcome", {}).get("status") or "") == "closed")
            sample_count = sum(_safe_int(item.get("learning", {}).get("sample_count")) for item in items)
            trainable_count = sum(1 for item in items if str(item.get("learning", {}).get("status") or "") == "learning_sample_ready")
            return {
                "schema_version": "trade_outcome_learning_preview.v1",
                "item_count": len(items),
                "closed_count": closed_count,
                "sample_count": sample_count,
                "trainable_count": trainable_count,
                "items": items,
            }
        finally:
            conn.close()

    def _trade_outcome_learning_item(
        self,
        conn,
        row: dict[str, Any],
        *,
        has_reviews: bool,
        has_lifecycle: bool,
        has_recovery: bool,
        has_samples: bool,
    ) -> dict[str, Any]:
        decision_id = str(row.get("decision_id") or "")
        trade_id = str(row.get("trade_id") or "")
        position_id = str(row.get("position_id") or "")
        event_type = str(row.get("event_type") or "")
        direction = self._decision_direction(row)
        review = self._find_trade_review(conn, decision_id=decision_id, trade_id=trade_id, position_id=position_id) if has_reviews else {}
        lifecycle = self._find_latest_position_event(conn, trade_id=trade_id, position_id=position_id) if has_lifecycle else {}
        recovery = self._find_recovery_position_state(conn, position_id=position_id) if has_recovery else {}
        samples = self._find_learning_samples(conn, decision_id=decision_id, trade_id=trade_id, position_id=position_id) if has_samples else []
        review_json = _loads(review.get("review_json"), {}) if review else {}
        lifecycle_json = _loads(lifecycle.get("details_json"), {}) if lifecycle else {}
        lifecycle_event_type = str(lifecycle.get("event_type") or "").lower()
        lifecycle_is_closed = lifecycle_event_type in {"closed", "broker_closed", "supervisor_closed"}
        recovery_status = str(recovery.get("status") or "").lower()
        recovery_close_ts = _safe_float(recovery.get("closed_at"))
        recovery_is_closed = recovery_status in {"closed", "broker_closed", "supervisor_closed"} or recovery_close_ts > 0
        if review:
            pnl = _safe_float(review.get("pnl"), _safe_float(lifecycle.get("realized_pnl")))
        elif lifecycle_is_closed:
            pnl = _safe_float(lifecycle.get("realized_pnl"))
        elif recovery_is_closed:
            pnl = _safe_float(recovery.get("close_pnl"))
        else:
            pnl = _safe_float(lifecycle.get("realized_pnl"))
        has_trade_ref = bool(trade_id or position_id)
        if not has_trade_ref and event_type != "open":
            outcome_status = "no_trade"
            outcome_result = "not_applicable"
        elif review or lifecycle_is_closed or recovery_is_closed:
            outcome_status = "closed"
            outcome_result = "profit" if pnl > 0 else "loss" if pnl < 0 else "flat"
        elif has_trade_ref:
            outcome_status = "open"
            outcome_result = "pending"
        else:
            outcome_status = "missing"
            outcome_result = "unknown"

        matured_samples = [
            item for item in samples
            if (
                str(item.get("label_status") or "") == "matured"
                and str(item.get("integrity") or "") == "full"
                and not bool(item.get("system_contaminated"))
                and bool(item.get("governance_eligible"))
                and _safe_float(item.get("governance_effective_weight")) > 0
            )
        ]
        if outcome_status == "no_trade":
            learning_status = "not_applicable_no_trade"
            learning_summary = "这次是跳过信号，不进入交易盈亏学习"
        elif outcome_status not in {"closed"}:
            learning_status = "awaiting_outcome"
            learning_summary = "等待平仓和收益归因后再生成学习样本"
        elif matured_samples:
            learning_status = "learning_sample_ready"
            learning_summary = "已有成熟样本，可参与后续训练/治理评估"
        elif samples:
            learning_status = "learning_sample_observe"
            learning_summary = "已有样本但暂未达到强训练条件，先观察"
        else:
            learning_status = "awaiting_learning_sample"
            learning_summary = "已完成收益归因，等待学习样本物化任务补入"

        latest_sample = samples[0] if samples else {}
        close_reason = str(
            review_json.get("close_reason")
            or lifecycle_json.get("close_reason")
            or lifecycle_json.get("real_pnl", {}).get("source")
            or recovery.get("close_reason")
            or ""
        )
        summary_text = str(review.get("summary_text") or "")
        close_ts = _safe_float(review_json.get("close_ts"))
        if close_ts <= 0 and lifecycle_is_closed:
            close_ts = _safe_float(lifecycle.get("event_ts"))
        if close_ts <= 0 and recovery_is_closed:
            close_ts = recovery_close_ts
        return {
            "decision_id": decision_id,
            "trade_id": trade_id,
            "position_id": position_id,
            "event_type": event_type,
            "symbol": str(row.get("symbol") or ""),
            "timeframe": str(row.get("timeframe") or ""),
            "direction": direction,
            "direction_label": self._direction_label(direction),
            "decision_ts": round(_safe_float(row.get("decision_ts")), 3),
            "outcome": {
                "status": outcome_status,
                "result": outcome_result,
                "pnl": round(pnl, 6),
                "outcome_label": str(review.get("outcome_label") or ""),
                "review_id": str(review.get("review_id") or ""),
                "exit_decision_id": str(review.get("exit_decision_id") or ""),
                "close_ts": round(close_ts, 3),
                "close_reason": close_reason,
                "summary": summary_text,
                "primary_factor": str(review_json.get("primary_factor") or self._summary_token(summary_text, "primary_factor")),
                "worst_factor": str(review_json.get("worst_factor") or self._summary_token(summary_text, "worst_factor")),
            },
            "learning": {
                "status": learning_status,
                "summary": learning_summary,
                "sample_count": len(samples),
                "matured_sample_count": len(matured_samples),
                "latest_sample_id": str(latest_sample.get("sample_id") or ""),
                "latest_sample_type": str(latest_sample.get("sample_type") or ""),
                "latest_label_status": str(latest_sample.get("label_status") or ""),
                "latest_integrity": str(latest_sample.get("integrity") or ""),
                "latest_train_weight": round(_safe_float(latest_sample.get("train_weight")), 6) if latest_sample else 0.0,
            },
        }

    def _find_trade_review(self, conn, *, decision_id: str, trade_id: str, position_id: str) -> dict[str, Any]:
        best: dict[str, Any] | None = None
        best_ts = 0.0
        for row in iter_review_rows(conn, limit=0):
            matched = (
                bool(decision_id)
                and (
                    str(row.get("entry_decision_id") or "") == decision_id
                    or str(row.get("exit_decision_id") or "") == decision_id
                )
            ) or (bool(trade_id) and str(row.get("trade_id") or "") == trade_id) or (
                bool(position_id) and str(row.get("position_id") or "") == position_id
            )
            if not matched:
                continue
            row_ts = _safe_float(row.get("created_at"))
            if best is None or row_ts > best_ts:
                best = row
                best_ts = row_ts
        return best or {}

    def _find_latest_position_event(self, conn, *, trade_id: str, position_id: str) -> dict[str, Any]:
        best: dict[str, Any] | None = None
        best_ts = 0.0
        for row in iter_position_rows(conn, limit=0):
            if (bool(position_id) and str(row.get("position_id") or "") == position_id) or (
                bool(trade_id) and str(row.get("trade_id") or "") == trade_id
            ):
                row_ts = _safe_float(row.get("event_ts"))
                if best is None or row_ts > best_ts:
                    best = row
                    best_ts = row_ts
        return best or {}

    def _find_recovery_position_state(self, conn, *, position_id: str) -> dict[str, Any]:
        if not position_id:
            return {}
        key: Any = int(position_id) if str(position_id).strip().isdigit() else position_id
        row = _execute(
            conn,
            """
            SELECT *
            FROM recovery_position_state
            WHERE position_id = ?
            LIMIT 1
            """,
            (key,),
        ).fetchone()
        return dict(row) if row else {}

    def _find_learning_samples(self, conn, *, decision_id: str, trade_id: str, position_id: str) -> list[dict[str, Any]]:
        from backend.services.canonical_v2_reader import iter_training_sample_rows

        merged: dict[str, dict[str, Any]] = {}
        for key, value in (
            ("decision_id", decision_id),
            ("trade_id", trade_id),
            ("position_id", position_id),
        ):
            if not value:
                continue
            for row in iter_training_sample_rows(conn, **{key: value}, limit=10):
                merged.setdefault(str(row.get("sample_id") or f"#{id(row)}"), row)
        ordered = sorted(
            merged.values(),
            key=lambda r: _safe_float(r.get("created_at")),
            reverse=True,
        )[:10]
        return ordered

    @staticmethod
    def _summary_token(summary: str, key: str) -> str:
        marker = f"{key}="
        if marker not in summary:
            return ""
        tail = summary.split(marker, 1)[1]
        return tail.split(";", 1)[0].split(",", 1)[0].strip()

    @staticmethod
    def _direction_label(direction: int) -> str:
        if direction > 0:
            return "direction_long"
        if direction < 0:
            return "direction_short"
        return "direction_flat"

    @staticmethod
    def _decision_direction(row: dict[str, Any]) -> int:
        action = _loads(row.get("action_json"), {})
        if isinstance(action, dict):
            raw = action.get("direction")
            direction = _safe_int(raw, 0)
            if direction:
                return 1 if direction > 0 else -1
        score = _safe_float(row.get("action_score"))
        if score > 0:
            return 1
        if score < 0:
            return -1
        return 0

    def _enrich_bar_window(self, bars: list[dict[str, Any]], *, decision_ts: float):
        import pandas as pd
        from data.factor_frame import FactorFrameBuilder

        frame = pd.DataFrame(bars)
        if frame.empty:
            return frame
        return FactorFrameBuilder(cache_ttl_sec=0.0).enrich_bars(frame, as_of=decision_ts)

    def _evaluate_factor_frames(self, windows: list[dict[str, Any]]) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        ok = 0
        empty = 0
        errors = 0
        column_counts: list[int] = []
        for item in windows:
            decision_id = str(item.get("decision_id") or "")
            bars = item.get("bars") if isinstance(item.get("bars"), list) else []
            decision_ts = _safe_float(item.get("decision_ts"))
            if not bars:
                empty += 1
                if len(examples) < 50:
                    examples.append({"decision_id": decision_id, "issues": ["missing_bar_window"]})
                fingerprints.append({"decision_id": decision_id, "frame_hash": "", "row_count": 0, "column_count": 0})
                continue
            try:
                frame = self._enrich_bar_window(bars, decision_ts=decision_ts)
                if frame is None or getattr(frame, "empty", True):
                    empty += 1
                    if len(examples) < 50:
                        examples.append({"decision_id": decision_id, "issues": ["empty_factor_frame"]})
                    fingerprints.append({"decision_id": decision_id, "frame_hash": "", "row_count": 0, "column_count": 0})
                    continue
                fingerprint = self._factor_frame_fingerprint(decision_id, frame)
                column_counts.append(_safe_int(fingerprint.get("column_count")))
                ok += 1
                fingerprints.append(fingerprint)
            except Exception as exc:
                errors += 1
                if len(examples) < 50:
                    examples.append(
                        {
                            "decision_id": decision_id,
                            "issues": ["factor_frame_error"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                fingerprints.append({"decision_id": decision_id, "frame_hash": "", "row_count": 0, "column_count": 0})
        decision_count = len(windows)
        mismatch_count = decision_count - ok
        coverage = ok / decision_count if decision_count else 0.0
        metrics = {
            "schema_version": "factor_frame_replay_metrics.v1",
            "mode": "factor_frame_enrich_bars_on_replay_windows",
            "read_only": True,
            "decision_count": decision_count,
            "factor_frame_ok_count": ok,
            "factor_frame_empty_count": empty,
            "factor_frame_error_count": errors,
            "factor_frame_mismatch_count": mismatch_count,
            "factor_frame_coverage": round(coverage, 6),
            "column_count_min": min(column_counts) if column_counts else 0,
            "column_count_max": max(column_counts) if column_counts else 0,
            "factor_frame_hash": _hash(fingerprints),
            "mismatch_examples": examples,
            "next_step": "extend_order_lifecycle_broker_and_supervisor_replay",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_gate_risk_recompute(self, rows: list[dict[str, Any]], windows: list[dict[str, Any]]) -> dict[str, Any]:
        window_by_decision = {str(item.get("decision_id") or ""): item for item in windows}
        gate_eval = self._evaluate_execution_gate_recompute(rows, window_by_decision)
        risk_eval = self._evaluate_risk_policy_recompute(rows)
        return {
            "gate_metrics": gate_eval["metrics"],
            "risk_metrics": risk_eval["metrics"],
        }

    def _evaluate_lifecycle_replay(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        evidence = self._load_lifecycle_evidence(rows)
        order_eval = self._evaluate_order_lifecycle(rows, evidence)
        order_outcome_eval = self._evaluate_order_outcome_causality(rows, evidence)
        slippage_eval = self._evaluate_broker_fill_slippage(rows, evidence)
        position_eval = self._evaluate_position_lifecycle(rows, evidence)
        supervisor_eval = self._evaluate_supervisor_actions(rows, evidence)
        counterfactual_eval = self._evaluate_supervisor_counterfactuals(rows, evidence)
        risk_subaction_eval = self._evaluate_risk_policy_subactions(rows, evidence)
        return {
            "order_metrics": order_eval["metrics"],
            "order_outcome_metrics": order_outcome_eval["metrics"],
            "slippage_metrics": slippage_eval["metrics"],
            "position_metrics": position_eval["metrics"],
            "supervisor_metrics": supervisor_eval["metrics"],
            "counterfactual_metrics": counterfactual_eval["metrics"],
            "risk_subaction_metrics": risk_subaction_eval["metrics"],
        }

    def _load_lifecycle_evidence(self, rows: list[dict[str, Any]]) -> dict[str, dict[str, list[dict[str, Any]]]]:
        empty = {"orders": {}, "positions": {}, "supervisor": {}, "deals": {}, "counterfactuals": {}}
        if not rows:
            return empty
        conn = _connect(self.db_path, read_only=True)
        try:
            has_orders = state_table_exists(conn, "order_lifecycle_event") or True
            has_positions = state_table_exists(conn, "position_lifecycle_event") or True
            has_supervisor = state_table_exists(conn, "position_supervisor_trace")
            has_deals = state_table_exists(conn, "ctrader_deals")
            has_counterfactuals = state_table_exists(conn, "supervisor_counterfactual_review")
            orders: dict[str, list[dict[str, Any]]] = {}
            positions: dict[str, list[dict[str, Any]]] = {}
            supervisor: dict[str, list[dict[str, Any]]] = {}
            deals: dict[str, list[dict[str, Any]]] = {}
            counterfactuals: dict[str, list[dict[str, Any]]] = {}
            # Order/position facts are canonical-owned; batch-restore once per replay
            # run, then filter per decision in memory.
            canonical_orders = iter_order_rows(conn, limit=0)
            canonical_positions = iter_position_rows(conn, limit=0)
            for row in rows:
                decision_id = str(row.get("decision_id") or "")
                trade_id = str(row.get("trade_id") or "")
                position_id = str(row.get("position_id") or "")
                numeric_position_id = _safe_int(position_id)
                if has_orders:
                    orders[decision_id] = [
                        item
                        for item in canonical_orders
                        if str(item.get("decision_id") or "") == decision_id
                        or (trade_id and str(item.get("trade_id") or "") == trade_id)
                    ]
                if has_positions:
                    positions[decision_id] = [
                        item
                        for item in canonical_positions
                        if (position_id and str(item.get("position_id") or "") == position_id)
                        or (trade_id and str(item.get("trade_id") or "") == trade_id)
                    ]
                if has_supervisor:
                    try:
                        has_trace_archive = "verdict_archive_hash" in state_table_columns(
                            conn, "position_supervisor_trace"
                        )
                    except Exception:
                        has_trace_archive = False
                    trace_archive_select = ", verdict_archive_hash" if has_trace_archive else ""
                    supervisor_rows = _execute(
                        conn,
                        f"""
                        SELECT trace_id, decision_id, position_id, trade_id, symbol,
                               timeframe, tick, event_ts, action, summary_reason,
                               confidence, template_id, template_version, stage,
                               outcome, risk_action, risk_allowed, risk_reason,
                               execution_status, execution_reason, context_json,
                               verdict_json, risk_verdict_json, execution_json,
                               trace_integrity, config_hash, evolution_run_id,
                               created_at{trace_archive_select}
                        FROM position_supervisor_trace
                        WHERE decision_id = ?
                           OR (? <> '' AND position_id = ?)
                           OR (? <> '' AND trade_id = ?)
                        ORDER BY event_ts ASC, trace_id ASC
                        """,
                        (decision_id, position_id, position_id, trade_id, trade_id),
                    ).fetchall()
                    supervisor_items = []
                    for item in supervisor_rows:
                        value = dict(item)
                        archive_hash = str(value.get("verdict_archive_hash") or "")
                        if archive_hash:
                            value.update(load_supervisor_trace_archive(conn, archive_hash))
                        supervisor_items.append(value)
                    supervisor[decision_id] = supervisor_items
                if has_deals and numeric_position_id > 0:
                    deal_rows = _execute(
                        conn,
                        """
                        SELECT deal_id, position_id, order_id, symbol_id, volume,
                               filled_volume, exec_price, trade_side, deal_status,
                               exec_timestamp, commission, entry_price, gross_profit,
                               swap, close_commission, balance, closed_volume,
                               is_close, fetched_at
                        FROM ctrader_deals
                        WHERE position_id = ?
                        ORDER BY exec_timestamp ASC, deal_id ASC
                        """,
                        (numeric_position_id,),
                    ).fetchall()
                    deals[decision_id] = [dict(item) for item in deal_rows]
                if has_counterfactuals:
                    valid_counterfactuals = []
                    cf_rows = _execute(
                        conn,
                        """
                        SELECT c.counterfactual_id, c.review_id, c.trade_id,
                               c.position_id, c.close_ts, c.close_reason,
                               c.supervisor_event_type, c.supervisor_reason,
                               c.label, c.confidence, c.horizons_json,
                               c.evidence_json, c.created_at, c.updated_at
                        FROM supervisor_counterfactual_review c
                        WHERE (? <> '' AND c.position_id = ?)
                           OR (? <> '' AND c.trade_id = ?)
                        ORDER BY c.close_ts ASC, c.counterfactual_id ASC
                        """,
                        (position_id, position_id, trade_id, trade_id),
                    ).fetchall()
                    for item in cf_rows:
                        value = dict(item)
                        evidence = _loads(value.get("evidence_json"), {})
                        source_review = review_row(conn, str(value.get("review_id") or ""))
                        if (
                            not str(value.get("review_id") or "")
                            or source_review is None
                            or review_has_system_contamination(source_review.get("review_json") or {})
                            or bool(evidence.get("evidence_invalidated"))
                        ):
                            continue
                        valid_counterfactuals.append(value)
                    counterfactuals[decision_id] = valid_counterfactuals
            return {
                "orders": orders,
                "positions": positions,
                "supervisor": supervisor,
                "deals": deals,
                "counterfactuals": counterfactuals,
            }
        finally:
            conn.close()

    def _evaluate_order_lifecycle(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        expected_decisions = 0
        covered_decisions = 0
        missing_expected = 0
        unexpected = 0
        submitted_count = 0
        filled_count = 0
        order_failed_count = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            event_type = str(row.get("event_type") or "")
            action_reason = str(row.get("action_reason") or "")
            action = _loads(row.get("action_json"), {})
            events = evidence.get("orders", {}).get(decision_id, [])
            event_types = {str(item.get("event_type") or "") for item in events}
            submitted_count += sum(1 for item in events if str(item.get("event_type") or "") == "submitted")
            filled_count += sum(1 for item in events if str(item.get("event_type") or "") == "filled")
            order_failed_count += sum(1 for item in events if str(item.get("event_type") or "") == "order_failed")
            expected: set[str] = set()
            if event_type == "open" and action_reason == "executed":
                expected = {"submitted", "filled"}
            elif event_type == "order_failed" or str(action.get("skip_stage") or "") == "broker_order_failed":
                expected = {"order_failed"}
            missing = sorted(expected - event_types)
            issues: list[str] = []
            if expected:
                expected_decisions += 1
                if missing:
                    missing_expected += 1
                    issues.append("missing_expected_order_event")
                else:
                    covered_decisions += 1
            elif events and event_type == "skip":
                unexpected += 1
                issues.append("unexpected_order_event_for_skip")
            if issues and len(examples) < 50:
                examples.append(
                    {
                        "decision_id": decision_id,
                        "event_type": event_type,
                        "action_reason": action_reason,
                        "expected": sorted(expected),
                        "observed": sorted(event_types),
                        "missing": missing,
                        "issues": issues,
                    }
                )
            fingerprints.append(
                {
                    "decision_id": decision_id,
                    "expected": sorted(expected),
                    "observed": sorted(event_types),
                    "event_count": len(events),
                    "issues": issues,
                }
            )
        coverage = covered_decisions / expected_decisions if expected_decisions else 0.0
        metrics = {
            "schema_version": "order_lifecycle_replay_metrics.v1",
            "mode": "ledger_order_event_alignment",
            "read_only": True,
            "decision_count": len(rows),
            "expected_order_decision_count": expected_decisions,
            "covered_order_decision_count": covered_decisions,
            "missing_expected_event_count": missing_expected,
            "unexpected_order_event_count": unexpected,
            "submitted_event_count": submitted_count,
            "filled_event_count": filled_count,
            "order_failed_event_count": order_failed_count,
            "coverage": round(coverage, 6),
            "mismatch_examples": examples,
            "lifecycle_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads order_lifecycle_event only; does not replay broker execution",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_order_outcome_causality(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        expected_open_count = 0
        complete_chain_count = 0
        causality_issues = 0
        broker_deal_link_count = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            event_type = str(row.get("event_type") or "")
            action_reason = str(row.get("action_reason") or "")
            if not (event_type == "open" and action_reason == "executed"):
                continue
            expected_open_count += 1
            orders = evidence.get("orders", {}).get(decision_id, [])
            positions = evidence.get("positions", {}).get(decision_id, [])
            deals = evidence.get("deals", {}).get(decision_id, [])
            submitted = self._first_event(orders, "submitted")
            filled = self._first_event(orders, "filled")
            opened = self._first_event(positions, "opened")
            open_deal = next((deal for deal in deals if not _safe_int(deal.get("is_close"))), {})
            if open_deal:
                broker_deal_link_count += 1
            issues: list[str] = []
            if not submitted:
                issues.append("missing_submitted_event")
            if not filled:
                issues.append("missing_filled_event")
            if not opened:
                issues.append("missing_opened_position_event")
            if submitted and filled and _safe_float(filled.get("event_ts")) < _safe_float(submitted.get("event_ts")):
                issues.append("filled_before_submitted")
            if filled and opened and _safe_float(opened.get("event_ts")) < _safe_float(filled.get("event_ts")):
                issues.append("position_opened_before_fill")
            if not issues:
                complete_chain_count += 1
            else:
                causality_issues += 1
                if len(examples) < 50:
                    examples.append(
                        {
                            "decision_id": decision_id,
                            "position_id": str(row.get("position_id") or ""),
                            "issues": issues,
                            "has_broker_deal": bool(open_deal),
                            "timestamps": {
                                "submitted": _safe_float(submitted.get("event_ts")) if submitted else 0.0,
                                "filled": _safe_float(filled.get("event_ts")) if filled else 0.0,
                                "opened": _safe_float(opened.get("event_ts")) if opened else 0.0,
                                "broker_deal": _safe_float(open_deal.get("exec_timestamp")) if open_deal else 0.0,
                            },
                        }
                    )
            fingerprints.append(
                {
                    "decision_id": decision_id,
                    "has_submitted": bool(submitted),
                    "has_filled": bool(filled),
                    "has_opened": bool(opened),
                    "has_open_deal": bool(open_deal),
                    "issues": issues,
                }
            )
        coverage = complete_chain_count / expected_open_count if expected_open_count else 0.0
        deal_coverage = broker_deal_link_count / expected_open_count if expected_open_count else 0.0
        metrics = {
            "schema_version": "order_outcome_causality_metrics.v1",
            "mode": "decision_to_order_position_broker_chain",
            "read_only": True,
            "decision_count": len(rows),
            "expected_open_count": expected_open_count,
            "complete_chain_count": complete_chain_count,
            "causality_issue_count": causality_issues,
            "broker_deal_link_count": broker_deal_link_count,
            "causality_coverage": round(coverage, 6),
            "broker_deal_coverage": round(deal_coverage, 6),
            "issue_examples": examples,
            "causality_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads ledger/lifecycle/deal facts only; does not replay broker execution",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_broker_fill_slippage(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        slippages: list[float] = []
        adverse_count = 0
        missing_reference = 0
        deal_price_match_count = 0
        filled_count = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            action = _loads(row.get("action_json"), {})
            direction = _safe_int(action.get("direction"), 1 if _safe_float(row.get("action_score")) > 0 else -1)
            orders = evidence.get("orders", {}).get(decision_id, [])
            deals = evidence.get("deals", {}).get(decision_id, [])
            filled = [item for item in orders if str(item.get("event_type") or "") == "filled"]
            submitted = self._first_event(orders, "submitted")
            for fill in filled:
                filled_count += 1
                fill_price = _safe_float(fill.get("price"))
                ref_price = self._slippage_reference_price(row, action, submitted)
                open_deal = self._nearest_open_deal(deals, fill)
                if open_deal and abs(_safe_float(open_deal.get("exec_price")) - fill_price) <= 1e-6:
                    deal_price_match_count += 1
                issues: list[str] = []
                if fill_price <= 0 or ref_price <= 0:
                    missing_reference += 1
                    issues.append("missing_fill_or_reference_price")
                    slippage_points = 0.0
                    adverse_points = 0.0
                else:
                    slippage_points = fill_price - ref_price
                    adverse_points = slippage_points * (1 if direction >= 0 else -1)
                    slippages.append(abs(slippage_points))
                    if adverse_points > 0:
                        adverse_count += 1
                if (issues or abs(slippage_points) > 0) and len(examples) < 50:
                    examples.append(
                        {
                            "decision_id": decision_id,
                            "fill_event_id": str(fill.get("event_id") or ""),
                            "reference_price": round(ref_price, 6),
                            "fill_price": round(fill_price, 6),
                            "broker_deal_price": round(_safe_float(open_deal.get("exec_price")) if open_deal else 0.0, 6),
                            "slippage_points": round(slippage_points, 6),
                            "adverse_points": round(adverse_points, 6),
                            "issues": issues,
                        }
                    )
                fingerprints.append(
                    {
                        "decision_id": decision_id,
                        "fill_event_id": str(fill.get("event_id") or ""),
                        "reference_price": round(ref_price, 6),
                        "fill_price": round(fill_price, 6),
                        "broker_deal_id": _safe_int(open_deal.get("deal_id")) if open_deal else 0,
                        "slippage_points": round(slippage_points, 6),
                        "adverse_points": round(adverse_points, 6),
                        "issues": issues,
                    }
                )
        avg_abs = sum(slippages) / len(slippages) if slippages else 0.0
        max_abs = max(slippages) if slippages else 0.0
        metrics = {
            "schema_version": "broker_fill_slippage_metrics.v1",
            "mode": "order_fill_reference_price_alignment",
            "read_only": True,
            "decision_count": len(rows),
            "filled_event_count": filled_count,
            "measured_fill_count": len(slippages),
            "missing_reference_count": missing_reference,
            "adverse_slippage_count": adverse_count,
            "broker_deal_price_match_count": deal_price_match_count,
            "avg_abs_slippage_points": round(avg_abs, 6),
            "max_abs_slippage_points": round(max_abs, 6),
            "examples": examples,
            "slippage_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads order_lifecycle_event and ctrader_deals only; does not feed circuit breaker",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_position_lifecycle(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        expected_decisions = 0
        covered_decisions = 0
        missing_expected = 0
        opened_count = 0
        amend_failed_count = 0
        closed_count = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            event_type = str(row.get("event_type") or "")
            action_reason = str(row.get("action_reason") or "")
            events = evidence.get("positions", {}).get(decision_id, [])
            event_types = {str(item.get("event_type") or "") for item in events}
            opened_count += sum(1 for item in events if str(item.get("event_type") or "") == "opened")
            amend_failed_count += sum(1 for item in events if str(item.get("event_type") or "") == "amend_failed")
            closed_count += sum(1 for item in events if str(item.get("event_type") or "") in {"closed", "broker_closed", "supervisor_closed"})
            expected: set[str] = set()
            if event_type == "open" and action_reason == "executed":
                expected = {"opened"}
            elif event_type == "amend_failed":
                expected = {"amend_failed"}
            missing = sorted(expected - event_types)
            issues: list[str] = []
            if expected:
                expected_decisions += 1
                if missing:
                    missing_expected += 1
                    issues.append("missing_expected_position_event")
                else:
                    covered_decisions += 1
            if issues and len(examples) < 50:
                examples.append(
                    {
                        "decision_id": decision_id,
                        "event_type": event_type,
                        "action_reason": action_reason,
                        "expected": sorted(expected),
                        "observed": sorted(event_types),
                        "missing": missing,
                        "issues": issues,
                    }
                )
            fingerprints.append(
                {
                    "decision_id": decision_id,
                    "expected": sorted(expected),
                    "observed": sorted(event_types),
                    "event_count": len(events),
                    "issues": issues,
                }
            )
        coverage = covered_decisions / expected_decisions if expected_decisions else 0.0
        metrics = {
            "schema_version": "position_lifecycle_replay_metrics.v1",
            "mode": "ledger_position_event_alignment",
            "read_only": True,
            "decision_count": len(rows),
            "expected_position_decision_count": expected_decisions,
            "covered_position_decision_count": covered_decisions,
            "missing_expected_event_count": missing_expected,
            "opened_event_count": opened_count,
            "amend_failed_event_count": amend_failed_count,
            "closed_event_count": closed_count,
            "coverage": round(coverage, 6),
            "mismatch_examples": examples,
            "lifecycle_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads position_lifecycle_event only; does not mutate positions",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_supervisor_actions(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        trace_count = 0
        risk_verdict_count = 0
        execution_status_count = 0
        integrity_issues = 0
        actions: dict[str, int] = {}
        outcomes: dict[str, int] = {}
        seen_trace_ids: set[str] = set()
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            traces = evidence.get("supervisor", {}).get(decision_id, [])
            for trace in traces:
                trace_id = str(trace.get("trace_id") or "")
                if trace_id and trace_id in seen_trace_ids:
                    continue
                if trace_id:
                    seen_trace_ids.add(trace_id)
                trace_count += 1
                action = str(trace.get("action") or "")
                outcome = str(trace.get("outcome") or "")
                if action:
                    actions[action] = actions.get(action, 0) + 1
                if outcome:
                    outcomes[outcome] = outcomes.get(outcome, 0) + 1
                risk_verdict = _loads(trace.get("risk_verdict_json"), {})
                execution = _loads(trace.get("execution_json"), {})
                status = str(trace.get("execution_status") or execution.get("status") or "")
                issues: list[str] = []
                if risk_verdict:
                    risk_verdict_count += 1
                else:
                    issues.append("missing_supervisor_risk_verdict")
                if status:
                    execution_status_count += 1
                else:
                    issues.append("missing_supervisor_execution_status")
                if str(trace.get("trace_integrity") or "full") != "full":
                    issues.append("trace_integrity_not_full")
                if issues:
                    integrity_issues += 1
                    if len(examples) < 50:
                        examples.append(
                            {
                                "trace_id": trace_id,
                                "decision_id": str(trace.get("decision_id") or decision_id),
                                "position_id": str(trace.get("position_id") or ""),
                                "action": action,
                                "outcome": outcome,
                                "issues": issues,
                            }
                        )
                fingerprints.append(
                    {
                        "trace_id": trace_id,
                        "decision_id": str(trace.get("decision_id") or decision_id),
                        "position_id": str(trace.get("position_id") or ""),
                        "action": action,
                        "outcome": outcome,
                        "risk_verdict": bool(risk_verdict),
                        "execution_status": status,
                        "trace_integrity": str(trace.get("trace_integrity") or ""),
                        "issues": issues,
                    }
                )
        risk_coverage = risk_verdict_count / trace_count if trace_count else 0.0
        execution_coverage = execution_status_count / trace_count if trace_count else 0.0
        metrics = {
            "schema_version": "supervisor_action_replay_metrics.v1",
            "mode": "position_supervisor_trace_alignment",
            "read_only": True,
            "decision_count": len(rows),
            "trace_count": trace_count,
            "risk_verdict_count": risk_verdict_count,
            "execution_status_count": execution_status_count,
            "trace_integrity_issue_count": integrity_issues,
            "risk_verdict_coverage": round(risk_coverage, 6),
            "execution_status_coverage": round(execution_coverage, 6),
            "actions": dict(sorted(actions.items())),
            "outcomes": dict(sorted(outcomes.items())),
            "integrity_examples": examples,
            "supervisor_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads position_supervisor_trace only; supervisor actions still require RiskPolicyService in live path",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_supervisor_counterfactuals(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        positions_with_supervisor = 0
        positions_with_counterfactual = 0
        counterfactual_count = 0
        confidence_sum = 0.0
        evidence_count = 0
        labels: dict[str, int] = {}
        seen_positions: set[str] = set()
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            position_id = str(row.get("position_id") or "")
            if not position_id or position_id in seen_positions:
                continue
            seen_positions.add(position_id)
            traces = evidence.get("supervisor", {}).get(decision_id, [])
            cfs = evidence.get("counterfactuals", {}).get(decision_id, [])
            actionable_traces = [
                trace
                for trace in traces
                if str(trace.get("action") or "") in {"tighten_sl", "reduce", "close", "supervisor_tighten", "supervisor_reduce", "supervisor_close"}
                or str(trace.get("risk_action") or "") in {"tighten_position", "reduce_position", "close_position"}
            ]
            if traces:
                positions_with_supervisor += 1
            if cfs:
                positions_with_counterfactual += 1
            for cf in cfs:
                counterfactual_count += 1
                label = str(cf.get("label") or "unknown")
                labels[label] = labels.get(label, 0) + 1
                confidence_sum += _safe_float(cf.get("confidence"))
                evidence_payload = _loads(cf.get("evidence_json"), {})
                if evidence_payload:
                    evidence_count += 1
                if len(examples) < 50:
                    examples.append(
                        {
                            "counterfactual_id": str(cf.get("counterfactual_id") or ""),
                            "position_id": position_id,
                            "label": label,
                            "confidence": round(_safe_float(cf.get("confidence")), 6),
                            "has_actionable_trace": bool(actionable_traces),
                            "close_reason": str(cf.get("close_reason") or ""),
                        }
                    )
                fingerprints.append(
                    {
                        "counterfactual_id": str(cf.get("counterfactual_id") or ""),
                        "position_id": position_id,
                        "label": label,
                        "confidence": round(_safe_float(cf.get("confidence")), 6),
                        "has_evidence": bool(evidence_payload),
                        "actionable_trace_count": len(actionable_traces),
                    }
                )
        coverage = positions_with_counterfactual / positions_with_supervisor if positions_with_supervisor else 0.0
        avg_confidence = confidence_sum / counterfactual_count if counterfactual_count else 0.0
        evidence_coverage = evidence_count / counterfactual_count if counterfactual_count else 0.0
        metrics = {
            "schema_version": "supervisor_counterfactual_replay_metrics.v1",
            "mode": "position_supervisor_counterfactual_evidence_alignment",
            "read_only": True,
            "decision_count": len(rows),
            "positions_with_supervisor_trace": positions_with_supervisor,
            "positions_with_counterfactual": positions_with_counterfactual,
            "counterfactual_count": counterfactual_count,
            "counterfactual_coverage": round(coverage, 6),
            "avg_confidence": round(avg_confidence, 6),
            "evidence_coverage": round(evidence_coverage, 6),
            "labels": dict(sorted(labels.items())),
            "examples": examples,
            "counterfactual_hash": _hash(fingerprints),
            "risk_policy_boundary": "reads supervisor_counterfactual_review only; does not train or switch supervisor templates",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_risk_policy_subactions(
        self,
        rows: list[dict[str, Any]],
        evidence: dict[str, dict[str, list[dict[str, Any]]]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        supported_actions = {"close_position", "reduce_position", "tighten_position"}
        attempted = 0
        agreements = 0
        disagreements = 0
        input_gaps = 0
        errors = 0
        action_counts: dict[str, int] = {}
        seen_trace_ids: set[str] = set()
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            traces = evidence.get("supervisor", {}).get(decision_id, [])
            for trace in traces:
                trace_id = str(trace.get("trace_id") or "")
                if trace_id and trace_id in seen_trace_ids:
                    continue
                if trace_id:
                    seen_trace_ids.add(trace_id)
                action = str(trace.get("risk_action") or "").strip()
                if action not in supported_actions:
                    continue
                action_counts[action] = action_counts.get(action, 0) + 1
                live_verdict = _loads(trace.get("risk_verdict_json"), {})
                context = _loads(trace.get("context_json"), {})
                if not isinstance(context, dict):
                    context = {}
                context = self._risk_subaction_context(action, trace, context)
                issues: list[str] = []
                if not live_verdict:
                    issues.append("missing_live_risk_verdict")
                if not context:
                    issues.append("missing_context")
                if issues:
                    input_gaps += 1
                    if len(gaps) < 50:
                        gaps.append({"trace_id": trace_id, "risk_action": action, "issues": issues})
                    fingerprints.append({"trace_id": trace_id, "risk_action": action, "attempted": False, "issues": issues})
                    continue
                try:
                    attempted += 1
                    from risk.policy_service import RiskPolicyService

                    recomputed = RiskPolicyService.shared().evaluate(action, context)
                    replay_verdict = recomputed.to_dict() if hasattr(recomputed, "to_dict") else dict(recomputed or {})
                    live_sig = _verdict_signature(live_verdict)
                    replay_sig = _verdict_signature(replay_verdict)
                    agreed = live_sig == replay_sig
                    if agreed:
                        agreements += 1
                    else:
                        disagreements += 1
                        if len(examples) < 50:
                            examples.append(
                                {
                                    "trace_id": trace_id,
                                    "decision_id": decision_id,
                                    "risk_action": action,
                                    "live": {"allowed": live_sig[0], "reason": live_sig[1]},
                                    "recomputed": {"allowed": replay_sig[0], "reason": replay_sig[1]},
                                }
                            )
                    fingerprints.append(
                        {
                            "trace_id": trace_id,
                            "decision_id": decision_id,
                            "risk_action": action,
                            "attempted": True,
                            "live": {"allowed": live_sig[0], "reason": live_sig[1]},
                            "recomputed": {"allowed": replay_sig[0], "reason": replay_sig[1]},
                            "agreed": agreed,
                        }
                    )
                except Exception as exc:
                    errors += 1
                    if len(examples) < 50:
                        examples.append(
                            {
                                "trace_id": trace_id,
                                "decision_id": decision_id,
                                "risk_action": action,
                                "issues": ["risk_policy_subaction_recompute_error"],
                                "error": f"{type(exc).__name__}: {exc}",
                            }
                        )
                    fingerprints.append({"trace_id": trace_id, "risk_action": action, "attempted": True, "error": f"{type(exc).__name__}: {exc}"})
        candidate_count = sum(action_counts.values())
        coverage = attempted / candidate_count if candidate_count else 0.0
        agreement_rate = agreements / attempted if attempted else 0.0
        metrics = {
            "schema_version": "risk_policy_subaction_replay_metrics.v1",
            "mode": "offline_RiskPolicyService_supervisor_subactions",
            "read_only": True,
            "decision_count": len(rows),
            "candidate_count": candidate_count,
            "attempted_count": attempted,
            "agreement_count": agreements,
            "disagreement_count": disagreements,
            "input_gap_count": input_gaps,
            "error_count": errors,
            "coverage": round(coverage, 6),
            "agreement_rate": round(agreement_rate, 6),
            "actions": dict(sorted(action_counts.items())),
            "mismatch_examples": examples,
            "input_gap_examples": gaps,
            "recompute_hash": _hash(fingerprints),
            "risk_policy_boundary": "calls RiskPolicyService only for supervisor risk subactions; audit evidence only",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_execution_gate_recompute(
        self,
        rows: list[dict[str, Any]],
        window_by_decision: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        attempted = 0
        agreements = 0
        disagreements = 0
        input_gaps = 0
        errors = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            action = _loads(row.get("action_json"), {})
            portfolio = _loads(row.get("portfolio_state_json"), {})
            live_gate = _extract_gate(action, portfolio)
            window = window_by_decision.get(decision_id) or {}
            bar = self._aligned_bar_for_recompute(window)
            factor_values = self._factor_values_for_recompute(row, action)
            issues: list[str] = []
            if not live_gate:
                issues.append("missing_live_gate_payload")
            if not bar:
                issues.append("missing_aligned_bar")
            if not isinstance(action, dict) or "direction" not in action:
                issues.append("missing_composite_direction")
            if not isinstance(action, dict) or "score" not in action:
                issues.append("missing_composite_score")
            if live_gate and str(live_gate.get("reason") or "").startswith("cooldown_"):
                issues.append("cooldown_state_not_replayable_v1")
            if issues:
                input_gaps += 1
                if len(gaps) < 50:
                    gaps.append({"decision_id": decision_id, "issues": issues})
                fingerprints.append({"decision_id": decision_id, "attempted": False, "issues": issues})
                continue
            try:
                attempted += 1
                composite = self._composite_for_recompute(row, action)
                gate_config = self._execution_gate_config_for_recompute(action)
                from alpha.execution_gate import ExecutionGate

                gate = ExecutionGate(gate_config)
                recomputed = gate.filter(composite, factor_values, bar)
                live_sig = _gate_signature(live_gate)
                replay_sig = (bool(getattr(recomputed, "passed", False)), str(getattr(recomputed, "reason", "")))
                agreed = live_sig == replay_sig
                if agreed:
                    agreements += 1
                else:
                    disagreements += 1
                    if len(examples) < 50:
                        examples.append(
                            {
                                "decision_id": decision_id,
                                "live": {"passed": live_sig[0], "reason": live_sig[1]},
                                "recomputed": {"passed": replay_sig[0], "reason": replay_sig[1]},
                            }
                        )
                fingerprints.append(
                    {
                        "decision_id": decision_id,
                        "attempted": True,
                        "live": {"passed": live_sig[0], "reason": live_sig[1]},
                        "recomputed": {"passed": replay_sig[0], "reason": replay_sig[1]},
                        "agreed": agreed,
                    }
                )
            except Exception as exc:
                errors += 1
                if len(examples) < 50:
                    examples.append(
                        {
                            "decision_id": decision_id,
                            "issues": ["execution_gate_recompute_error"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                fingerprints.append({"decision_id": decision_id, "attempted": True, "error": f"{type(exc).__name__}: {exc}"})
        decision_count = len(rows)
        coverage = attempted / decision_count if decision_count else 0.0
        agreement_rate = agreements / attempted if attempted else 0.0
        metrics = {
            "schema_version": "execution_gate_recompute_metrics.v1",
            "mode": "offline_execution_gate_filter",
            "read_only": True,
            "decision_count": decision_count,
            "attempted_count": attempted,
            "agreement_count": agreements,
            "disagreement_count": disagreements,
            "input_gap_count": input_gaps,
            "error_count": errors,
            "coverage": round(coverage, 6),
            "agreement_rate": round(agreement_rate, 6),
            "mismatch_examples": examples,
            "input_gap_examples": gaps,
            "recompute_hash": _hash(fingerprints),
            "risk_policy_boundary": "uses ExecutionGate.filter only; does not authorize live execution",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    def _evaluate_risk_policy_recompute(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        examples: list[dict[str, Any]] = []
        gaps: list[dict[str, Any]] = []
        fingerprints: list[dict[str, Any]] = []
        attempted = 0
        agreements = 0
        disagreements = 0
        input_gaps = 0
        errors = 0
        for row in rows:
            decision_id = str(row.get("decision_id") or "")
            action = _loads(row.get("action_json"), {})
            risk_state = _loads(row.get("risk_state_json"), {})
            portfolio = _loads(row.get("portfolio_state_json"), {})
            state_verdict, action_verdict = _extract_risk(action, risk_state)
            live_verdict = action_verdict or state_verdict
            issues: list[str] = []
            if not live_verdict:
                issues.append("missing_live_risk_policy_verdict")
            if not isinstance(action, dict) or "score" not in action:
                issues.append("missing_signal_score")
            if not isinstance(portfolio, dict):
                issues.append("missing_portfolio_state")
            context = self._risk_policy_context_for_recompute(row, action, portfolio, risk_state, live_verdict)
            if context.get("_input_gaps"):
                issues.extend(context.pop("_input_gaps"))
            if issues:
                input_gaps += 1
                if len(gaps) < 50:
                    gaps.append({"decision_id": decision_id, "issues": issues})
                fingerprints.append({"decision_id": decision_id, "attempted": False, "issues": issues})
                continue
            try:
                attempted += 1
                from risk.policy_service import RiskPolicyService

                recomputed = RiskPolicyService.shared().evaluate("open_trade", context)
                replay_verdict = recomputed.to_dict() if hasattr(recomputed, "to_dict") else dict(recomputed or {})
                live_sig = _verdict_signature(live_verdict)
                replay_sig = _verdict_signature(replay_verdict)
                agreed = live_sig == replay_sig
                if agreed:
                    agreements += 1
                else:
                    disagreements += 1
                    if len(examples) < 50:
                        examples.append(
                            {
                                "decision_id": decision_id,
                                "live": {"allowed": live_sig[0], "reason": live_sig[1]},
                                "recomputed": {"allowed": replay_sig[0], "reason": replay_sig[1]},
                            }
                        )
                fingerprints.append(
                    {
                        "decision_id": decision_id,
                        "attempted": True,
                        "live": {"allowed": live_sig[0], "reason": live_sig[1]},
                        "recomputed": {"allowed": replay_sig[0], "reason": replay_sig[1]},
                        "agreed": agreed,
                    }
                )
            except Exception as exc:
                errors += 1
                if len(examples) < 50:
                    examples.append(
                        {
                            "decision_id": decision_id,
                            "issues": ["risk_policy_recompute_error"],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                fingerprints.append({"decision_id": decision_id, "attempted": True, "error": f"{type(exc).__name__}: {exc}"})
        decision_count = len(rows)
        coverage = attempted / decision_count if decision_count else 0.0
        agreement_rate = agreements / attempted if attempted else 0.0
        metrics = {
            "schema_version": "risk_policy_recompute_metrics.v1",
            "mode": "offline_RiskPolicyService_open_trade",
            "read_only": True,
            "decision_count": decision_count,
            "attempted_count": attempted,
            "agreement_count": agreements,
            "disagreement_count": disagreements,
            "input_gap_count": input_gaps,
            "error_count": errors,
            "coverage": round(coverage, 6),
            "agreement_rate": round(agreement_rate, 6),
            "mismatch_examples": examples,
            "input_gap_examples": gaps,
            "recompute_hash": _hash(fingerprints),
            "risk_policy_boundary": "calls RiskPolicyService.evaluate('open_trade'); result is audit evidence only",
        }
        return {"metrics": metrics, "fingerprints": fingerprints}

    @staticmethod
    def _first_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any]:
        for event in events:
            if str(event.get("event_type") or "") == event_type:
                return dict(event)
        return {}

    @staticmethod
    def _slippage_reference_price(row: dict[str, Any], action: dict[str, Any], submitted: dict[str, Any]) -> float:
        candidates = [
            submitted.get("price") if submitted else None,
            action.get("current_price"),
            action.get("price"),
            action.get("reference_price"),
            action.get("quote_price"),
            action.get("bar_close"),
            row.get("action_price"),
        ]
        for candidate in candidates:
            value = _safe_float(candidate)
            if value > 0:
                return value
        return 0.0

    @staticmethod
    def _nearest_open_deal(deals: list[dict[str, Any]], fill: dict[str, Any]) -> dict[str, Any]:
        open_deals = [dict(deal) for deal in deals if not _safe_int(deal.get("is_close"))]
        if not open_deals:
            return {}
        fill_ts = _safe_float(fill.get("event_ts"))
        return min(open_deals, key=lambda deal: abs(_safe_float(deal.get("exec_timestamp")) - fill_ts))

    @staticmethod
    def _risk_subaction_context(action: str, trace: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        result = dict(context or {})
        result.setdefault("position_id", str(trace.get("position_id") or ""))
        result.setdefault("supervisor_action", str(trace.get("action") or action))
        result.setdefault("supervisor_reason", str(trace.get("summary_reason") or trace.get("execution_reason") or ""))
        result.setdefault("supervisor_confidence", _safe_float(trace.get("confidence")))
        result.setdefault("recommended_controls", {})
        result.setdefault("temporal_context", {})
        result.setdefault("loop_running", True)
        result.setdefault("bridge_connected", True)
        result.setdefault("runtime_incident_mode", "normal")
        if action == "close_position":
            result.setdefault("close_reason", "supervisor")
        return result

    @staticmethod
    def _aligned_bar_for_recompute(window: dict[str, Any]) -> dict[str, Any]:
        bars = window.get("bars") if isinstance(window.get("bars"), list) else []
        decision_ts = _safe_float(window.get("decision_ts"))
        before = [bar for bar in bars if _safe_float(bar.get("time")) <= decision_ts]
        return dict((before[-1] if before else bars[-1]) or {}) if bars else {}

    @staticmethod
    def _factor_values_for_recompute(row: dict[str, Any], action: dict[str, Any]) -> dict[str, float | None]:
        values: dict[str, float | None] = {}
        for item in row.get("factor_snapshots") or []:
            if not isinstance(item, dict):
                continue
            factor = str(item.get("factor") or "")
            if not factor:
                continue
            if int(item.get("gated") or 0):
                values[factor] = None
            else:
                values[factor] = _safe_float(item.get("raw_value"))
        action_values = action.get("factor_values") if isinstance(action.get("factor_values"), dict) else {}
        for key, value in action_values.items():
            values[str(key)] = None if value is None else _safe_float(value)
        return values

    @staticmethod
    def _composite_for_recompute(row: dict[str, Any], action: dict[str, Any]):
        from alpha.portfolio_compositor import CompositeSignal

        snapshots = [item for item in row.get("factor_snapshots") or [] if isinstance(item, dict)]
        factor_signals = {
            str(item.get("factor") or ""): (None if int(item.get("gated") or 0) else _safe_float(item.get("normalized_value")))
            for item in snapshots
            if str(item.get("factor") or "")
        }
        factor_values = {
            str(item.get("factor") or ""): (None if int(item.get("gated") or 0) else _safe_float(item.get("raw_value")))
            for item in snapshots
            if str(item.get("factor") or "")
        }
        active_weights = {
            str(item.get("factor") or ""): _safe_float(item.get("policy_weight"))
            for item in snapshots
            if str(item.get("factor") or "")
        }
        roles = action.get("factor_roles") if isinstance(action.get("factor_roles"), dict) else {}
        score = _safe_float(action.get("score"), _safe_float(row.get("action_score")))
        direction = _safe_int(action.get("direction"), 1 if score > 0 else -1 if score < 0 else 0)
        return CompositeSignal(
            direction=direction,
            score=score,
            tactical_score=_safe_float(action.get("tactical_score"), score),
            macro_score=_safe_float(action.get("macro_score")),
            tactical_weight=_safe_float(action.get("tactical_weight"), 0.7),
            macro_weight=_safe_float(action.get("macro_weight"), 0.3),
            factor_signals=factor_signals,
            factor_values=factor_values,
            active_weights=active_weights,
            tags_breakdown=action.get("tags_breakdown") if isinstance(action.get("tags_breakdown"), dict) else {},
            n_active_factors=_safe_int(action.get("n_active_factors"), len([v for v in factor_signals.values() if v is not None])),
            n_abstain_factors=_safe_int(action.get("n_abstain_factors"), len([v for v in factor_signals.values() if v is None])),
            timestamp=_safe_float(row.get("decision_ts")),
            composer_version=str(action.get("composer_version") or "replay"),
            alpha_score=_safe_float(action.get("alpha_score"), score),
            context_signals=action.get("context_signals") if isinstance(action.get("context_signals"), dict) else {},
            factor_roles={str(k): str(v) for k, v in roles.items()},
            n_active_alpha_factors=_safe_int(action.get("n_active_alpha_factors")),
            context_state=action.get("context_state") if isinstance(action.get("context_state"), dict) else {},
            redundancy_groups=action.get("redundancy_groups") if isinstance(action.get("redundancy_groups"), dict) else {},
            effective_alpha_factor_count=_safe_int(action.get("effective_alpha_factor_count")),
        )

    @staticmethod
    def _execution_gate_config_for_recompute(action: dict[str, Any]) -> dict[str, Any]:
        try:
            from backend.services.live_loop_shell import execution_gate_config
            from config.runtime_config import shared as runtime_config

            config = execution_gate_config(runtime_config())
        except Exception:
            config = {"signal_threshold": 0.3, "cooldown_bars": 3}
        context_policy = action.get("context_policy") if isinstance(action.get("context_policy"), dict) else {}
        try:
            delta = float(context_policy.get("signal_threshold_delta") or 0.0)
            config["signal_threshold"] = max(0.0, min(1.0, float(config.get("signal_threshold", 0.3) or 0.3) + delta))
        except Exception:
            pass
        return config

    def _risk_policy_context_for_recompute(
        self,
        row: dict[str, Any],
        action: dict[str, Any],
        portfolio: dict[str, Any],
        risk_state: dict[str, Any],
        live_verdict: dict[str, Any],
    ) -> dict[str, Any]:
        gaps: list[str] = []
        audit = live_verdict.get("audit_payload") if isinstance(live_verdict.get("audit_payload"), dict) else {}
        audit_state = audit.get("state") if isinstance(audit.get("state"), dict) else {}
        temporal_context = audit.get("temporal_context") if isinstance(audit.get("temporal_context"), dict) else {}
        if not temporal_context:
            temporal_context = {
                "timeframe_seconds": _timeframe_seconds(str(row.get("timeframe") or "M5")),
                "seconds_since_last_trade": 999999.0,
                "bars_since_last_trade": 999999.0,
            }
            gaps.append("missing_recorded_temporal_context")
        execution_context = action.get("execution_context") if isinstance(action.get("execution_context"), dict) else {}
        event_context = action.get("event_context") if isinstance(action.get("event_context"), dict) else {}
        entry_cluster = action.get("entry_cluster") if isinstance(action.get("entry_cluster"), dict) else {}
        portfolio_exposure = action.get("portfolio_exposure") if isinstance(action.get("portfolio_exposure"), dict) else {}
        data_quality = action.get("data_quality_context") if isinstance(action.get("data_quality_context"), dict) else {}
        trade = audit.get("trade") if isinstance(audit.get("trade"), dict) else {}
        requested_volume = (
            execution_context.get("requested_volume")
            or action.get("requested_volume")
            or action.get("volume")
            or audit.get("requested_api_volume")
            or 0.0
        )
        current_price = (
            trade.get("current_price")
            or execution_context.get("current_price")
            or action.get("price")
            or action.get("fill_price")
            or 0.0
        )
        try:
            from config.runtime_config import shared as runtime_config

            cfg = runtime_config()
        except Exception:
            cfg = SimpleNamespace(
                var_enabled=False,
                var_cvar_threshold=0.02,
                max_position_count=3,
                max_position_api_volume=1000.0,
                pyramid_enabled=True,
                risk_loss_cooldown_after_losses=0,
                risk_loss_cooldown_bars=0,
                risk_block_on_disk_critical=True,
                runtime_incident_mode="normal",
            )
        incident_mode = (
            audit.get("runtime_incident_mode")
            or risk_state.get("runtime_incident_mode")
            or getattr(cfg, "runtime_incident_mode", "normal")
            or "normal"
        )
        recorded_var = (
            audit.get("candidate_forward_var")
            if isinstance(audit.get("candidate_forward_var"), dict)
            else risk_state.get("var")
            if isinstance(risk_state.get("var"), dict)
            else {}
        )
        replay_risk_state = dict(risk_state)
        if recorded_var:
            replay_risk_state["var"] = recorded_var
        recorded_shadow_var = audit.get("candidate_forward_var_shadow_99")
        if isinstance(recorded_shadow_var, dict):
            replay_risk_state["var_shadow_99"] = recorded_shadow_var
        var_replayable = bool(recorded_var.get("status"))
        if bool(getattr(cfg, "var_enabled", False)) and not var_replayable:
            gaps.append("missing_recorded_risk_metrics_snapshot")
        context = {
            "trade": {
                "symbol": str(row.get("symbol") or trade.get("symbol") or "XAUUSD"),
                "direction": _safe_int(action.get("direction"), _safe_int(trade.get("direction"))),
                "current_price": _safe_float(current_price),
                "atr_price": _safe_float(action.get("atr_price")),
            },
            "account": {
                "balance": _safe_float(portfolio.get("balance")),
                "equity": _safe_float(portfolio.get("equity")),
            },
            "session": {
                "pnl": _safe_float(portfolio.get("session_pnl")),
                "start_balance": _safe_float(portfolio.get("start_balance")),
                "trades": _safe_int(portfolio.get("session_trades")),
                "consecutive_losses": _safe_int(portfolio.get("consecutive_losses")),
                "drawdown_pct": _safe_float(portfolio.get("drawdown_pct")),
                "circuit_breaker": bool(portfolio.get("circuit_breaker", False)),
            },
            "risk_snapshot": replay_risk_state,
            "var": {
                "enabled": (
                    bool(getattr(cfg, "var_enabled", False))
                    and var_replayable
                ),
                "threshold_pct": float(getattr(cfg, "var_cvar_threshold", 0.02) or 0.02) * 100.0,
            },
            "open_position_count": _safe_int(portfolio.get("n_positions"), _safe_int(audit_state.get("open_position_count"))),
            "max_position_count": int(getattr(cfg, "max_position_count", 3) or 0),
            "total_api_volume": _safe_float(portfolio_exposure.get("total_api_volume_before"), _safe_float(audit_state.get("total_api_volume"))),
            "requested_api_volume": _safe_float(requested_volume),
            "max_position_api_volume": float(getattr(cfg, "max_position_api_volume", 1000.0) or 0.0),
            "event_sizing": event_context or {"enabled": False, "multiplier": 1.0},
            "event_window_learning_policy": {},
            "entry_quality_gate": {},
            "entry_cluster": entry_cluster,
            "entry_cluster_learning_policy": {},
            "same_direction_cooldown_seconds": 0.0,
            "pyramid_enabled": bool(getattr(cfg, "pyramid_enabled", True)),
            "max_abs_entry_score": _safe_float(audit.get("max_abs_entry_score")),
            "signal_score": _safe_float(action.get("score"), _safe_float(row.get("action_score"))),
            "loop_running": bool(audit_state.get("loop_running", True)),
            "bridge_connected": bool(audit_state.get("bridge_connected", True)),
            "data_lag_seconds": _safe_float(data_quality.get("data_lag_seconds"), _safe_float(audit_state.get("data_lag_seconds"))),
            "runtime_health": audit_state.get("runtime_health") if isinstance(audit_state.get("runtime_health"), dict) else {},
            "loss_cooldown_after_losses": int(getattr(cfg, "risk_loss_cooldown_after_losses", 0) or 0),
            "loss_cooldown_bars": int(getattr(cfg, "risk_loss_cooldown_bars", 0) or 0),
            "block_on_disk_critical": bool(getattr(cfg, "risk_block_on_disk_critical", True)),
            "temporal_context": temporal_context,
            "supervisor_reentry_block": {},
            "runtime_incident_mode": str(incident_mode),
            "_input_gaps": gaps,
        }
        return context

    @staticmethod
    def _factor_frame_fingerprint(decision_id: str, frame) -> dict[str, Any]:
        columns = [str(col) for col in getattr(frame, "columns", [])]
        row_count = len(frame)
        tail = frame.tail(1).to_dict(orient="records")[0] if row_count else {}
        non_null = 0
        total = max(1, row_count * max(1, len(columns)))
        try:
            non_null = int(frame.notna().sum().sum())
        except Exception:
            non_null = 0
        return {
            "decision_id": decision_id,
            "row_count": row_count,
            "column_count": len(columns),
            "columns_hash": _hash(columns),
            "last_row_hash": _hash(tail),
            "frame_hash": _hash({"columns": columns, "tail": tail, "row_count": row_count}),
            "non_null_ratio": round(non_null / total, 6),
        }

    @staticmethod
    def _p1_replay_grade(
        anchor_grade: str,
        bar_metrics: dict[str, Any],
        frame_metrics: dict[str, Any],
        gate_metrics: dict[str, Any] | None,
        risk_metrics: dict[str, Any] | None,
        decision_count: int,
    ) -> str:
        if not decision_count:
            return "missing"
        if anchor_grade == "failed":
            return "failed"
        bar_coverage = _safe_float(bar_metrics.get("bar_window_coverage"))
        frame_coverage = _safe_float(frame_metrics.get("factor_frame_coverage"))
        gate_disagreements = _safe_int((gate_metrics or {}).get("disagreement_count"))
        gate_errors = _safe_int((gate_metrics or {}).get("error_count"))
        risk_disagreements = _safe_int((risk_metrics or {}).get("disagreement_count"))
        risk_errors = _safe_int((risk_metrics or {}).get("error_count"))
        stale = _safe_int(bar_metrics.get("stale_bar_alignment_count"))
        if gate_disagreements or gate_errors or risk_disagreements or risk_errors:
            return "C"
        if bar_coverage >= 0.95 and frame_coverage >= 0.95 and stale == 0 and anchor_grade in {"A", "B"}:
            return anchor_grade
        if bar_coverage >= 0.80 and frame_coverage >= 0.80 and anchor_grade in {"A", "B", "C"}:
            return "B" if anchor_grade in {"A", "B"} else "C"
        return "C"

    def _build_report(
        self,
        *,
        run_id: str,
        scope: dict[str, Any],
        rows: list[dict[str, Any]],
        created_at: float,
        replay_error: str,
    ) -> dict[str, Any]:
        mismatches: list[dict[str, Any]] = []
        matched = 0
        factor_covered = 0
        gate_covered = 0
        risk_covered = 0
        risk_verdict_disagreements = 0
        dataset_fingerprint: list[dict[str, Any]] = []
        for row in rows:
            action = _loads(row.get("action_json"), {})
            risk_state = _loads(row.get("risk_state_json"), {})
            portfolio = _loads(row.get("portfolio_state_json"), {})
            factor_count = _safe_int(row.get("factor_snapshot_count"))
            gate = _extract_gate(action, portfolio)
            state_verdict, action_verdict = _extract_risk(action, risk_state)
            row_issues: list[str] = []
            if factor_count > 0:
                factor_covered += 1
            else:
                row_issues.append("missing_factor_snapshot")
            if gate:
                gate_covered += 1
            else:
                row_issues.append("missing_gate_payload")
            if state_verdict or action_verdict:
                risk_covered += 1
            else:
                row_issues.append("missing_risk_policy_verdict")
            if state_verdict and action_verdict and _verdict_signature(state_verdict) != _verdict_signature(action_verdict):
                risk_verdict_disagreements += 1
                row_issues.append("risk_verdict_mismatch")
            if not row_issues:
                matched += 1
            elif len(mismatches) < 50:
                mismatches.append(
                    {
                        "decision_id": str(row.get("decision_id") or ""),
                        "event_type": str(row.get("event_type") or ""),
                        "issues": row_issues,
                    }
                )
            dataset_fingerprint.append(
                {
                    "decision_id": str(row.get("decision_id") or ""),
                    "decision_ts": _safe_float(row.get("decision_ts")),
                    "event_type": str(row.get("event_type") or ""),
                    "factor_snapshot_count": factor_count,
                }
            )
        decision_count = len(rows)
        mismatch_count = decision_count - matched
        risk_coverage = risk_covered / decision_count if decision_count else 0.0
        gate_coverage = gate_covered / decision_count if decision_count else 0.0
        factor_coverage = factor_covered / decision_count if decision_count else 0.0
        mismatch_rate = mismatch_count / decision_count if decision_count else 1.0
        if replay_error:
            evidence_grade = "failed"
        elif not decision_count:
            evidence_grade = "missing"
        elif mismatch_count == 0 and risk_coverage >= 0.95 and gate_coverage >= 0.95 and factor_coverage >= 0.95:
            evidence_grade = "A"
        elif mismatch_rate <= 0.10 and risk_coverage >= 0.80:
            evidence_grade = "B"
        else:
            evidence_grade = "C"
        snapshot = current_runtime_config_snapshot(db_path=self.db_path, create_if_missing=False)
        metrics = {
            "schema_version": "replay_metrics.v1",
            "factor_coverage": round(factor_coverage, 6),
            "gate_coverage": round(gate_coverage, 6),
            "risk_verdict_coverage": round(risk_coverage, 6),
            "risk_verdict_disagreement_count": risk_verdict_disagreements,
            "mismatch_rate": round(mismatch_rate, 6),
            "mismatch_examples": mismatches,
            "risk_policy_boundary": "RiskPolicyService verdict required in ledger payload",
        }
        return {
            "replay_run_id": run_id,
            "scope": scope,
            "input_dataset_hash": _hash(dataset_fingerprint),
            "runtime_config_hash": str(snapshot.get("config_hash") or ""),
            "code_version": _code_version(),
            "decision_count": decision_count,
            "matched_live_count": matched,
            "mismatch_count": mismatch_count,
            "metric_summary": metrics,
            "replay_error": replay_error,
            "evidence_grade": evidence_grade,
            "artifact_path": "",
            "artifact_hash": "",
            "status": "failed" if replay_error else "completed",
            "created_at": created_at,
        }

    def _attach_artifact(self, report: dict[str, Any]) -> dict[str, Any]:
        payload = {
            "schema_version": "replay_artifact.v1",
            "report": {
                **report,
                "artifact_path": "",
                "artifact_hash": "",
            },
        }
        try:
            self.artifact_dir.mkdir(parents=True, exist_ok=True)
            path = self.artifact_dir / f"{report.get('replay_run_id')}.json"
            raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2, default=str).encode("utf-8")
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_bytes(raw)
            tmp_path.replace(path)
            return {
                **report,
                "artifact_path": str(path),
                "artifact_hash": _hash_bytes(raw),
            }
        except Exception as exc:
            replay_error = str(report.get("replay_error") or "")
            artifact_error = f"artifact_write_failed:{type(exc).__name__}: {exc}"
            return {
                **report,
                "replay_error": f"{replay_error}; {artifact_error}" if replay_error else artifact_error,
                "evidence_grade": "failed",
                "status": "failed",
                "artifact_path": "",
                "artifact_hash": "",
            }

    def _persist_report(self, report: dict[str, Any]) -> None:
        ensure_replay_report_table(self.db_path)
        conn = _connect(self.db_path)
        try:
            _execute(
                conn,
                """
                INSERT INTO replay_report
                (replay_run_id, scope_json, input_dataset_hash, runtime_config_hash,
                 code_version, decision_count, matched_live_count, mismatch_count,
                 metric_summary_json, replay_error, evidence_grade, artifact_path,
                 artifact_hash, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(replay_run_id) DO UPDATE SET
                    scope_json=excluded.scope_json,
                    input_dataset_hash=excluded.input_dataset_hash,
                    runtime_config_hash=excluded.runtime_config_hash,
                    code_version=excluded.code_version,
                    decision_count=excluded.decision_count,
                    matched_live_count=excluded.matched_live_count,
                    mismatch_count=excluded.mismatch_count,
                    metric_summary_json=excluded.metric_summary_json,
                    replay_error=excluded.replay_error,
                    evidence_grade=excluded.evidence_grade,
                    artifact_path=excluded.artifact_path,
                    artifact_hash=excluded.artifact_hash,
                    status=excluded.status,
                    created_at=excluded.created_at
                """,
                (
                    str(report.get("replay_run_id") or ""),
                    _dumps(report.get("scope") or {}),
                    str(report.get("input_dataset_hash") or ""),
                    str(report.get("runtime_config_hash") or ""),
                    str(report.get("code_version") or ""),
                    _safe_int(report.get("decision_count")),
                    _safe_int(report.get("matched_live_count")),
                    _safe_int(report.get("mismatch_count")),
                    _dumps(report.get("metric_summary") or {}),
                    str(report.get("replay_error") or ""),
                    str(report.get("evidence_grade") or ""),
                    str(report.get("artifact_path") or ""),
                    str(report.get("artifact_hash") or ""),
                    str(report.get("status") or "completed"),
                    _safe_float(report.get("created_at")),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _row_to_report(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "replay_run_id": str(row.get("replay_run_id") or ""),
            "scope": _loads(row.get("scope_json"), {}),
            "input_dataset_hash": str(row.get("input_dataset_hash") or ""),
            "runtime_config_hash": str(row.get("runtime_config_hash") or ""),
            "code_version": str(row.get("code_version") or ""),
            "decision_count": _safe_int(row.get("decision_count")),
            "matched_live_count": _safe_int(row.get("matched_live_count")),
            "mismatch_count": _safe_int(row.get("mismatch_count")),
            "metric_summary": _loads(row.get("metric_summary_json"), {}),
            "replay_error": str(row.get("replay_error") or ""),
            "evidence_grade": str(row.get("evidence_grade") or ""),
            "artifact_path": str(row.get("artifact_path") or ""),
            "artifact_hash": str(row.get("artifact_hash") or ""),
            "status": str(row.get("status") or ""),
            "created_at": _safe_float(row.get("created_at")),
        }
