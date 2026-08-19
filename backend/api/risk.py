"""Risk API endpoints: summary, VaR, Kelly, stress test, concentration."""
import json
import re
import sqlite3
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import Any

from backend.core.auth import RequireUser
from backend.core.db import get_state_pg_conn, state_table_columns
from backend.risk import VaRCalculator, KellyCriterion, StressTest, ConcentrationChecker
from backend.services.api_fact_views import (
    policy_verdicts_fact_payload,
    risk_summary_fact_payload,
    trade_traces_fact_payload,
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
from backend.services.parameter_templates import ParameterTemplateService
from backend.services.review_contract import normalize_trade_review_contract
from backend.services.state_payload_archive import load_json_payload

router = APIRouter(prefix="/api/risk", tags=["risk"])

# Module-level singletons
_var_calc = VaRCalculator(confidence=0.95)
_kelly = KellyCriterion()
_stress = StressTest()
_conc = ConcentrationChecker(max_single_weight=0.40)
_CANDIDATE_ID_RE = re.compile(r"(ptrc_[0-9a-f]{16})")
_STATE_SQL_DIALECT = "postgres"


def get_state_conn(*, read_only: bool = True):
    return get_state_pg_conn(read_only=read_only)


def _state_conn(*, read_only: bool = True):
    global _STATE_SQL_DIALECT
    try:
        conn = get_state_conn(read_only=read_only)
    except TypeError:
        # Older tests and local tooling monkeypatch get_state_conn with a
        # zero-argument SQLite factory.
        conn = get_state_conn()
    _STATE_SQL_DIALECT = "sqlite" if isinstance(conn, sqlite3.Connection) else "postgres"
    return conn


def _state_sql(sql: str) -> str:
    if _STATE_SQL_DIALECT == "sqlite":
        return sql
    return sql.replace("%", "%%").replace("?", "%s")


def _loads_json(raw: Any, default: Any) -> Any:
    if not raw:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default


def _coerce_direction(value: Any) -> int:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        if value > 0:
            return 1
        if value < 0:
            return -1
        return 0
    text = str(value).strip().lower()
    if text in {"1", "+1", "long", "buy", "b", "多"}:
        return 1
    if text in {"-1", "short", "sell", "s", "空"}:
        return -1
    return 0


def _admission_owner_code(blockers: list[Any]) -> str:
    owners: list[str] = []
    for blocker in blockers:
        code = str(blocker or "").strip().lower()
        if not code:
            continue
        if (
            "safety" in code
            or "freshness" in code
            or "reconcile" in code
            or code == "no_new_risk_latched"
        ):
            owner = "safety"
        elif (
            code == "accepting_new_risk_false"
            or "generation_not_accepting" in code
            or "loop_stop" in code
            or "process_shutdown" in code
            or "session_" in code
        ):
            owner = "live_loop"
        else:
            owner = "open_admission"
        if owner not in owners:
            owners.append(owner)
    return "+".join(owners) or "open_admission"


def _direction_from_policy_payload(action_json: dict[str, Any], verdict: dict[str, Any]) -> int:
    audit_payload = verdict.get("audit_payload") or {}
    if not isinstance(audit_payload, dict):
        audit_payload = {}
    supervisor_evidence = audit_payload.get("supervisor_evidence") or {}
    if not isinstance(supervisor_evidence, dict):
        supervisor_evidence = {}
    for value in (
        action_json.get("direction"),
        action_json.get("side"),
        action_json.get("type"),
        audit_payload.get("direction"),
        audit_payload.get("side"),
        supervisor_evidence.get("direction"),
        supervisor_evidence.get("side"),
    ):
        direction = _coerce_direction(value)
        if direction:
            return direction
    return 0


def _position_id_from_policy_payload(row: Any, action_json: dict[str, Any], verdict: dict[str, Any]) -> str:
    audit_payload = verdict.get("audit_payload") or {}
    if not isinstance(audit_payload, dict):
        audit_payload = {}
    for value in (
        row["position_id"] if "position_id" in row else "",
        action_json.get("position_id"),
        audit_payload.get("position_id"),
    ):
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _get_system_health_report():
    try:
        from monitor.system_health import shared as _system_health_shared

        return _system_health_shared().get_last_report()
    except Exception:
        return None


def _runtime_risk_policy() -> dict[str, bool]:
    try:
        from config.runtime_config import shared as _runtime_cfg

        cfg = _runtime_cfg()
        return {
            "block_on_disk_critical": bool(getattr(cfg, "risk_block_on_disk_critical", True)),
        }
    except Exception:
        return {
            "block_on_disk_critical": True,
        }


def _active_bar_component() -> str:
    try:
        from config.runtime_config import shared as _runtime_cfg

        timeframe = str(getattr(_runtime_cfg(), "timeframe", "M5") or "M5").upper()
    except Exception:
        timeframe = "M5"
    if timeframe == "M1":
        return "bar_m1"
    return "bar_m5"


def _advisory_only_components() -> set[str]:
    advisory: set[str] = set()
    active_bar = _active_bar_component()
    for name in ("bar_m1", "bar_m5"):
        if name != active_bar:
            advisory.add(name)
    return advisory


def _legacy_policy_verdict_rows(
    conn: sqlite3.Connection, *, limit: int, pre_policy_limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Legacy decision_ledger reads (pre-cutover environments only)."""
    query = """
        SELECT decision_id, position_id, event_type, symbol, timeframe, decision_ts,
               action_reason, action_json, risk_state_json
        FROM decision_ledger
        WHERE risk_state_json LIKE '%policy_verdict%'
           OR action_json LIKE '%risk_verdict%'
        ORDER BY decision_ts DESC, created_at DESC
        LIMIT ?
        """
    try:
        rows = conn.execute(_state_sql(query), (limit,)).fetchall()
    except Exception as exc:
        if "position_id" not in str(exc).lower():
            raise
        try:
            conn.rollback()
        except Exception:
            pass
        rows = conn.execute(
            _state_sql("""
            SELECT decision_id, '' AS position_id, event_type, symbol, timeframe, decision_ts,
                   action_reason, action_json, risk_state_json
            FROM decision_ledger
            WHERE risk_state_json LIKE '%policy_verdict%'
               OR action_json LIKE '%risk_verdict%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """),
            (limit,),
        ).fetchall()
    # A signal can pass the factor gate and still be stopped by the live
    # open-admission gate before RiskPolicy is called. Those observations
    # deliberately have no policy_verdict, so keep them as a separate
    # read-only projection instead of counting them as policy decisions.
    pre_policy_query = """
        SELECT decision_id, position_id, event_type, symbol, timeframe, decision_ts,
               action_reason, action_json, risk_state_json
        FROM decision_ledger
        WHERE event_type = 'skip'
          AND action_json LIKE '%before_candidate%'
        ORDER BY decision_ts DESC, created_at DESC
        LIMIT ?
        """
    try:
        pre_policy_rows = conn.execute(
            _state_sql(pre_policy_query), (pre_policy_limit,)
        ).fetchall()
    except Exception as exc:
        if "position_id" not in str(exc).lower():
            raise
        try:
            conn.rollback()
        except Exception:
            pass
        pre_policy_rows = conn.execute(
            _state_sql("""
            SELECT decision_id, '' AS position_id, event_type, symbol, timeframe, decision_ts,
                   action_reason, action_json, risk_state_json
            FROM decision_ledger
            WHERE event_type = 'skip'
              AND action_json LIKE '%before_candidate%'
            ORDER BY decision_ts DESC, created_at DESC
            LIMIT ?
            """),
            (pre_policy_limit,),
        ).fetchall()
    return [dict(row) for row in rows], [dict(row) for row in pre_policy_rows]


def _canonical_policy_verdict_rows(
    conn: Any, *, limit: int, pre_policy_limit: int
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Canonical equivalent of the legacy verdict reads.

    Streams decisions newest-first and drains same-timestamp ties before the
    cut, then applies the legacy (decision_ts DESC, created_at DESC) order and
    LIMIT in Python, so the result is identical to the legacy SQL projection.
    """
    verdict_matches: list[dict[str, Any]] = []
    pre_policy_matches: list[dict[str, Any]] = []
    verdict_boundary: float | None = None
    pre_boundary: float | None = None
    for row in iter_decision_rows(conn, limit=0, reverse=True):
        decision_ts = float(row.get("decision_ts") or 0.0)
        if (
            verdict_boundary is not None
            and pre_boundary is not None
            and decision_ts < verdict_boundary
            and decision_ts < pre_boundary
        ):
            break
        risk_state_json = str(row.get("risk_state_json") or "")
        action_json = str(row.get("action_json") or "")
        if "policy_verdict" in risk_state_json or "risk_verdict" in action_json:
            if len(verdict_matches) < limit:
                verdict_matches.append(row)
                if len(verdict_matches) == limit:
                    verdict_boundary = decision_ts
            elif decision_ts >= verdict_boundary:
                verdict_matches.append(row)
        if str(row.get("event_type") or "") == "skip" and "before_candidate" in action_json:
            if len(pre_policy_matches) < pre_policy_limit:
                pre_policy_matches.append(row)
                if len(pre_policy_matches) == pre_policy_limit:
                    pre_boundary = decision_ts
            elif decision_ts >= pre_boundary:
                pre_policy_matches.append(row)

    def _sort_key(item: dict[str, Any]) -> tuple[float, float]:
        return (float(item.get("decision_ts") or 0.0), float(item.get("created_at") or 0.0))

    verdict_matches.sort(key=_sort_key, reverse=True)
    pre_policy_matches.sort(key=_sort_key, reverse=True)
    return verdict_matches[:limit], pre_policy_matches[:pre_policy_limit]


def _recent_policy_verdicts(limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(int(limit or 50), 200))
    pre_policy_limit = min(max(limit * 4, limit), 1000)
    conn = _state_conn(read_only=True)
    try:
        rows, pre_policy_rows = _canonical_policy_verdict_rows(
            conn, limit=limit, pre_policy_limit=pre_policy_limit
        )

        pre_policy_skips: list[dict[str, Any]] = []
        for row in pre_policy_rows:
            action_json = _loads_json(row["action_json"], {})
            if not isinstance(action_json, dict):
                continue
            # These fields are written by build_skip_ledger_payload when
            # RiskPolicy was not reached. Do not infer this from a missing
            # verdict: the explicit stage and boolean are the authority.
            if action_json.get("skip_stage") != "before_candidate":
                continue
            if action_json.get("risk_stage") != "not_reached":
                continue
            if action_json.get("risk_policy_reached") is not False:
                continue
            blockers = action_json.get("blockers") or []
            if not isinstance(blockers, list):
                blockers = [blockers]
            pre_policy_skips.append({
                "decision_id": row["decision_id"],
                "position_id": str(row["position_id"] or ""),
                "event_type": row["event_type"],
                "symbol": row["symbol"],
                "timeframe": row["timeframe"],
                "decision_ts": row["decision_ts"],
                "action_reason": str(
                    action_json.get("action_reason")
                    or row["action_reason"]
                    or "open_admission_blocked"
                ),
                "direction": _coerce_direction(
                    action_json.get("direction") or action_json.get("side")
                ),
                "tick": action_json.get("tick"),
                "gate_passed": bool(action_json.get("gate_passed", False)),
                "gate_reason": str(action_json.get("gate_reason") or ""),
                "skip_stage": "before_candidate",
                "risk_stage": "not_reached",
                "risk_policy_reached": False,
                "admission_gate_passed": bool(
                    action_json.get("admission_gate_passed", False)
                ),
                "blockers": [str(blocker) for blocker in blockers if blocker],
                "admission_owner": _admission_owner_code(blockers),
                "execution_intent_created": bool(
                    action_json.get("execution_intent_created", False)
                ),
            })

        parsed: list[tuple[Any, dict[str, Any], dict[str, Any], dict[str, Any], int, str]] = []
        position_ids: set[str] = set()
        for row in rows:
            risk_state = _loads_json(row["risk_state_json"], {})
            action_json = _loads_json(row["action_json"], {})
            if not isinstance(risk_state, dict):
                risk_state = {}
            if not isinstance(action_json, dict):
                action_json = {}
            verdict = risk_state.get("policy_verdict") or action_json.get("risk_verdict") or {}
            if not isinstance(verdict, dict):
                verdict = {}
            direction = _direction_from_policy_payload(action_json, verdict)
            position_id = _position_id_from_policy_payload(row, action_json, verdict)
            if not direction and position_id:
                position_ids.add(position_id)
            parsed.append((row, risk_state, action_json, verdict, direction, position_id))

        trace_by_decision: dict[str, dict[str, Any]] = {}
        decision_ids = {
            str(row["decision_id"] or "")
            for row, *_rest in parsed
            if str(row["decision_id"] or "")
        }
        if decision_ids:
            placeholders = ",".join("?" for _ in decision_ids)
            try:
                trace_rows = conn.execute(
                    _state_sql(f"""
                    SELECT decision_id, stage, outcome, execution_status,
                           execution_reason, event_ts
                    FROM position_supervisor_trace
                    WHERE decision_id IN ({placeholders})
                    ORDER BY event_ts DESC, created_at DESC
                    """),
                    tuple(decision_ids),
                ).fetchall()
                for trace_row in trace_rows:
                    decision_id = str(trace_row["decision_id"] or "")
                    trace_by_decision.setdefault(decision_id, dict(trace_row))
            except Exception:
                try:
                    conn.rollback()
                except Exception:
                    pass

        position_directions: dict[str, int] = {}
        if position_ids:
            placeholders = ",".join("?" for _ in position_ids)
            position_rows = conn.execute(
                _state_sql(f"""
                SELECT position_id, direction
                FROM recovery_position_state
                WHERE position_id IN ({placeholders})
                """),
                tuple(int(pid) if pid.isdigit() else pid for pid in position_ids),
            ).fetchall()
            position_directions = {
                str(item["position_id"]): _coerce_direction(item["direction"])
                for item in position_rows
            }
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    counts: dict[str, int] = {"allowed": 0, "blocked": 0}
    execution_counts: dict[str, int] = {
        "applied": 0,
        "skipped": 0,
        "blocked": 0,
        "failed": 0,
        "unknown": 0,
    }
    by_reason: dict[str, int] = {}
    by_action: dict[str, int] = {}

    for row, _risk_state, _action_json, verdict, direction, position_id in parsed:
        if not direction and position_id:
            direction = position_directions.get(position_id, 0)
        allowed = bool(verdict.get("allowed", False))
        reason = str(verdict.get("reason") or row["action_reason"] or "unknown")
        action = str((verdict.get("audit_payload") or {}).get("action") or _action_json.get("skip_stage") or row["event_type"])
        execution = trace_by_decision.get(str(row["decision_id"] or ""), {})
        execution_stage = str(execution.get("stage") or "")
        execution_outcome = str(execution.get("outcome") or "")
        execution_status = str(execution.get("execution_status") or "")
        execution_reason = str(execution.get("execution_reason") or "")
        execution_applied = (
            execution_stage == "executed" and execution_outcome == "applied"
        )
        if execution_applied:
            execution_category = "applied"
        elif execution_outcome == "blocked" or execution_status == "blocked":
            execution_category = "blocked"
        elif execution_outcome == "failed" or execution_status in {"failed", "exception"}:
            execution_category = "failed"
        elif execution_outcome in {"skipped", "hold"} or execution_status in {
            "skipped",
            "no_op",
            "cooldown",
            "not_required",
        }:
            execution_category = "skipped"
        else:
            execution_category = "unknown"
        counts["allowed" if allowed else "blocked"] += 1
        execution_counts[execution_category] += 1
        by_reason[reason] = by_reason.get(reason, 0) + 1
        by_action[action] = by_action.get(action, 0) + 1
        items.append({
            "decision_id": row["decision_id"],
            "position_id": position_id,
            "event_type": row["event_type"],
            "symbol": row["symbol"],
            "timeframe": row["timeframe"],
            "decision_ts": row["decision_ts"],
            "allowed": allowed,
            "reason": reason,
            "action": action,
            "direction": direction,
            "risk_verdict": verdict,
            "execution_stage": execution_stage,
            "execution_outcome": execution_outcome,
            "execution_status": execution_status,
            "execution_reason": execution_reason,
            "execution_applied": execution_applied,
            "execution_category": execution_category,
        })

    return {
        "limit": limit,
        "total": len(items),
        "counts": counts,
        "execution_counts": execution_counts,
        "by_reason": by_reason,
        "by_action": by_action,
        "items": items,
        "pre_policy_skips": pre_policy_skips,
    }


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value or default)
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            return value
    return value


def _risk_metrics_snapshot() -> dict[str, Any]:
    from backend.risk.metrics_snapshot import SNAPSHOT_KEY

    conn = _state_conn()
    try:
        row = conn.execute(
            _state_sql(
                "SELECT value_json FROM runtime_kv WHERE key=? LIMIT 1"
            ),
            (SNAPSHOT_KEY,),
        ).fetchone()
    except Exception:
        return {}
    finally:
        conn.close()
    if not row:
        return {}
    try:
        raw = row["value_json"]
    except (KeyError, TypeError):
        raw = row[0]
    return _loads_json(raw, {})


def _risk_component(name: str, fallback: dict[str, Any]) -> dict[str, Any]:
    snapshot = _risk_metrics_snapshot()
    result = dict((snapshot.get("components") or {}).get(name) or fallback)
    snapshot_status = snapshot.get("status") or "unknown"
    result["metric_status"] = result.get("status") or "unknown"
    if snapshot_status in {"unknown", "stale", "error"}:
        result["status"] = snapshot_status
    result["snapshot_status"] = snapshot_status
    result["as_of"] = snapshot.get("as_of")
    return result


def _review_archive_select(conn, *, alias: str = "r") -> str:
    if "review_archive_hash" not in state_table_columns(conn, "trade_outcome_review"):
        return ""
    return f", {alias}.review_archive_hash AS review_archive_hash"


def _parse_review_row(row: sqlite3.Row, conn=None) -> dict[str, Any]:
    item = dict(row)
    item["failure_tags"] = _loads_json(item.pop("failure_tags_json", None), [])
    inline_json = item.pop("review_json", None)
    archive_hash = item.pop("review_archive_hash", "")
    if conn is not None:
        review = load_json_payload(
            conn,
            source_table="trade_outcome_review",
            source_id=str(item.get("review_id") or ""),
            inline_json=inline_json,
            archive_hash=archive_hash,
            default={},
        )
    else:
        review = _loads_json(inline_json, {})
    if not isinstance(review, dict):
        review = {}
    normalized = normalize_trade_review_contract(
        review,
        entry_quality=item.get("entry_quality"),
        hold_quality=item.get("hold_quality"),
        exit_quality=item.get("exit_quality"),
        regime_fit_score=item.get("regime_fit_score"),
        execution_quality=item.get("execution_quality"),
    )
    item["review"] = normalized
    item["regime_fit"] = normalized["regime_fit"]
    item["thesis_status_at_exit"] = normalized["thesis_status_at_exit"]
    item["regime_shift_at_exit"] = normalized["regime_shift_at_exit"]
    item["profit_capture_ratio"] = normalized["profit_capture_ratio"]
    item["giveback_ratio"] = normalized["giveback_ratio"]
    item["time_in_profit"] = normalized["time_in_profit"]
    item["holding_efficiency"] = normalized["holding_efficiency"]
    taxonomy = normalized.get("failure_taxonomy") or {}
    item["failure_taxonomy"] = taxonomy
    item["primary_responsibility"] = str(
        normalized.get("primary_responsibility")
        or taxonomy.get("primary_responsibility")
        or ""
    )
    item["responsibility_labels"] = list(
        normalized.get("responsibility_labels")
        or taxonomy.get("responsibility_labels")
        or []
    )
    return item


def _safe_int(value: Any) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except Exception:
        return None


def _db_path_from_conn(conn: sqlite3.Connection) -> str | None:
    return None


def _latest_symbol_context(conn: sqlite3.Connection, *, position_id: str, trade_id: str) -> dict[str, str]:
    if not position_id and not trade_id:
        return {"symbol": "", "timeframe": ""}
    best = None
    best_key = (0.0, 0.0)
    first_ts: float | None = None
    for row in iter_decision_rows(conn, limit=0, reverse=True):
        decision_ts = float(row.get("decision_ts") or 0.0)
        if first_ts is not None and decision_ts < first_ts:
            break
        if (position_id and str(row.get("position_id") or "") == position_id) or (
            trade_id and str(row.get("trade_id") or "") == trade_id
        ):
            if first_ts is None:
                first_ts = decision_ts
            key = (decision_ts, float(row.get("created_at") or 0.0))
            if key > best_key:
                best = row
                best_key = key
    if best is None:
        return {"symbol": "", "timeframe": ""}
    return {
        "symbol": str(best.get("symbol") or ""),
        "timeframe": str(best.get("timeframe") or ""),
    }


def _top_factor_hint_for_review(conn: sqlite3.Connection, review_id: str) -> dict[str, Any]:
    if not review_id:
        return {}
    try:
        rows = conn.execute(
            _state_sql("""
            SELECT factor, net_contribution, notes
            FROM factor_contribution_review
            WHERE review_id = ?
            ORDER BY ABS(net_contribution) DESC, id ASC
            LIMIT 5
            """),
            (review_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    items: list[dict[str, Any]] = []
    for row in rows:
        payload = _loads_json(str(row["notes"] or ""), {})
        items.append(
            {
                "factor": str(row["factor"] or ""),
                "net_contribution": float(row["net_contribution"] or 0.0),
                "primary_responsibility": str(payload.get("primary_responsibility") or ""),
                "responsibility_labels": list(payload.get("responsibility_labels") or []),
            }
        )
    if not items:
        return {}
    parameter_items = [
        item for item in items
        if item["primary_responsibility"] == "parameter"
        or "factor_logic_ok_but_param_suspect" in item["responsibility_labels"]
    ]
    return parameter_items[0] if parameter_items else items[0]
    if row is None:
        return None
    try:
        return str(row[2] or "")
    except Exception:
        return None


def _humanize_template_responsibility(value: str) -> str:
    key = str(value or "").lower()
    if key == "exit":
        return "退出问题"
    if key == "timing":
        return "时长问题"
    if key == "regime":
        return "市场切换问题"
    if key == "parameter":
        return "参数问题"
    if key == "thesis":
        return "thesis 失效"
    if key == "holding":
        return "持仓效率问题"
    return "待继续归因"


def _humanize_template_candidate_status(value: str) -> str:
    key = str(value or "").lower()
    if key == "pending_review":
        return "待审"
    if key == "approved":
        return "已批准"
    if key == "rejected":
        return "已拒绝"
    if key == "deployed":
        return "已发布"
    if key == "rolled_back":
        return "已回滚"
    if key == "superseded":
        return "已被新建议替代"
    return "状态未知"


def _humanize_approval_path(value: str) -> str:
    key = str(value or "").lower()
    if key == "offline_validation_then_gray_release":
        return "先离线验证再灰度发布"
    if key == "offline_replay_then_governed_release":
        return "先离线回放再规则发布"
    if key == "governed_apply_switch":
        return "经治理审批后受控切换"
    if key == "governor_review_then_live_switch":
        return "经 governor 审批后在线切换"
    return "按治理链继续推进"


def _parameter_governance_stage_snapshot(
    *,
    candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
) -> dict[str, str]:
    candidate = candidate or {}
    recommendation = recommendation or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    if candidate_id:
        if candidate_status == "pending_review":
            return {
                "stage_label": "待审候选",
                "next_step_label": "进入候选审核",
                "next_step_summary": "下一步先由系统规则审核离线证据；只有审核通过后，才允许继续灰度发布到运行态。",
                "entry_type": "candidate",
            }
        if candidate_status == "approved":
            return {
                "stage_label": "等待发布",
                "next_step_label": "执行灰度发布",
                "next_step_summary": "下一步把候选模板切到运行态，并继续观察后验 reward、胜率和是否需要回滚。",
                "entry_type": "candidate",
            }
        if candidate_status == "deployed":
            return {
                "stage_label": "发布观察",
                "next_step_label": "观察发布效果",
                "next_step_summary": "下一步持续盯后验表现，确认是否要强化当前模板，或者因为效果恶化而回滚。",
                "entry_type": "candidate",
            }
        if candidate_status == "rolled_back":
            return {
                "stage_label": "已回滚",
                "next_step_label": "回到离线复核",
                "next_step_summary": "下一步复核这次回滚的原因，再决定是否要重新离线验证或改用别的模板。",
                "entry_type": "candidate",
            }
        if candidate_status == "rejected":
            return {
                "stage_label": "已拒绝",
                "next_step_label": "保留证据继续观察",
                "next_step_summary": "下一步保留这次离线证据，等待更多样本后再决定是否重新发起模板候选。",
                "entry_type": "candidate",
            }
    recommendation_id = str(recommendation.get("recommendation_id") or "")
    if recommendation_id:
        scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
        if scope == "offline_deep":
            return {
                "stage_label": "离线深调",
                "next_step_label": "创建离线验证",
                "next_step_summary": "下一步先发起离线验证；验证通过后再登记灰度候选，并进入系统规则审核与发布链。",
                "entry_type": "recommendation",
            }
        return {
            "stage_label": "在线轻调",
            "next_step_label": "生成治理建议",
            "next_step_summary": "下一步把推荐转成正式治理建议，走 governor 审批后即可受控切换运行态模板。",
            "entry_type": "recommendation",
        }
    return {
        "stage_label": "",
        "next_step_label": "",
        "next_step_summary": "",
        "entry_type": "",
    }


def _parameter_governance_target_type(entry_type: str) -> str:
    key = str(entry_type or "").lower()
    if key == "candidate":
        return "模板候选"
    if key == "recommendation":
        return "参数推荐"
    if key == "suggestion":
        return "治理建议"
    if key == "parameter_lifecycle":
        return "治理轨迹"
    return ""


def _parameter_governance_action_label(entry_type: str, stage_label: str) -> str:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_label or "")
    if normalized_type == "candidate":
        if normalized_stage == "待审候选":
            return "去审候选"
        if normalized_stage == "等待发布":
            return "去发布"
        if normalized_stage == "发布观察":
            return "看观察"
        if normalized_stage == "已回滚":
            return "看回滚"
        return "看候选"
    if normalized_type == "recommendation":
        if normalized_stage == "在线轻调":
            return "去审建议"
        if normalized_stage == "离线深调":
            return "去做验证"
        return "看建议"
    return "打开治理"


def _parameter_governance_priority_snapshot(
    *,
    entry_type: str = "",
    stage_label: str = "",
    has_governance_factor: bool = False,
) -> dict[str, Any]:
    normalized_type = str(entry_type or "").lower()
    normalized_stage = str(stage_label or "")
    if normalized_type == "candidate" and normalized_stage == "待审候选":
        return {
            "score": 100,
            "label": "优先治理",
            "summary": "这条样本已经形成候选，优先等待系统规则审核推进。",
        }
    if normalized_type == "candidate" and normalized_stage == "等待发布":
        return {
            "score": 90,
            "label": "优先发布",
            "summary": "这条样本对应的候选已经批准，下一步应推进灰度发布。",
        }
    if normalized_type == "recommendation" and normalized_stage == "离线深调":
        return {
            "score": 80,
            "label": "优先验证",
            "summary": "这条样本已收敛到离线深调入口，当前应尽快做离线验证。",
        }
    if normalized_type == "recommendation" and normalized_stage == "在线轻调":
        return {
            "score": 70,
            "label": "优先审建议",
            "summary": "这条样本已满足在线轻调边界，可继续生成或审批治理建议。",
        }
    if normalized_type == "candidate" and normalized_stage == "发布观察":
        return {
            "score": 60,
            "label": "优先观察",
            "summary": "这条样本对应模板已经上线，当前重点是观察效果与回滚信号。",
        }
    if normalized_type == "candidate" and normalized_stage == "已回滚":
        return {
            "score": 50,
            "label": "优先复核",
            "summary": "这条样本对应治理链已经回滚，当前应回到离线复核。",
        }
    if has_governance_factor:
        return {
            "score": 40,
            "label": "继续收敛",
            "summary": "这条样本已经露出参数问题线索，但还没有形成更具体的治理对象。",
        }
    return {
        "score": 0,
        "label": "",
        "summary": "",
    }


def _parameter_governance_jump_snapshot(
    *,
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    lifecycle_event: dict[str, Any] | None = None,
    stage_label: str = "",
    target_type: str = "",
    action_label: str = "",
) -> dict[str, Any]:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    suggestion = suggestion or {}
    lifecycle_event = lifecycle_event or {}
    candidate_id = str(candidate.get("candidate_id") or "")
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    suggestion_id = str(suggestion.get("suggestion_id") or "")
    lifecycle_event_id = str(lifecycle_event.get("id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    suggestion_status = str(suggestion.get("status") or "").lower()
    recommendation_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()

    if candidate_id:
        return {
            "type": "offline_candidate",
            "type_label": target_type or "模板候选",
            "button_text": action_label or _parameter_governance_action_label("candidate", stage_label),
            "summary": (
                f"当前最该处理的是候选 {candidate_id} 的规则审核。"
                if candidate_status == "pending_review"
                else f"当前最该处理的是候选 {candidate_id} 的灰度发布。"
                if candidate_status == "approved"
                else f"当前治理链已落到候选 {candidate_id}，应继续围绕候选状态推进。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if suggestion_id:
        return {
            "type": "suggestion",
            "type_label": "治理建议",
            "button_text": (
                "去审批治理建议"
                if suggestion_status == "proposed"
                else "看已批建议"
                if suggestion_status == "approved"
                else "看回滚建议"
                if suggestion_status == "rolled_back"
                else "看替代记录"
                if suggestion_status == "superseded"
                else "看治理建议"
            ),
            "summary": (
                f"推荐 {recommendation_id or '--'} 已生成建议 {suggestion_id}，当前正等待审批。"
                if suggestion_status == "proposed"
                else f"建议 {suggestion_id} 已被更新证据替代，不再进入应用链路。"
                if suggestion_status == "superseded"
                else f"推荐 {recommendation_id or '--'} 已生成建议 {suggestion_id}，可继续查看其后续状态。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if lifecycle_event_id:
        return {
            "type": "parameter_lifecycle",
            "type_label": "治理轨迹",
            "button_text": "看治理轨迹",
            "summary": f"推荐 {recommendation_id or '--'} 已进入治理轨迹 {lifecycle_event_id}，可回看链路推进情况。",
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    if recommendation_id:
        return {
            "type": "template_recommendation",
            "type_label": target_type or "参数推荐",
            "button_text": action_label or ("看离线推荐" if recommendation_scope == "offline_deep" else "看在线推荐"),
            "summary": (
                f"当前还停在推荐 {recommendation_id}，下一步应先发起离线验证。"
                if recommendation_scope == "offline_deep"
                else f"当前还停在推荐 {recommendation_id}，下一步应先生成正式治理建议。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "suggestion_id": suggestion_id,
            "lifecycle_event_id": lifecycle_event_id,
        }
    return {
        "type": "",
        "type_label": "",
        "button_text": "",
        "summary": "",
        "candidate_id": "",
        "recommendation_id": "",
        "suggestion_id": "",
        "lifecycle_event_id": "",
    }


def _parameter_governance_todo_queue_snapshot(
    *,
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
    suggestion: dict[str, Any] | None = None,
    lifecycle_event: dict[str, Any] | None = None,
    stage_label: str = "",
    next_step_summary: str = "",
    priority_label: str = "",
    jump: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    suggestion = suggestion or {}
    lifecycle_event = lifecycle_event or {}
    primary_jump = jump or {}
    tasks: list[dict[str, Any]] = []

    def push_task(task: dict[str, Any] | None) -> None:
      if not task or not str(task.get("type") or ""):
          return
      target_id = str(task.get("target_id") or "")
      for existing in tasks:
          if str(existing.get("type") or "") == str(task.get("type") or "") and str(existing.get("target_id") or "") == target_id:
              return
      tasks.append(task)

    candidate_id = str(candidate.get("candidate_id") or "")
    candidate_status = str(candidate.get("status") or "").lower()
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    recommendation_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
    suggestion_id = str(suggestion.get("suggestion_id") or "")
    suggestion_status = str(suggestion.get("status") or "").lower()
    lifecycle_event_id = str(lifecycle_event.get("id") or "")

    if candidate_id:
        priority = _parameter_governance_priority_snapshot(entry_type="candidate", stage_label=stage_label)
        push_task({
            "type": "offline_candidate",
            "type_label": "模板候选",
            "target_id": candidate_id,
            "title": f"{candidate_id} · {stage_label or _humanize_template_candidate_status(candidate.get('status') or '')}",
            "reason": (
                "离线证据已经收敛，当前卡点是系统规则审核。"
                if candidate_status == "pending_review"
                else "候选已经通过审核，当前最关键的是推进灰度发布。"
                if candidate_status == "approved"
                else "模板已经进入运行态，当前重点转成观察效果与回滚信号。"
                if candidate_status == "deployed"
                else "候选已经回滚，当前应先回到离线复核，而不是继续上线。"
                if candidate_status == "rolled_back"
                else "当前治理链已经落到候选层，继续围绕候选状态推进。"
            ),
            "button_text": _parameter_governance_action_label("candidate", stage_label),
            "priority_label": str(priority["label"]),
            "summary": (
                f"先处理候选 {candidate_id} 的审核，避免治理链停在“已验证但未批准”。"
                if candidate_status == "pending_review"
                else f"先处理候选 {candidate_id} 的发布，把治理动作真正切到运行态。"
                if candidate_status == "approved"
                else f"候选 {candidate_id} 已经是当前最具体的治理对象。"
            ),
            "candidate_id": candidate_id,
            "recommendation_id": recommendation_id,
            "priority_score": int(priority["score"]),
        })

    if suggestion_id:
        suggestion_score = 75 if suggestion_status == "proposed" else 55 if suggestion_status == "approved" else 35
        push_task({
            "type": "suggestion",
            "type_label": "治理建议",
            "target_id": suggestion_id,
            "title": f"{suggestion_id or '--'} · {'待审批' if suggestion_status == 'proposed' else '已批准' if suggestion_status == 'approved' else '已回滚' if suggestion_status == 'rolled_back' else '已被新建议替代' if suggestion_status == 'superseded' else '建议处理中'}",
            "reason": "推荐已经生成正式治理建议，当前卡点是 governor 审批。" if suggestion_status == "proposed" else "推荐已经沉淀为 suggestion，可继续沿建议状态回看治理链。",
            "button_text": "去审建议" if suggestion_status == "proposed" else "看已批建议" if suggestion_status == "approved" else "看治理建议",
            "summary": f"推荐 {recommendation_id} 已经物化成 suggestion {suggestion_id or '--'}。" if recommendation_id else f"当前建议对象为 {suggestion_id or '--'}。",
            "suggestion_id": suggestion_id,
            "recommendation_id": recommendation_id,
            "priority_score": suggestion_score,
        })

    if recommendation_id:
        recommendation_stage = "离线深调" if recommendation_scope == "offline_deep" else "在线轻调"
        priority = _parameter_governance_priority_snapshot(entry_type="recommendation", stage_label=recommendation_stage)
        push_task({
            "type": "template_recommendation",
            "type_label": "参数推荐",
            "target_id": recommendation_id,
            "title": f"{recommendation_id} · {recommendation_stage}",
            "reason": "这条推荐当前只能先走离线验证，不能直接切线上。" if recommendation_scope == "offline_deep" else "这条推荐已经满足在线轻调边界，可以继续进入 suggestion 审批链。",
            "button_text": _parameter_governance_action_label("recommendation", recommendation_stage),
            "priority_label": str(priority["label"]),
            "summary": f"推荐 {recommendation_id} 还停在离线验证入口。" if recommendation_scope == "offline_deep" else f"推荐 {recommendation_id} 已可继续生成治理建议。",
            "recommendation_id": recommendation_id,
            "priority_score": int(priority["score"]),
        })

    if lifecycle_event_id:
        push_task({
            "type": "parameter_lifecycle",
            "type_label": "治理轨迹",
            "target_id": lifecycle_event_id,
            "title": f"{lifecycle_event_id or '--'} · 生命周期",
            "reason": "这条轨迹适合回看 recommendation -> candidate -> release 的完整推进链。",
            "button_text": "看治理轨迹",
            "summary": "需要核对历史推进脉络时，优先回到 lifecycle 事件。",
            "lifecycle_event_id": lifecycle_event_id,
            "priority_score": 10,
        })

    if not tasks:
        return None
    tasks.sort(key=lambda item: (-int(item.get("priority_score") or 0), str(item.get("target_id") or "")))
    primary_type = str(primary_jump.get("type") or "")
    primary_target_id = (
        str(primary_jump.get("candidate_id") or "")
        or str(primary_jump.get("suggestion_id") or "")
        or str(primary_jump.get("recommendation_id") or "")
        or str(primary_jump.get("lifecycle_event_id") or "")
    )
    primary_task = None
    if primary_type:
        for task in tasks:
            if str(task.get("type") or "") == primary_type and (
                not primary_target_id or str(task.get("target_id") or "") == primary_target_id
            ):
                primary_task = task
                break
    if not primary_task:
        primary_task = tasks[0]
    secondary_tasks = [
        task for task in tasks
        if not (
            str(task.get("type") or "") == str(primary_task.get("type") or "")
            and str(task.get("target_id") or "") == str(primary_task.get("target_id") or "")
        )
    ]
    return {
        "primary_task": primary_task,
        "secondary_tasks": secondary_tasks,
        "queue_summary": f"当前主推进动作：{next_step_summary}" if next_step_summary else "当前已识别出可继续推进的参数治理对象。",
        "queue_hint": f"除主任务外，当前还可回看 {len(secondary_tasks)} 个关联治理对象。" if secondary_tasks else "当前没有更多并行治理对象，先把主任务处理完。",
        "priority_label": priority_label,
    }


def _parameter_governance_timeline_context_snapshot(
    *,
    factor_id: str = "",
    stage_label: str = "",
    stage_summary: str = "",
    next_step_summary: str = "",
    jump: dict[str, Any] | None = None,
    todo_queue: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jump = jump or {}
    todo_queue = todo_queue or {}
    jump_type = str(jump.get("type") or "")
    jump_type_label = str(jump.get("type_label") or "")
    actions: list[dict[str, Any]] = []

    def push_action(action: dict[str, Any] | None) -> None:
        if not action or not str(action.get("type") or ""):
            return
        normalized = {
            "type": str(action.get("type") or ""),
            "type_label": str(action.get("type_label") or ""),
            "button_text": str(action.get("button_text") or ""),
            "summary": str(action.get("summary") or ""),
            "factor_id": str(action.get("factor_id") or factor_id or ""),
            "source": str(action.get("source") or "trade_trace_timeline"),
            "candidate_id": str(action.get("candidate_id") or ""),
            "recommendation_id": str(action.get("recommendation_id") or ""),
            "suggestion_id": str(action.get("suggestion_id") or ""),
            "lifecycle_event_id": str(action.get("lifecycle_event_id") or ""),
        }
        target_id = (
            normalized["candidate_id"]
            or normalized["suggestion_id"]
            or normalized["recommendation_id"]
            or normalized["lifecycle_event_id"]
        )
        for existing in actions:
            existing_target = (
                existing["candidate_id"]
                or existing["suggestion_id"]
                or existing["recommendation_id"]
                or existing["lifecycle_event_id"]
            )
            if existing["type"] == normalized["type"] and existing_target == target_id:
                return
        actions.append(normalized)

    push_action(jump)
    primary_task = dict(todo_queue.get("primary_task") or {})
    if primary_task:
        push_action(primary_task)
    for task in todo_queue.get("secondary_tasks") or []:
        push_action(dict(task or {}))
    return {
        "stage_tag": str(stage_label or ""),
        "stage_summary": str(stage_summary or next_step_summary or ""),
        "review_stage_tag": str(stage_label or "参数问题已入治理"),
        "review_stage_summary": str(stage_summary or next_step_summary or "这条复盘已经能直接接到后续参数治理链。"),
        "governance_jump_type": jump_type,
        "governance_jump_type_label": jump_type_label,
        "governance_jump_button_text": str(jump.get("button_text") or ""),
        "governance_jump_summary": str(jump.get("summary") or ""),
        "review_jump_button_text": (
            "按复盘去审建议"
            if jump_type == "suggestion"
            else "按复盘看候选"
            if jump_type == "offline_candidate"
            else "按复盘继续治理"
            if jump_type
            else ""
        ),
        "review_jump_summary": (
            f"这条复盘已经把问题收敛到参数治理，当前建议直接转去{jump_type_label or '治理对象'}继续处理。"
            if jump_type
            else ""
        ),
        "governance_actions": actions,
        "candidate_id": str(jump.get("candidate_id") or ""),
        "recommendation_id": str(jump.get("recommendation_id") or ""),
        "suggestion_id": str(jump.get("suggestion_id") or ""),
        "lifecycle_event_id": str(jump.get("lifecycle_event_id") or ""),
    }


def _parameter_governance_timeline_filter_context_snapshot() -> dict[str, Any]:
    return {
        "focus_filters": {
            "all": {
                "label": "全部",
                "summary_template": "当前证据链共 {count} 个事件。",
                "empty_summary": "当前还没有可展示的时间线事件。",
            },
            "governance": {
                "label": "治理相关",
                "summary_template": "优先关注 {count} 个治理/复盘事件，先判断是否要走 recommendation、suggestion 或 candidate 链路。",
                "empty_summary": "当前还没有治理或复盘事件，先看执行与决策证据。",
            },
            "decision": {
                "label": "决策监督",
                "summary_template": "这里收敛了 {count} 个开仓、监督或风控裁决事件。",
                "empty_summary": "当前没有额外的决策或监督事件。",
            },
            "execution": {
                "label": "执行落地",
                "summary_template": "这里收敛了 {count} 个仓位、订单或恢复事件。",
                "empty_summary": "当前没有额外的执行落地事件。",
            },
        },
        "governance_stage_filters": {
            "all": {
                "label": "全部治理态",
                "summary_template": "当前治理相关时间线共 {count} 个事件。",
                "empty_summary": "当前还没有治理相关时间线事件。",
            },
            "online_light": {
                "label": "在线轻调",
                "summary": "当前可以继续生成建议并走受控审批切换。",
            },
            "offline_deep": {
                "label": "离线深调",
                "summary": "当前不能直接上线，必须先走离线验证。",
            },
            "pending_review": {
                "label": "待审候选",
                "summary": "离线证据已经形成，当前重点是系统规则审核。",
            },
            "approved": {
                "label": "等待发布",
                "summary": "候选已通过审核，下一步应推进灰度发布。",
            },
            "deployed": {
                "label": "发布观察",
                "summary": "模板已进运行态，当前重点是观察效果与回滚信号。",
            },
            "rolled_back": {
                "label": "已回滚",
                "summary": "这条参数治理链已经回滚，当前应回到离线复核。",
            },
        },
    }


def _parameter_governance_entry_context_snapshot(
    *,
    stage_label: str = "",
    stage_summary: str = "",
    next_step_label: str = "",
    next_step_summary: str = "",
    entry_type: str = "",
    target_type: str = "",
    action_label: str = "",
) -> dict[str, Any]:
    return {
        "entry_type": str(entry_type or ""),
        "entry_label": str(target_type or ""),
        "action_label": str(action_label or ""),
        "stage_label": str(stage_label or ""),
        "stage_summary": str(stage_summary or ""),
        "next_step_label": str(next_step_label or ""),
        "next_step_summary": str(next_step_summary or ""),
    }


def _parameter_governance_quick_actions_snapshot(
    *,
    factor_id: str = "",
    latest_candidate: dict[str, Any] | None = None,
    recommendation: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidate = latest_candidate or {}
    recommendation = recommendation or {}
    actions: list[dict[str, Any]] = []
    recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or "")) or str(recommendation.get("recommendation_id") or "")
    candidate_id = str(candidate.get("candidate_id") or "")
    if recommendation_id:
        actions.append({
            "type": "template_recommendation",
            "label": "查看治理建议",
            "button_tone": "primary",
            "summary": f"回到推荐 {recommendation_id} 查看原始治理建议与边界结论。",
            "factor_id": str(factor_id or ""),
            "recommendation_id": recommendation_id,
            "candidate_id": candidate_id,
        })
    if candidate_id:
        actions.append({
            "type": "offline_candidate",
            "label": "查看模板候选",
            "button_tone": "secondary",
            "summary": f"回到候选 {candidate_id} 查看离线验证、审核与发布状态。",
            "factor_id": str(factor_id or ""),
            "recommendation_id": recommendation_id,
            "candidate_id": candidate_id,
        })
    return actions


def _parameter_governance_overview_snapshot(
    *,
    ops_summary: str = "",
    stage_label: str = "",
    stage_summary: str = "",
    next_step_label: str = "",
    next_step_summary: str = "",
    entry_type: str = "",
    target_type: str = "",
    action_label: str = "",
    priority_label: str = "",
    priority_summary: str = "",
    latest_candidate: dict[str, Any] | None = None,
) -> dict[str, Any]:
    candidate = latest_candidate or {}
    candidate_trace = dict(candidate.get("trace") or {})
    latest_candidate_id = str(candidate.get("candidate_id") or "")
    latest_candidate_status_text = _humanize_template_candidate_status(candidate.get("status") or "") if candidate else ""
    latest_candidate_trace_text = (
        f"来源推荐 {candidate_trace.get('recommendation_id')} · "
        f"{_humanize_template_responsibility(((candidate_trace.get('responsibility') or {}).get('primary_responsibility') or ''))}"
        if candidate_trace.get("recommendation_id")
        else ""
    )
    overview_ops_summary = str(ops_summary or "")
    overview_stage_label = str(stage_label or ("参数问题待收敛" if overview_ops_summary else ""))
    overview_stage_summary = str(
        stage_summary
        or ("这笔交易已经暴露出参数问题线索，但还没有形成可执行的模板推荐或候选。" if overview_ops_summary else "")
    )
    overview_next_step_label = str(next_step_label or ("继续收敛证据" if overview_ops_summary else ""))
    overview_next_step_summary = str(
        next_step_summary
        or ("下一步继续积累参数可疑证据，等待推荐或离线候选正式出现。" if overview_ops_summary else "")
    )
    return {
        "ops_summary": overview_ops_summary,
        "stage_label": overview_stage_label,
        "stage_summary": overview_stage_summary,
        "next_step_label": overview_next_step_label,
        "next_step_summary": overview_next_step_summary,
        "entry_type": str(entry_type or ""),
        "entry_label": str(target_type or ""),
        "entry_hint_text": f"建议入口：{target_type}" if target_type else "",
        "target_type": str(target_type or ""),
        "action_label": str(action_label or ""),
        "priority_label": str(priority_label or ""),
        "priority_summary": str(priority_summary or ""),
        "latest_candidate_id": latest_candidate_id,
        "latest_candidate_status_text": latest_candidate_status_text,
        "latest_candidate_trace_text": latest_candidate_trace_text,
        "latest_candidate_summary_text": (
            f"最新模板候选 {latest_candidate_id} · {latest_candidate_status_text}"
            if latest_candidate_id and latest_candidate_status_text
            else ""
        ),
        "show_stage_card": bool(
            overview_stage_label
            or overview_stage_summary
            or overview_next_step_summary
            or target_type
            or action_label
            or priority_label
        ),
    }


def _latest_template_candidate_for_factor(conn: sqlite3.Connection, factor_id: str) -> dict[str, Any]:
    if not factor_id:
        return {}
    try:
        row = conn.execute(
            _state_sql("""
            SELECT candidate_id, factor_id, template_id, regime_key, status,
                   validation_summary_json, validation_report_path, created_at, updated_at
            FROM parameter_template_release_candidate
            WHERE factor_id=?
            ORDER BY updated_at DESC, created_at DESC
            LIMIT 1
            """),
            (factor_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return {}
    if not row:
        return {}
    validation_summary = _loads_json(row["validation_summary_json"], {})
    recommendation_source = dict(validation_summary.get("recommendation_source") or {})
    trace = {
        "source": str(recommendation_source.get("source") or ""),
        "recommendation_id": str(recommendation_source.get("recommendation_id") or ""),
        "reason": str(recommendation_source.get("reason") or ""),
        "responsibility": dict(recommendation_source.get("responsibility") or {}),
        "approval_path": str(recommendation_source.get("approval_path") or ""),
    } if recommendation_source else {}
    return {
        "candidate_id": str(row["candidate_id"] or ""),
        "factor_id": str(row["factor_id"] or ""),
        "template_id": str(row["template_id"] or ""),
        "regime_key": str(row["regime_key"] or ""),
        "status": str(row["status"] or ""),
        "validation_summary": validation_summary,
        "validation_report_path": str(row["validation_report_path"] or ""),
        "created_at": float(row["created_at"] or 0.0),
        "updated_at": float(row["updated_at"] or 0.0),
        "trace": trace,
    }


def _latest_template_suggestion_for_recommendation(
    conn: sqlite3.Connection,
    recommendation_id: str,
) -> dict[str, Any]:
    recommendation_key = str(recommendation_id or "").strip()
    if not recommendation_key:
        return {}
    try:
        rows = conn.execute(
            """
            SELECT suggestion_id, scope_type, scope_key, action, confidence, reason,
                   evidence_json, status, reviewed_at, review_note, created_at
            FROM policy_suggestion
            WHERE scope_type='parameter_template'
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        try:
            evidence = json.loads(row["evidence_json"] or "{}")
        except Exception:
            evidence = {}
        evidence_context = dict((evidence or {}).get("evidence_context") or {})
        if str(evidence_context.get("recommendation_id") or "") != recommendation_key:
            continue
        return {
            "suggestion_id": str(row["suggestion_id"] or ""),
            "scope_type": str(row["scope_type"] or ""),
            "scope_key": str(row["scope_key"] or ""),
            "action": str(row["action"] or ""),
            "confidence": float(row["confidence"] or 0.0),
            "reason": str(row["reason"] or ""),
            "status": str(row["status"] or ""),
            "reviewed_at": float(row["reviewed_at"] or 0.0),
            "review_note": str(row["review_note"] or ""),
            "created_at": float(row["created_at"] or 0.0),
            "evidence": evidence,
        }
    return {}


def _latest_parameter_template_lifecycle_for_recommendation(
    conn: sqlite3.Connection,
    *,
    factor_id: str,
    recommendation_id: str,
    candidate_id: str = "",
) -> dict[str, Any]:
    factor_key = str(factor_id or "").strip()
    recommendation_key = str(recommendation_id or "").strip()
    candidate_key = str(candidate_id or "").strip()
    if not factor_key and not recommendation_key and not candidate_key:
        return {}
    try:
        rows = conn.execute(
            _state_sql("""
            SELECT id, timestamp, event, factor, source, description, score, status, reason
            FROM lifecycle_events
            WHERE source='parameter_template' AND factor=?
            ORDER BY timestamp DESC, id DESC
            LIMIT 40
            """),
            (factor_key,),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    for row in rows:
        text = f"{row['description'] or ''} {row['reason'] or ''}"
        candidate_trace = {}
        match = _CANDIDATE_ID_RE.search(text)
        if match:
            candidate_trace = _candidate_trace_by_id(conn, match.group(1))
        trace_recommendation_id = str(candidate_trace.get("recommendation_id") or "")
        trace_candidate_id = str(candidate_trace.get("candidate_id") or "")
        if recommendation_key and trace_recommendation_id == recommendation_key:
            return {
                "id": int(row["id"] or 0),
                "ts": float(row["timestamp"] or 0.0),
                "event": str(row["event"] or ""),
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or ""),
                "description": str(row["description"] or ""),
                "score": float(row["score"] or 0.0),
                "status": str(row["status"] or ""),
                "reason": str(row["reason"] or row["description"] or ""),
                "candidate_trace": candidate_trace,
                "trace_locator": _latest_factor_trace_locator(conn, factor_key),
            }
        if candidate_key and trace_candidate_id == candidate_key:
            return {
                "id": int(row["id"] or 0),
                "ts": float(row["timestamp"] or 0.0),
                "event": str(row["event"] or ""),
                "factor": str(row["factor"] or ""),
                "source": str(row["source"] or ""),
                "description": str(row["description"] or ""),
                "score": float(row["score"] or 0.0),
                "status": str(row["status"] or ""),
                "reason": str(row["reason"] or row["description"] or ""),
                "candidate_trace": candidate_trace,
                "trace_locator": _latest_factor_trace_locator(conn, factor_key),
            }
    return {}


def _build_trade_trace_parameter_governance(
    conn: sqlite3.Connection,
    *,
    factor_contributions: list[dict[str, Any]],
) -> dict[str, Any]:
    suspected = [
        item for item in factor_contributions
        if str(item.get("primary_responsibility") or "") == "parameter"
        or "factor_logic_ok_but_param_suspect" in (item.get("responsibility_labels") or [])
    ]
    if not suspected:
        return {
            "timeline_filter_context": _parameter_governance_timeline_filter_context_snapshot(),
            "overview": {
                "ops_summary": "当前这笔交易还没有进入参数治理链。",
                "stage_label": "未进入治理链",
                "stage_summary": "",
                "next_step_label": "",
                "next_step_summary": "",
                "entry_type": "",
                "entry_label": "",
                "entry_hint_text": "",
                "target_type": "",
                "action_label": "",
                "priority_label": "",
                "priority_summary": "",
                "latest_candidate_id": "",
                "latest_candidate_status_text": "",
                "latest_candidate_trace_text": "",
                "latest_candidate_summary_text": "",
                "show_stage_card": False,
            },
        }
    suspected.sort(key=lambda item: abs(float(item.get("net_contribution") or 0.0)), reverse=True)
    anchor = suspected[0]
    factor_id = str(anchor.get("factor") or "")
    recommendation = None
    db_path = _db_path_from_conn(conn)
    try:
        recommendation = ParameterTemplateService(db_path).list_recommendations(
            factor_id=factor_id,
            limit=1,
        )[0]
    except Exception:
        recommendation = None
    latest_candidate = _latest_template_candidate_for_factor(conn, factor_id)
    latest_trace = dict(latest_candidate.get("trace") or {})
    suggestion = _latest_template_suggestion_for_recommendation(
        conn,
        str(
            (latest_trace.get("recommendation_id") or "")
            or ((recommendation or {}).get("recommendation_id") or "")
        ),
    )
    lifecycle_event = _latest_parameter_template_lifecycle_for_recommendation(
        conn,
        factor_id=factor_id,
        recommendation_id=str(
            (latest_trace.get("recommendation_id") or "")
            or ((recommendation or {}).get("recommendation_id") or "")
        ),
        candidate_id=str(latest_candidate.get("candidate_id") or ""),
    )
    governance_stage = _parameter_governance_stage_snapshot(
        candidate=latest_candidate,
        recommendation=recommendation,
    )
    priority = _parameter_governance_priority_snapshot(
        entry_type=str(governance_stage.get("entry_type") or ""),
        stage_label=str(governance_stage.get("stage_label") or ""),
        has_governance_factor=bool(factor_id),
    )
    target_type = _parameter_governance_target_type(str(governance_stage.get("entry_type") or ""))
    action_label = _parameter_governance_action_label(
        str(governance_stage.get("entry_type") or ""),
        str(governance_stage.get("stage_label") or ""),
    )
    governance_jump = _parameter_governance_jump_snapshot(
        latest_candidate=latest_candidate,
        recommendation=recommendation,
        suggestion=suggestion,
        lifecycle_event=lifecycle_event,
        stage_label=str(governance_stage.get("stage_label") or ""),
        target_type=target_type,
        action_label=action_label,
    )
    governance_todo_queue = _parameter_governance_todo_queue_snapshot(
        latest_candidate=latest_candidate,
        recommendation=recommendation,
        suggestion=suggestion,
        lifecycle_event=lifecycle_event,
        stage_label=str(governance_stage.get("stage_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        priority_label=str(priority["label"]),
        jump=governance_jump,
    )
    timeline_context = _parameter_governance_timeline_context_snapshot(
        factor_id=factor_id,
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        jump=governance_jump,
        todo_queue=governance_todo_queue,
    )
    timeline_filter_context = _parameter_governance_timeline_filter_context_snapshot()
    entry_context = _parameter_governance_entry_context_snapshot(
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_label=str(governance_stage.get("next_step_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        entry_type=str(governance_stage.get("entry_type") or ""),
        target_type=target_type,
        action_label=action_label,
    )
    quick_actions = _parameter_governance_quick_actions_snapshot(
        factor_id=factor_id,
        latest_candidate=latest_candidate,
        recommendation=recommendation,
    )
    responsibility_text = _humanize_template_responsibility(
        str(anchor.get("primary_responsibility") or "")
    )
    labels = list(anchor.get("responsibility_labels") or [])
    if latest_candidate:
        candidate_status = _humanize_template_candidate_status(latest_candidate.get("status") or "")
        if latest_trace.get("recommendation_id"):
            ops_summary = (
                f"这笔交易当前最值得关注的参数治理对象是 {factor_id}。"
                f"最近候选 {latest_candidate.get('candidate_id')} 当前{candidate_status}，"
                f"来源推荐 {latest_trace.get('recommendation_id')} "
                f"({responsibility_text}，{_humanize_approval_path(latest_trace.get('approval_path') or '')})。"
            )
        else:
            ops_summary = (
                f"这笔交易当前最值得关注的参数治理对象是 {factor_id}。"
                f"最近候选 {latest_candidate.get('candidate_id')} 当前{candidate_status}。"
            )
    elif recommendation:
        boundary_scope = str(((recommendation.get("boundary") or {}).get("recommended_scope") or "")).lower()
        boundary_text = "离线深调" if boundary_scope == "offline_deep" else "在线轻调"
        ops_summary = (
            f"这笔交易对 {factor_id} 的归因更像参数问题。"
            f"当前建议切到 {recommendation.get('target_template_version') or recommendation.get('target_template_id') or '--'}，"
            f"并按 {boundary_text} 路径推进。"
        )
    else:
        ops_summary = (
            f"这笔交易对 {factor_id} 的归因更像参数问题，"
            "但当前还没有形成可执行的模板推荐。"
        )
    overview = _parameter_governance_overview_snapshot(
        ops_summary=ops_summary,
        stage_label=str(governance_stage.get("stage_label") or ""),
        stage_summary=str(priority["summary"] or ""),
        next_step_label=str(governance_stage.get("next_step_label") or ""),
        next_step_summary=str(governance_stage.get("next_step_summary") or ""),
        entry_type=str(governance_stage.get("entry_type") or ""),
        target_type=target_type,
        action_label=action_label,
        priority_label=str(priority["label"]),
        priority_summary=str(priority["summary"]),
        latest_candidate=latest_candidate,
    )
    return {
        "factor_id": factor_id,
        "primary_responsibility": str(anchor.get("primary_responsibility") or ""),
        "responsibility_text": responsibility_text,
        "responsibility_labels": labels,
        "net_contribution": float(anchor.get("net_contribution") or 0.0),
        "factor_role": str(anchor.get("factor_role") or ""),
        "recommendation": recommendation,
        "latest_candidate": latest_candidate or None,
        "suggestion": suggestion or None,
        "lifecycle_event": lifecycle_event or None,
        "governance_jump": governance_jump,
        "governance_todo_queue": governance_todo_queue,
        "timeline_context": timeline_context,
        "timeline_filter_context": timeline_filter_context,
        "entry_context": entry_context,
        "quick_actions": quick_actions,
        "overview": overview,
        "stage_label": str(governance_stage.get("stage_label") or ""),
        "stage_summary": str(governance_stage.get("next_step_summary") or ""),
        "next_step_label": str(governance_stage.get("next_step_label") or ""),
        "next_step_summary": str(governance_stage.get("next_step_summary") or ""),
        "entry_type": str(governance_stage.get("entry_type") or ""),
        "target_type": target_type,
        "action_label": action_label,
        "priority_score": int(priority["score"]),
        "priority_label": str(priority["label"]),
        "priority_summary": str(priority["summary"]),
        "ops_summary": ops_summary,
    }


def _recent_trade_trace_index(limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(int(limit or 20), 100))
    conn = _state_conn(read_only=True)
    try:
        rows = iter_review_rows(conn, limit=0)
        rows.sort(key=lambda r: float(r.get("created_at") or 0.0), reverse=True)
        rows = rows[:limit]
        # Canonical symbol/timeframe context: resolve the review's entry
        # decision directly (indexed mapping); fall back to a position-event
        # map built from one bounded stream for reviews without a decision
        # reference. Symbol/timeframe is constant per trade, so any decision
        # or position event of the trade carries the same value.
        position_context: dict[str, tuple[str, str]] = {}
        for item in iter_position_rows(conn, limit=0):
            position_id = str(item.get("position_id") or "")
            trade_id = str(item.get("trade_id") or "")
            entry = (str(item.get("symbol") or ""), str(item.get("timeframe") or ""))
            if position_id:
                position_context[position_id] = entry
            if trade_id:
                position_context[trade_id] = entry

        def _symbol_context(
            position_id: str, trade_id: str, entry_decision_id: str = ""
        ) -> dict[str, str]:
            if entry_decision_id:
                resolved = decision_row(conn, entry_decision_id)
                if resolved:
                    return {
                        "symbol": str(resolved.get("symbol") or ""),
                        "timeframe": str(resolved.get("timeframe") or ""),
                    }
            entry = position_context.get(position_id) or position_context.get(trade_id)
            return {
                "symbol": entry[0] if entry else "",
                "timeframe": entry[1] if entry else "",
            }
        items: list[dict[str, Any]] = []
        for row in rows:
            parsed = _parse_review_row(row, conn)
            position_id = str(parsed.get("position_id") or "")
            trade_id = str(parsed.get("trade_id") or "")
            context = _symbol_context(position_id, trade_id, str(parsed.get("entry_decision_id") or ""))
            factor_hint = _top_factor_hint_for_review(conn, str(parsed.get("review_id") or ""))
            parameter_factor = ""
            parameter_candidate_status = ""
            parameter_candidate_id = ""
            parameter_recommendation_id = ""
            parameter_governance_stage = ""
            parameter_governance_stage_summary = ""
            parameter_governance_next_step = ""
            parameter_governance_entry_type = ""
            parameter_governance_target_type = ""
            parameter_governance_entry_hint_text = ""
            parameter_governance_action_label = ""
            parameter_governance_priority_score = 0
            parameter_governance_priority_label = ""
            parameter_governance_priority_summary = ""
            if factor_hint and (
                str(factor_hint.get("primary_responsibility") or "") == "parameter"
                or "factor_logic_ok_but_param_suspect" in (factor_hint.get("responsibility_labels") or [])
            ):
                parameter_factor = str(factor_hint.get("factor") or "")
                candidate = _latest_template_candidate_for_factor(conn, parameter_factor)
                recommendation = None
                db_path = _db_path_from_conn(conn)
                try:
                    recommendation = ParameterTemplateService(db_path).list_recommendations(
                        factor_id=parameter_factor,
                        limit=1,
                    )[0]
                except Exception:
                    recommendation = None
                parameter_candidate_status = str(candidate.get("status") or "")
                parameter_candidate_id = str(candidate.get("candidate_id") or "")
                parameter_recommendation_id = str(((candidate.get("trace") or {}).get("recommendation_id") or ""))
                if not parameter_recommendation_id and recommendation:
                    parameter_recommendation_id = str(recommendation.get("recommendation_id") or "")
                governance_stage = _parameter_governance_stage_snapshot(
                    candidate=candidate,
                    recommendation=recommendation,
                )
                parameter_governance_stage = governance_stage["stage_label"]
                parameter_governance_next_step = governance_stage["next_step_summary"]
                parameter_governance_entry_type = governance_stage["entry_type"]
                parameter_governance_stage_summary = (
                    parameter_governance_next_step
                    if parameter_governance_stage
                    else ""
                )
                parameter_governance_target_type = _parameter_governance_target_type(
                    parameter_governance_entry_type
                )
                parameter_governance_entry_hint_text = (
                    f"建议先看{parameter_governance_target_type}"
                    if parameter_governance_target_type
                    else ""
                )
                parameter_governance_action_label = _parameter_governance_action_label(
                    parameter_governance_entry_type,
                    parameter_governance_stage,
                )
                priority = _parameter_governance_priority_snapshot(
                    entry_type=parameter_governance_entry_type,
                    stage_label=parameter_governance_stage,
                    has_governance_factor=bool(parameter_factor),
                )
                parameter_governance_priority_score = int(priority["score"])
                parameter_governance_priority_label = str(priority["label"])
                parameter_governance_priority_summary = str(priority["summary"])
            items.append(
                {
                    "review_id": str(parsed.get("review_id") or ""),
                    "position_id": position_id,
                    "trade_id": trade_id,
                    "entry_decision_id": str(parsed.get("entry_decision_id") or ""),
                    "exit_decision_id": str(parsed.get("exit_decision_id") or ""),
                    "symbol": context["symbol"],
                    "timeframe": context["timeframe"],
                    "outcome_label": str(parsed.get("outcome_label") or ""),
                    "summary_text": str(parsed.get("summary_text") or ""),
                    "close_reason": str((parsed.get("review") or {}).get("close_reason") or ""),
                    "primary_responsibility": str(parsed.get("primary_responsibility") or ""),
                    "responsibility_labels": list(parsed.get("responsibility_labels") or []),
                    "parameter_governance_factor": parameter_factor,
                    "parameter_candidate_status": parameter_candidate_status,
                    "parameter_candidate_id": parameter_candidate_id,
                    "parameter_recommendation_id": parameter_recommendation_id,
                    "parameter_governance_stage": parameter_governance_stage,
                    "parameter_governance_stage_summary": parameter_governance_stage_summary,
                    "parameter_governance_next_step": parameter_governance_next_step,
                    "parameter_governance_entry_type": parameter_governance_entry_type,
                    "parameter_governance_target_type": parameter_governance_target_type,
                    "parameter_governance_entry_hint_text": parameter_governance_entry_hint_text,
                    "parameter_governance_action_label": parameter_governance_action_label,
                    "parameter_governance_priority_score": parameter_governance_priority_score,
                    "parameter_governance_priority_label": parameter_governance_priority_label,
                    "parameter_governance_priority_summary": parameter_governance_priority_summary,
                    "created_at": float(parsed.get("created_at") or 0.0),
                }
            )
        return {"items": items, "count": len(items), "limit": limit}
    finally:
        conn.close()


def _canonical_trace_review(
    conn: Any, *, position_id: str = "", decision_id: str = ""
) -> dict[str, Any] | None:
    """Latest review matching a position/trade or an entry/exit decision."""
    best = None
    best_ts = 0.0
    for row in iter_review_rows(conn, limit=0):
        matched = (
            bool(position_id)
            and (
                str(row.get("position_id") or "") == position_id
                or str(row.get("trade_id") or "") == position_id
            )
        ) or (
            bool(decision_id)
            and (
                str(row.get("entry_decision_id") or "") == decision_id
                or str(row.get("exit_decision_id") or "") == decision_id
            )
        )
        if not matched:
            continue
        row_ts = float(row.get("created_at") or 0.0)
        if best is None or row_ts > best_ts:
            best = row
            best_ts = row_ts
    return best


def _canonical_trace_ledger_rows(
    conn: Any, position_id: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Decisions for a position/trade plus that position's lifecycle events.

    The decision scan is bounded by the position's own event window (±24h):
    any decision that references the position must occur inside the position's
    lifecycle (entry before open, supervisor/close while live).  When the
    position has no lifecycle events the scan falls back to the full stream
    (correctness over speed).
    """
    position_key = str(position_id or "")
    position_events: list[dict[str, Any]] = []
    lo = hi = None
    for item in iter_position_rows(conn, limit=0):
        if str(item.get("position_id") or "") != position_key:
            continue
        position_events.append(item)
        ts = float(item.get("event_ts") or 0.0)
        lo = ts if lo is None else min(lo, ts)
        hi = ts if hi is None else max(hi, ts)
    margin = 24 * 3600.0
    if lo is not None:
        rows = [
            row
            for row in iter_decision_rows(
                conn,
                limit=0,
                min_observed_epoch=lo - margin,
                max_observed_epoch=(hi or lo) + margin,
            )
            if str(row.get("position_id") or "") == position_key
            or str(row.get("trade_id") or "") == position_key
        ]
    else:
        rows = [
            row
            for row in iter_decision_rows(conn, limit=0)
            if str(row.get("position_id") or "") == position_key
            or str(row.get("trade_id") or "") == position_key
        ]
    rows.sort(
        key=lambda r: (float(r.get("decision_ts") or 0.0), float(r.get("created_at") or 0.0))
    )
    return rows, position_events


def _trade_trace(position_id: str | None = None, decision_id: str | None = None) -> dict[str, Any]:
    resolved_position_id = str(position_id or "").strip()
    resolved_decision_id = str(decision_id or "").strip()
    if not resolved_position_id and not resolved_decision_id:
        raise ValueError("position_id or decision_id is required")

    conn = _state_conn(read_only=True)
    try:
        anchor = None
        if resolved_decision_id:
            anchor = decision_row(conn, resolved_decision_id)
            if anchor and not resolved_position_id:
                resolved_position_id = str(anchor["position_id"] or anchor["trade_id"] or "").strip()

        ledger_rows = []
        position_events_for_window: list[dict[str, Any]] = []
        if resolved_position_id:
            ledger_rows, position_events_for_window = _canonical_trace_ledger_rows(
                conn, resolved_position_id
            )
        elif anchor:
            ledger_rows = [anchor]

        if not anchor and ledger_rows:
            anchor = ledger_rows[0]
        if not anchor and resolved_decision_id and not resolved_position_id:
            raise LookupError(f"decision_id not found: {resolved_decision_id}")

        trade_id = ""
        symbol = ""
        timeframe = ""
        for row in ledger_rows:
            trade_id = trade_id or str(row["trade_id"] or "")
            symbol = symbol or str(row["symbol"] or "")
            timeframe = timeframe or str(row["timeframe"] or "")

        position_events = []
        recovery_state = None
        pos_int = _safe_int(resolved_position_id)
        if pos_int is not None:
            position_events = [
                item for item in position_events_for_window
                if str(item.get("position_id") or "") == str(pos_int)
            ]
            recovery_state = conn.execute(
                _state_sql("""
                SELECT position_id, broker, symbol, direction, open_price, volume, first_seen_at,
                       last_seen_at, status, strategy_name, entry_decision_id, context_integrity,
                       recovery_meta_json, closed_at, close_reason, close_pnl
                FROM recovery_position_state
                WHERE position_id = ?
                LIMIT 1
                """),
                (pos_int,),
            ).fetchone()

        order_events = []
        if trade_id:
            order_events = [
                item for item in iter_order_rows(conn, limit=0)
                if str(item.get("trade_id") or "") == trade_id
            ]

        review_row = None
        if resolved_position_id:
            review_row = _canonical_trace_review(conn, position_id=resolved_position_id)
        if review_row is None and resolved_decision_id:
            review_row = _canonical_trace_review(conn, decision_id=resolved_decision_id)

        factor_rows = []
        if review_row is not None:
            factor_rows = conn.execute(
                _state_sql("""
                SELECT id, review_id, trade_id, factor, entry_contribution, hold_contribution,
                       exit_contribution, net_contribution, confidence, notes
                FROM factor_contribution_review
                WHERE review_id = ?
                ORDER BY ABS(net_contribution) DESC, id ASC
                """),
                (review_row["review_id"],),
            ).fetchall()

        def _parse_ledger(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["portfolio_state"] = _loads_json(item.pop("portfolio_state_json", None), {})
            item["risk_state"] = _loads_json(item.pop("risk_state_json", None), {})
            item["action"] = _loads_json(item.pop("action_json", None), {})
            return item

        def _parse_event(row: sqlite3.Row) -> dict[str, Any]:
            item = dict(row)
            item["details"] = _loads_json(item.pop("details_json", None), {})
            return item

        review = _parse_review_row(review_row, conn) if review_row is not None else None
        factor_contributions = [dict(row) for row in factor_rows]
        for item in factor_contributions:
            raw_notes = str(item.get("notes") or "")
            note_payload = _loads_json(raw_notes, {}) if raw_notes.startswith("{") else {}
            item["note_payload"] = note_payload
            item["primary_responsibility"] = str(note_payload.get("primary_responsibility") or "")
            item["responsibility_labels"] = list(note_payload.get("responsibility_labels") or [])
            item["factor_role"] = str(note_payload.get("factor_role") or "")
        supervisor_events = []
        latest_supervisor = None
        for row in ledger_rows:
            event_type = str(row["event_type"] or "")
            if event_type.startswith("supervisor_"):
                parsed = _parse_ledger(row)
                supervisor_events.append(parsed)
                latest_supervisor = parsed

        def _supervisor_action(item: dict[str, Any] | None) -> str:
            if not item:
                return ""
            action = item.get("action") or {}
            verdict = action.get("supervisor_verdict") or {}
            return str(verdict.get("action") or "")

        def _supervisor_reason(item: dict[str, Any] | None) -> str:
            if not item:
                return ""
            action = item.get("action") or {}
            verdict = action.get("supervisor_verdict") or {}
            return str(verdict.get("summary_reason") or item.get("action_reason") or "")

        def _close_source_snapshot() -> dict[str, Any]:
            close_rows: list[dict[str, Any]] = []
            for row in ledger_rows:
                event_type = str(row["event_type"] or "")
                parsed_action = _loads_json(row["action_json"], {})
                verdict = parsed_action.get("supervisor_verdict") or {}
                if event_type == "close" or str(verdict.get("action") or "") == "close":
                    parsed = _parse_ledger(row)
                    close_rows.append(parsed)
            close_row = None
            if review and str(review.get("exit_decision_id") or ""):
                wanted = str(review.get("exit_decision_id") or "")
                close_row = next((item for item in close_rows if str(item.get("decision_id") or "") == wanted), None)
            if close_row is None and close_rows:
                close_row = close_rows[-1]

            direct_verdict = ((close_row or {}).get("action") or {}).get("supervisor_verdict") or {}
            if direct_verdict:
                return {
                    "source": "supervisor_direct",
                    "close_decision_id": str((close_row or {}).get("decision_id") or ""),
                    "close_event_type": str((close_row or {}).get("event_type") or ""),
                    "supervisor_decision_id": str((close_row or {}).get("decision_id") or ""),
                    "supervisor_event_type": str((close_row or {}).get("event_type") or ""),
                    "supervisor_action": str(direct_verdict.get("action") or ""),
                    "supervisor_reason": str(direct_verdict.get("summary_reason") or (close_row or {}).get("action_reason") or ""),
                    "seconds_before_close": 0.0,
                    "evidence": direct_verdict.get("evidence") or {},
                    "recommended_controls": direct_verdict.get("recommended_controls") or {},
                }

            close_ts = None
            if position_events:
                parsed_events = [_parse_event(row) for row in position_events]
                closed_events = [
                    item for item in parsed_events
                    if str(item.get("event_type") or "") == "closed"
                ]
                if closed_events:
                    close_ts = float(closed_events[-1].get("event_ts") or 0.0)
            if not close_ts and review:
                review_payload = review.get("review") or {}
                close_ts = float(review_payload.get("close_ts") or review.get("created_at") or 0.0)
            if not close_ts and close_row is not None:
                close_ts = float(close_row.get("decision_ts") or 0.0)

            inferred = None
            if close_ts:
                candidates = [
                    item for item in supervisor_events
                    if float(item.get("decision_ts") or 0.0) <= float(close_ts)
                    and (float(close_ts) - float(item.get("decision_ts") or 0.0)) <= 300.0
                ]
                if candidates:
                    inferred = candidates[-1]
            if inferred is not None:
                return {
                    "source": "supervisor_inferred",
                    "close_decision_id": str((close_row or {}).get("decision_id") or ""),
                    "close_event_type": str((close_row or {}).get("event_type") or ""),
                    "supervisor_decision_id": str(inferred.get("decision_id") or ""),
                    "supervisor_event_type": str(inferred.get("event_type") or ""),
                    "supervisor_action": _supervisor_action(inferred),
                    "supervisor_reason": _supervisor_reason(inferred),
                    "seconds_before_close": max(0.0, float(close_ts) - float(inferred.get("decision_ts") or 0.0)),
                    "evidence": (((inferred.get("action") or {}).get("supervisor_verdict") or {}).get("evidence") or {}),
                    "recommended_controls": (((inferred.get("action") or {}).get("supervisor_verdict") or {}).get("recommended_controls") or {}),
                }

            close_reason = str((review.get("review") or {}).get("close_reason") or "") if review else ""
            if not close_reason and recovery_state is not None:
                close_reason = str(recovery_state["close_reason"] or "")
            return {
                "source": close_reason or "unknown",
                "close_decision_id": str((close_row or {}).get("decision_id") or ""),
                "close_event_type": str((close_row or {}).get("event_type") or ""),
                "supervisor_decision_id": "",
                "supervisor_event_type": "",
                "supervisor_action": "",
                "supervisor_reason": "",
                "seconds_before_close": None,
                "evidence": {},
                "recommended_controls": {},
            }

        close_source = _close_source_snapshot()
        parameter_governance = _build_trade_trace_parameter_governance(
            conn,
            factor_contributions=factor_contributions,
        )
        if not ledger_rows and not position_events and not order_events and review is None and recovery_state is None:
            locator = resolved_position_id or resolved_decision_id
            raise LookupError(f"trade trace not found: {locator}")
        summary = {
            "position_id": resolved_position_id or (str(review["position_id"]) if review else ""),
            "decision_id": resolved_decision_id or (str(anchor["decision_id"]) if anchor else ""),
            "trade_id": trade_id or (str(review["trade_id"]) if review else ""),
            "symbol": symbol or (str(review["review"].get("symbol") or "") if review else ""),
            "timeframe": timeframe,
            "ledger_events": len(ledger_rows),
            "position_events": len(position_events),
            "order_events": len(order_events),
            "has_review": review is not None,
            "factor_count": len(factor_contributions),
            "latest_outcome": str(review["outcome_label"] or "") if review else "",
            "latest_close_reason": str((review.get("review") or {}).get("close_reason") or "") if review else "",
            "close_reason_source": str(close_source.get("source") or ""),
            "inferred_close_supervisor_action": str(close_source.get("supervisor_action") or ""),
            "inferred_close_supervisor_reason": str(close_source.get("supervisor_reason") or ""),
            "supervisor_events": len(supervisor_events),
            "latest_supervisor_action": _supervisor_action(latest_supervisor),
            "parameter_governance_factor": str(parameter_governance.get("factor_id") or ""),
        }
        return {
            "summary": summary,
            "anchor": dict(anchor) if anchor is not None else None,
            "decision_ledger": [_parse_ledger(row) for row in ledger_rows],
            "position_supervisor": {
                "latest": latest_supervisor,
                "events": supervisor_events,
                "close_source": close_source,
            },
            "inferred_close_supervisor": {
                "decision_id": str(close_source.get("supervisor_decision_id") or ""),
                "event_type": str(close_source.get("supervisor_event_type") or ""),
                "action": str(close_source.get("supervisor_action") or ""),
                "summary_reason": str(close_source.get("supervisor_reason") or ""),
                "seconds_before_close": close_source.get("seconds_before_close"),
                "evidence": close_source.get("evidence") or {},
                "recommended_controls": close_source.get("recommended_controls") or {},
            } if str(close_source.get("source") or "").startswith("supervisor_") else None,
            "position_lifecycle": [_parse_event(row) for row in position_events],
            "order_lifecycle": [_parse_event(row) for row in order_events],
            "review": review,
            "factor_contributions": factor_contributions,
            "parameter_governance": parameter_governance or None,
            "recovery_state": {
                **dict(recovery_state),
                "recovery_meta": _loads_json(recovery_state["recovery_meta_json"], {}),
            } if recovery_state is not None else None,
        }
    finally:
        conn.close()


def _capacity_trend_snapshot() -> dict[str, Any] | None:
    """Read-only capacity growth digest for the system_health dashboard.

    Invokes scripts/capacity_observe.py --trend (pure SELECT + JSON), reusing the existing
    observation instrument. Failure or PG unavailability yields None so it never blocks health.
    """
    try:
        import json as _json
        import subprocess
        from pathlib import Path
        root = Path(__file__).resolve().parents[2]
        out = subprocess.run(
            [str(root / ".venv/bin/python"), "scripts/capacity_observe.py", "--trend"],
            capture_output=True, text=True, cwd=root, timeout=20,
        )
        if out.returncode != 0:
            return None
        payload = _json.loads(out.stdout)
        return payload
    except Exception:
        return None


def _system_health_summary() -> dict[str, Any]:
    report = _get_system_health_report()
    if report is None:
        return {
            "overall": "unknown",
            "overall_score": 0.0,
            "critical_components": [],
            "degraded_components": [],
            "blocking_components": [],
            "advisory_critical_components": [],
            "trading_blocked": False,
            "impact_status": "unknown",
            "impact_summary": "还没有拿到运行环境快照，暂时无法判断是否会影响交易。",
            "policy_flags": _runtime_risk_policy(),
            "components": {},
            "capacity": None,
            "errors": [],
        }

    policy_flags = _runtime_risk_policy()
    components = getattr(report, "components", {}) or {}
    component_status = {
        str(name): {
            "status": str(getattr(component, "status", "") or ""),
            "detail": str(getattr(component, "detail", "") or ""),
            "score": float(getattr(component, "score", 0.0) or 0.0),
        }
        for name, component in components.items()
    }
    critical_components = [name for name, item in component_status.items() if item["status"] == "critical"]
    degraded_components = [name for name, item in component_status.items() if item["status"] == "degraded"]

    advisory_only_components = _advisory_only_components()
    blocking_components: list[str] = []
    advisory_critical_components: list[str] = []
    for name in critical_components:
        if name in advisory_only_components:
            advisory_critical_components.append(name)
        elif name == "disk_space" and not policy_flags["block_on_disk_critical"]:
            advisory_critical_components.append(name)
        else:
            blocking_components.append(name)

    trading_blocked = bool(blocking_components)
    if trading_blocked:
        impact_status = "blocked"
        impact_summary = (
            f"当前有 {len(blocking_components)} 个运行风险会直接阻断新开仓："
            + " / ".join(blocking_components)
        )
        if advisory_critical_components or degraded_components:
            advisory_parts = advisory_critical_components + degraded_components
            impact_summary += "；同时还有需要盯住的观察项：" + " / ".join(advisory_parts)
    elif advisory_critical_components or degraded_components:
        impact_status = "observe"
        focus_items = advisory_critical_components or degraded_components
        impact_summary = (
            "当前有运行观察项，但按现有风控配置不会直接阻断交易："
            + " / ".join(focus_items)
        )
        if advisory_critical_components and degraded_components:
            impact_summary += "；一般观察项：" + " / ".join(degraded_components)
    else:
        impact_status = "ok"
        impact_summary = "运行环境目前没有明显风险项，暂时不会额外拖累交易执行。"

    return {
        "overall": str(getattr(report, "overall", "unknown") or "unknown"),
        "overall_score": float(getattr(report, "overall_score", 0.0) or 0.0),
        "critical_components": critical_components,
        "degraded_components": degraded_components,
        "blocking_components": blocking_components,
        "advisory_critical_components": advisory_critical_components,
        "trading_blocked": trading_blocked,
        "impact_status": impact_status,
        "impact_summary": impact_summary,
        "policy_flags": policy_flags,
        "components": component_status,
        "capacity": _capacity_trend_snapshot(),
        "errors": list(getattr(report, "errors", []) or []),
        "ts": float(getattr(report, "ts", 0.0) or 0.0),
    }


class VarRequest(BaseModel):
    equity_series: list[float]


class KellyRequest(BaseModel):
    win_rate: float
    avg_win: float
    avg_loss: float


class StressRequest(BaseModel):
    positions: list[dict[str, Any]]
    account: dict[str, Any]
    shocks: list[float] = [-0.05, 0.05]


@router.get("/summary")
def get_risk_summary(_user: RequireUser) -> dict[str, Any]:
    """
    获取风控指标概览: VaR, Kelly, stress, concentration.
    """
    policy = _recent_policy_verdicts(limit=25)
    snapshot = _risk_metrics_snapshot()
    components = snapshot.get("components") or {}
    payload = {
        "snapshot": snapshot,
        "var": _json_safe(components.get("var") or _var_calc.get_status()),
        "kelly": _json_safe(components.get("kelly") or _kelly.get_status()),
        "stress": _json_safe(components.get("stress") or _stress.get_status()),
        "concentration": _json_safe(
            components.get("concentration") or _conc.get_status()
        ),
        "policy": policy,
        "system_health": _system_health_summary(),
    }
    return risk_summary_fact_payload(
        payload,
        risk_observed_at=snapshot.get("as_of"),
        risk_error=None if snapshot else "risk_metrics_snapshot_missing",
    )


@router.get("/policy/verdicts")
def get_policy_verdicts(_user: RequireUser, limit: int = 50) -> dict[str, Any]:
    """最近的统一风控裁决，用于 Phase B 风控面板与审计."""
    return policy_verdicts_fact_payload(_recent_policy_verdicts(limit=limit))


@router.get("/trade-trace")
def get_trade_trace(
    _user: RequireUser,
    position_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    """按 position_id / decision_id 查询一笔交易的风控、生命周期与复盘证据链。"""
    try:
        return _trade_trace(position_id=position_id, decision_id=decision_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.get("/trade-trace/recent")
def get_recent_trade_traces(_user: RequireUser, limit: int = 20) -> dict[str, Any]:
    return trade_traces_fact_payload(_recent_trade_trace_index(limit=limit))


@router.post("/var")
def calc_var(_user: RequireUser, req: VarRequest) -> dict[str, Any]:
    """
    计算并返回 VaR / CVaR.
    """
    return _var_calc.calculate(req.equity_series)


@router.get("/var")
def get_var_status(_user: RequireUser) -> dict[str, Any]:
    return _risk_component("var", _var_calc.get_status())


@router.post("/kelly")
def calc_kelly(_user: RequireUser, req: KellyRequest) -> dict[str, Any]:
    """
    计算 Kelly 最优下注比例。
    """
    return _kelly.calculate(req.win_rate, req.avg_win, req.avg_loss)


@router.get("/kelly")
def get_kelly_status(_user: RequireUser) -> dict[str, Any]:
    return _risk_component("kelly", _kelly.get_status())


@router.post("/stress/run")
def run_stress(_user: RequireUser, req: StressRequest) -> dict[str, Any]:
    """
    运行压力测试场景。
    """
    return _stress.run(req.positions, req.account, req.shocks)


@router.get("/stress")
def get_stress_status(_user: RequireUser) -> dict[str, Any]:
    return _risk_component("stress", _stress.get_status())


@router.post("/concentration")
def check_concentration(
    _user: RequireUser,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    检查因子/仓位集中度。
    weights: {因子名: 权重百分比}
    """
    return _conc.check(weights)


@router.get("/concentration")
def get_concentration_status(_user: RequireUser) -> dict[str, Any]:
    return _risk_component("concentration", _conc.get_status())
