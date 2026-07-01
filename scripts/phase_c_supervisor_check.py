"""Phase C acceptance check for position supervisor scenarios.

Usage:
  python scripts/phase_c_supervisor_check.py
  python scripts/phase_c_supervisor_check.py --db data/state.db --limit 20
  python scripts/phase_c_supervisor_check.py --api-base https://www.zhuzhu666.icu --username zhu --password ****
  python scripts/phase_c_supervisor_check.py --direct-broker
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path  # noqa: E402


def _loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _use_pg(db_path: Path) -> bool:
    return is_state_db_path(db_path)


def _sql(conn, sql: str) -> str:
    return sql.replace("?", "%s") if conn.__class__.__module__.split(".", 1)[0] == "psycopg" else sql


def _execute(conn, sql: str, params: tuple | list | None = None):
    if params is None:
        return conn.execute(_sql(conn, sql))
    return conn.execute(_sql(conn, sql), tuple(params))


def _connect(db_path: Path):
    conn = get_state_pg_conn(read_only=True) if _use_pg(db_path) else connect_sqlite(db_path, read_only=True)
    if not _use_pg(db_path):
        conn.row_factory = sqlite3.Row
    return conn


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, dict[str, Any]]:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    req = request.Request(url, data=body, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
            return int(resp.status), json.loads(raw or "{}")
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw or "{}")
        except Exception:
            data = {"error": raw[:500]}
        return int(exc.code), data


def _remote_login(api_base: str, username: str, password: str, timeout: float) -> str:
    status, data = _http_json(
        "POST",
        f"{api_base.rstrip('/')}/api/auth/login",
        payload={"username": username, "password": password},
        timeout=timeout,
    )
    if status != 200 or not data.get("token"):
        raise RuntimeError(f"remote login failed: status={status}, body={json.dumps(data, ensure_ascii=False)}")
    return str(data["token"])


def _fetch_cases(conn: sqlite3.Connection, limit: int) -> list[dict]:
    rows = _execute(
        conn,
        """
        SELECT review_id, trade_id, position_id, outcome_label, pnl, mae, mfe, summary_text, review_json, created_at
        FROM trade_outcome_review
        ORDER BY created_at DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    cases: list[dict] = []
    for row in rows:
        review = _loads(row["review_json"], {})
        cases.append(
            {
                "review_id": str(row["review_id"] or ""),
                "trade_id": str(row["trade_id"] or ""),
                "position_id": str(row["position_id"] or ""),
                "outcome_label": str(row["outcome_label"] or ""),
                "pnl": float(row["pnl"] or 0.0),
                "mfe": float(row["mfe"] or 0.0),
                "mae": float(row["mae"] or 0.0),
                "close_reason": str(review.get("close_reason") or ""),
                "holding_seconds": float(review.get("holding_seconds") or 0.0),
                "giveback_ratio": float(review.get("giveback_ratio") or 0.0),
                "profit_capture_ratio": float(review.get("profit_capture_ratio") or 0.0),
                "holding_efficiency": float(review.get("holding_efficiency") or 0.0),
                "time_decay_score": float(review.get("time_decay_score") or 0.0),
                "thesis_status": str(review.get("thesis_status") or ""),
                "regime_shift": str(review.get("regime_shift") or ""),
                "close_reason_source": str(review.get("close_reason_source") or ""),
                "phase_c_diagnosis": review.get("phase_c_diagnosis") or {},
                "summary_text": str(row["summary_text"] or ""),
            }
        )
    return cases


def _fetch_remote_reviews(api_base: str, token: str, limit: int, timeout: float) -> list[dict]:
    status, data = _http_json(
        "GET",
        f"{api_base.rstrip('/')}/api/learning/reviews?limit={int(limit)}",
        token=token,
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"fetch remote reviews failed: status={status}, body={json.dumps(data, ensure_ascii=False)}")
    cases: list[dict] = []
    for item in data.get("items") or []:
        review = item.get("review") or {}
        cases.append(
            {
                "review_id": str(item.get("review_id") or ""),
                "trade_id": str(item.get("trade_id") or ""),
                "position_id": str(item.get("position_id") or ""),
                "outcome_label": str(item.get("outcome_label") or ""),
                "pnl": float(item.get("pnl") or 0.0),
                "mfe": float(item.get("mfe") or 0.0),
                "mae": float(item.get("mae") or 0.0),
                "close_reason": str(review.get("close_reason") or ""),
                "holding_seconds": float(review.get("holding_seconds") or 0.0),
                "giveback_ratio": float(review.get("giveback_ratio") or 0.0),
                "profit_capture_ratio": float(review.get("profit_capture_ratio") or 0.0),
                "holding_efficiency": float(review.get("holding_efficiency") or 0.0),
                "time_decay_score": float(review.get("time_decay_score") or 0.0),
                "thesis_status": str(review.get("thesis_status") or ""),
                "regime_shift": str(review.get("regime_shift") or ""),
                "close_reason_source": str(review.get("close_reason_source") or ""),
                "phase_c_diagnosis": review.get("phase_c_diagnosis") or {},
                "summary_text": str(item.get("summary_text") or ""),
            }
        )
    return cases


def _fetch_remote_open_positions(api_base: str, token: str, timeout: float) -> list[dict]:
    status, data = _http_json(
        "GET",
        f"{api_base.rstrip('/')}/api/live/positions?broker=ctrader",
        token=token,
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"fetch remote positions failed: status={status}, body={json.dumps(data, ensure_ascii=False)}")
    return list(data.get("positions") or [])


def _fetch_remote_policy_verdicts(api_base: str, token: str, limit: int, timeout: float) -> list[dict]:
    status, data = _http_json(
        "GET",
        f"{api_base.rstrip('/')}/api/risk/policy/verdicts?limit={int(limit)}",
        token=token,
        timeout=timeout,
    )
    if status != 200:
        raise RuntimeError(f"fetch remote policy verdicts failed: status={status}, body={json.dumps(data, ensure_ascii=False)}")
    return list(data.get("items") or [])


def _fetch_direct_broker_open_positions() -> list[dict]:
    from execution._env import load_env

    load_env()
    from config.runtime_config import shared
    from execution.ctrader_bridge import CTraderBridge
    from backend.services import live_service

    bridge = CTraderBridge(
        client_id=os.environ.get("CTRADER_CLIENT_ID", ""),
        client_secret=os.environ.get("CTRADER_CLIENT_SECRET", ""),
        access_token=os.environ.get("CTRADER_ACCESS_TOKEN", ""),
        account_id=int(os.environ.get("CTRADER_ACCOUNT_ID", "0") or 0),
    )
    if not bridge.connect():
        raise RuntimeError("direct broker connect failed")
    try:
        positions = bridge.get_positions()
        enriched = live_service._enrich_positions_with_path_metrics(
            positions,
            cfg=shared(),
            persist=False,
            broker="ctrader",
            strategy_name="factor_v4",
        )
        return [dict(item) for item in enriched]
    finally:
        bridge.disconnect()


def _bucket(case: dict) -> list[str]:
    tags: list[str] = []
    close_reason_source = str(case.get("close_reason_source") or "")
    if case["holding_seconds"] >= 6 * 3600:
        tags.append("long_hold")
    if case["mfe"] > 0 and case["giveback_ratio"] >= 0.5:
        tags.append("profit_giveback")
    if case["close_reason"] in {"manual", "broker_close", "manual_close"}:
        tags.append("manual_close")
    if case["close_reason"] == "holding_timeout":
        tags.append("timeout_close")
        if close_reason_source == "phase_c_inferred":
            tags.append("inferred_timeout_close")
    if case["close_reason"] in {"supervisor_tighten", "supervisor_reduce", "thesis_broken"}:
        tags.append("active_protection")
    elif case["close_reason"] == "profit_giveback_after_mfe":
        tags.append("inferred_active_protection")
    return tags


def _open_position_bucket(position: dict) -> list[str]:
    tags: list[str] = []
    holding_seconds = float(position.get("holding_seconds") or 0.0)
    timeout_ratio = float(position.get("holding_timeout_ratio") or 0.0)
    supervisor_action = str(position.get("supervisor_action") or "")
    supervisor_reason = str(position.get("supervisor_reason") or "")
    if holding_seconds >= 6 * 3600:
        tags.append("long_hold")
    if timeout_ratio >= 0.8:
        tags.append("timeout_watch")
    if supervisor_action == "close":
        tags.append("supervisor_close")
    elif supervisor_action == "reduce":
        tags.append("supervisor_reduce")
    elif supervisor_action == "tighten":
        tags.append("supervisor_tighten")
    if supervisor_reason == "thesis_broken":
        tags.append("thesis_broken")
    return tags


def _event_bucket(event: dict) -> list[str]:
    tags: list[str] = []
    event_type = str(event.get("event_type") or "")
    action = str(event.get("action") or "")
    audit_payload = event.get("risk_verdict", {}).get("audit_payload") or {}
    close_reason = str(audit_payload.get("close_reason") or "")

    if close_reason == "holding_timeout":
        tags.append("timeout_close")
    if event_type in {"supervisor_close", "supervisor_reduce", "supervisor_tighten"}:
        tags.append("active_protection")
    elif action in {"reduce_position", "tighten_position"}:
        tags.append("active_protection")
    elif close_reason in {"thesis_broken", "profit_giveback_after_mfe"}:
        tags.append("active_protection")
    return tags


def _build_coverage(
    summary: dict[str, int],
    cases: list[dict],
    open_positions: list[dict],
    pending_events: list[dict],
) -> dict[str, dict[str, Any]]:
    def _append_unique(target: list[str], value: str, limit: int = 3) -> None:
        if value and value not in target and len(target) < limit:
            target.append(value)

    coverage = {
        "long_hold_case": {
            "covered": bool(summary.get("long_hold") or summary.get("open_long_hold_positions")),
            "required": "近期长持仓样本",
            "evidence": [],
        },
        "profit_giveback_case": {
            "covered": bool(summary.get("profit_giveback")),
            "required": "曾盈利后回吐样本",
            "evidence": [],
        },
        "manual_close_case": {
            "covered": bool(summary.get("manual_close")),
            "required": "手动关闭 / broker close 样本",
            "evidence": [],
        },
        "timeout_close_case": {
            "covered": bool(summary.get("timeout_close") or summary.get("open_timeout_watch_positions")),
            "required": "超时关闭或接近超时样本",
            "evidence": [],
            "inferred_evidence": [],
        },
        "active_protection_case": {
            "covered": bool(
                summary.get("active_protection")
                or summary.get("open_supervisor_close_positions")
                or summary.get("open_supervisor_reduce_positions")
                or summary.get("open_supervisor_tighten_positions")
            ),
            "required": "主动保护关闭/减仓/收紧样本",
            "evidence": [],
            "inferred_evidence": [],
        },
    }
    for case in cases:
        tags = set(case.get("tags") or [])
        position_id = str(case.get("position_id") or "")
        if "long_hold" in tags:
            _append_unique(coverage["long_hold_case"]["evidence"], position_id)
        if "profit_giveback" in tags:
            _append_unique(coverage["profit_giveback_case"]["evidence"], position_id)
        if "manual_close" in tags:
            _append_unique(coverage["manual_close_case"]["evidence"], position_id)
        if "timeout_close" in tags:
            _append_unique(coverage["timeout_close_case"]["evidence"], position_id)
        if "inferred_timeout_close" in tags:
            _append_unique(coverage["timeout_close_case"]["inferred_evidence"], position_id)
        if "active_protection" in tags:
            _append_unique(coverage["active_protection_case"]["evidence"], position_id)
        if "inferred_active_protection" in tags:
            _append_unique(coverage["active_protection_case"]["inferred_evidence"], position_id)
    for pos in open_positions:
        tags = set(pos.get("tags") or [])
        position_id = str(pos.get("position_id") or "")
        action = str(pos.get("supervisor_action") or "")
        if "long_hold" in tags:
            _append_unique(coverage["long_hold_case"]["evidence"], f"open:{position_id}")
        if "timeout_watch" in tags:
            _append_unique(coverage["timeout_close_case"]["evidence"], f"open:{position_id}")
        if action in {"close", "reduce", "tighten"}:
            _append_unique(coverage["active_protection_case"]["evidence"], f"open:{position_id}:{action}")
    for event in pending_events:
        tags = set(event.get("tags") or [])
        position_id = str(event.get("position_id") or "")
        event_type = str(event.get("event_type") or "")
        close_reason = str(event.get("close_reason") or "")
        if "timeout_close" in tags:
            _append_unique(coverage["timeout_close_case"]["evidence"], f"event:{position_id}:{close_reason or event_type}")
        if "active_protection" in tags:
            _append_unique(coverage["active_protection_case"]["evidence"], f"event:{position_id}:{close_reason or event_type}")
    return coverage


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase C supervisor acceptance check.")
    parser.add_argument("--db", default=str(STATE_DB), help="State DB path; defaults to PostgreSQL state when enabled")
    parser.add_argument("--limit", type=int, default=30, help="Number of recent reviews to inspect")
    parser.add_argument("--api-base", default=os.environ.get("QUANT_API_BASE", "").strip(), help="Remote API base URL")
    parser.add_argument("--username", default=os.environ.get("QUANT_AUTH_USER", "zhu").strip(), help="Remote login username")
    parser.add_argument("--password", default=os.environ.get("QUANT_AUTH_PASSWORD", ""), help="Remote login password")
    parser.add_argument("--token", default=os.environ.get("QUANT_BEARER_TOKEN", ""), help="Existing bearer token")
    parser.add_argument("--timeout", type=float, default=15.0, help="Remote request timeout")
    parser.add_argument("--direct-broker", action="store_true", help="Fetch current open positions directly from local cTrader credentials")
    args = parser.parse_args()
    source = "local_db"
    open_positions: list[dict] = []
    pending_events: list[dict] = []

    if args.api_base:
        source = "remote_api"
        token = args.token.strip() or _remote_login(args.api_base, args.username, args.password, args.timeout)
        cases = _fetch_remote_reviews(args.api_base, token, args.limit, args.timeout)
        open_positions = _fetch_remote_open_positions(args.api_base, token, args.timeout)
        pending_events = _fetch_remote_policy_verdicts(args.api_base, token, args.limit, args.timeout)
    elif args.direct_broker:
        source = "direct_broker"
        cases = []
        open_positions = _fetch_direct_broker_open_positions()
    else:
        db_path = Path(args.db)
        if not _use_pg(db_path) and not db_path.exists():
            print(f"db_not_found: {db_path}")
            return 1
        conn = _connect(db_path)
        try:
            cases = _fetch_cases(conn, args.limit)
        finally:
            conn.close()

    if not cases and not open_positions and not pending_events:
        print("no_review_cases: 当前数据源里还没有可用于 Phase C 验收的样本。")
        return 2

    bucketed = []
    summary_counts = {
        "long_hold": 0,
        "profit_giveback": 0,
        "manual_close": 0,
        "timeout_close": 0,
        "active_protection": 0,
        "inferred_timeout_close": 0,
        "inferred_active_protection": 0,
        "open_long_hold_positions": 0,
        "open_timeout_watch_positions": 0,
        "open_supervisor_close_positions": 0,
        "open_supervisor_reduce_positions": 0,
        "open_supervisor_tighten_positions": 0,
    }
    for case in cases:
        tags = _bucket(case)
        for tag in tags:
            summary_counts[tag] += 1
        bucketed.append({**case, "tags": tags})

    open_position_items = []
    for pos in open_positions:
        holding_seconds = float(pos.get("holding_seconds") or 0.0)
        tags = _open_position_bucket(pos)
        if "long_hold" in tags:
            summary_counts["open_long_hold_positions"] += 1
        if "timeout_watch" in tags:
            summary_counts["open_timeout_watch_positions"] += 1
        if "supervisor_close" in tags:
            summary_counts["open_supervisor_close_positions"] += 1
        if "supervisor_reduce" in tags:
            summary_counts["open_supervisor_reduce_positions"] += 1
        if "supervisor_tighten" in tags:
            summary_counts["open_supervisor_tighten_positions"] += 1
        open_position_items.append(
            {
                "position_id": str(pos.get("position_id") or pos.get("ticket") or ""),
                "symbol": str(pos.get("symbol") or ""),
                "direction": str(pos.get("type") or ""),
                "pnl": float(pos.get("pnl", pos.get("profit", 0.0)) or 0.0),
                "holding_seconds": holding_seconds,
                "holding_timeout_ratio": float(pos.get("holding_timeout_ratio") or 0.0),
                "mfe": float(pos.get("mfe") or 0.0),
                "mae": float(pos.get("mae") or 0.0),
                "giveback_ratio": float(pos.get("giveback_ratio") or 0.0),
                "profit_capture_ratio": float(pos.get("profit_capture_ratio") or 0.0),
                "holding_efficiency": float(pos.get("holding_efficiency") or 0.0),
                "time_decay_score": float(pos.get("time_decay_score") or 0.0),
                "thesis_status": str(pos.get("thesis_status") or ""),
                "regime_shift": str(pos.get("regime_shift") or ""),
                "supervisor_action": str(pos.get("supervisor_action") or ""),
                "supervisor_reason": str(pos.get("supervisor_reason") or ""),
                "supervisor_summary": str(pos.get("supervisor_summary") or ""),
                "tags": tags,
            }
        )

    pending_event_items = []
    for event in pending_events:
        audit_payload = event.get("risk_verdict", {}).get("audit_payload") or {}
        tags = _event_bucket(event)
        if "timeout_close" in tags:
            summary_counts["timeout_close"] += 1
        if "active_protection" in tags:
            summary_counts["active_protection"] += 1
        pending_event_items.append(
            {
                "decision_id": str(event.get("decision_id") or ""),
                "event_type": str(event.get("event_type") or ""),
                "action": str(event.get("action") or ""),
                "position_id": str(audit_payload.get("position_id") or ""),
                "close_reason": str(audit_payload.get("close_reason") or ""),
                "holding_seconds": float(audit_payload.get("holding_seconds") or 0.0),
                "allowed": bool(event.get("allowed")),
                "tags": tags,
            }
        )

    print(
        json.dumps(
            {
                "source": source,
                "summary": summary_counts,
                "coverage": _build_coverage(summary_counts, bucketed, open_position_items, pending_event_items),
                "open_positions": open_position_items,
                "pending_events": pending_event_items,
                "cases": bucketed,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
