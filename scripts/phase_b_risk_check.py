"""Phase B risk closure check.

Verifies the unified risk surface from either:
1. A running HTTP API (`/api/auth/login`, `/api/risk/summary`, `/api/risk/policy/verdicts`)
2. The local FastAPI app via TestClient

Typical usage:

    python scripts/phase_b_risk_check.py --local-testclient --username zhu --password xxx
    python scripts/phase_b_risk_check.py --api-base https://quant.example.com --username zhu --password xxx

Environment variables:

    QUANT_API_BASE
    QUANT_AUTH_USER
    QUANT_AUTH_PASSWORD
    QUANT_BEARER_TOKEN
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import error, request

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    token: str | None = None,
    timeout: float = 10.0,
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
        raise RuntimeError(f"login failed: status={status}, body={json.dumps(data, ensure_ascii=False)}")
    return str(data["token"])


def _remote_fetch(
    api_base: str,
    token: str,
    timeout: float,
    verdict_limit: int,
    position_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    base = api_base.rstrip("/")
    summary_status, summary = _http_json(
        "GET",
        f"{base}/api/risk/summary",
        token=token,
        timeout=timeout,
    )
    if summary_status != 200:
        raise RuntimeError(
            f"risk summary failed: status={summary_status}, body={json.dumps(summary, ensure_ascii=False)}"
        )
    verdict_status, verdicts = _http_json(
        "GET",
        f"{base}/api/risk/policy/verdicts?limit={int(verdict_limit)}",
        token=token,
        timeout=timeout,
    )
    if verdict_status != 200:
        raise RuntimeError(
            f"policy verdicts failed: status={verdict_status}, body={json.dumps(verdicts, ensure_ascii=False)}"
        )
    payload = {"summary": summary, "verdicts": verdicts}
    if position_id or decision_id:
        q = []
        if position_id:
            q.append(f"position_id={position_id}")
        if decision_id:
            q.append(f"decision_id={decision_id}")
        trace_status, trace = _http_json(
            "GET",
            f"{base}/api/risk/trade-trace?{'&'.join(q)}",
            token=token,
            timeout=timeout,
        )
        if trace_status != 200:
            raise RuntimeError(
                f"trade trace failed: status={trace_status}, body={json.dumps(trace, ensure_ascii=False)}"
            )
        payload["trade_trace"] = trace
    return payload


def _local_fetch(
    username: str,
    password: str | None,
    token: str | None,
    verdict_limit: int,
    position_id: str | None = None,
    decision_id: str | None = None,
) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from backend.app import app
    from backend.core.auth import create_token

    with TestClient(app) as client:
        effective_token = token
        if not effective_token:
            if password:
                resp = client.post("/api/auth/login", json={"username": username, "password": password})
                if resp.status_code != 200:
                    raise RuntimeError(
                        f"local login failed: status={resp.status_code}, body={resp.text[:500]}"
                    )
                effective_token = resp.json()["token"]
            else:
                effective_token = create_token(username)

        headers = {"Authorization": f"Bearer {effective_token}"}
        summary_resp = client.get("/api/risk/summary", headers=headers)
        if summary_resp.status_code != 200:
            raise RuntimeError(
                f"local risk summary failed: status={summary_resp.status_code}, body={summary_resp.text[:500]}"
            )
        verdict_resp = client.get(f"/api/risk/policy/verdicts?limit={int(verdict_limit)}", headers=headers)
        if verdict_resp.status_code != 200:
            raise RuntimeError(
                f"local policy verdicts failed: status={verdict_resp.status_code}, body={verdict_resp.text[:500]}"
            )
        payload = {"summary": summary_resp.json(), "verdicts": verdict_resp.json()}
        if position_id or decision_id:
            params = []
            if position_id:
                params.append(f"position_id={position_id}")
            if decision_id:
                params.append(f"decision_id={decision_id}")
            trace_resp = client.get(f"/api/risk/trade-trace?{'&'.join(params)}", headers=headers)
            if trace_resp.status_code != 200:
                raise RuntimeError(
                    f"local trade trace failed: status={trace_resp.status_code}, body={trace_resp.text[:500]}"
                )
            payload["trade_trace"] = trace_resp.json()
        return payload


def _top_items(counts: dict[str, int], limit: int = 5) -> list[dict[str, Any]]:
    pairs = sorted(((str(k), int(v)) for k, v in (counts or {}).items()), key=lambda item: (-item[1], item[0]))
    return [{"name": name, "count": count} for name, count in pairs[: max(0, int(limit))]]


def _build_report(summary: dict[str, Any], verdicts: dict[str, Any], trade_trace: dict[str, Any] | None = None) -> dict[str, Any]:
    policy = (summary or {}).get("policy") or verdicts or {}
    system_health = (summary or {}).get("system_health") or {}
    items = list((policy.get("items") or []))
    blocked_items = [item for item in items if not bool(item.get("allowed", False))]
    allowed_items = [item for item in items if bool(item.get("allowed", False))]
    report = {
        "ok": bool(system_health.get("overall")) and "system_health" in (summary or {}),
        "system_health": {
            "overall": system_health.get("overall", "unknown"),
            "overall_score": system_health.get("overall_score", 0.0),
            "critical_components": list(system_health.get("critical_components") or []),
            "degraded_components": list(system_health.get("degraded_components") or []),
            "errors": list(system_health.get("errors") or []),
        },
        "policy": {
            "total": int(policy.get("total", 0) or 0),
            "counts": dict(policy.get("counts") or {}),
            "top_block_reasons": _top_items(policy.get("by_reason") or {}),
            "top_actions": _top_items(policy.get("by_action") or {}),
            "latest_blocked": blocked_items[:5],
            "latest_allowed": allowed_items[:5],
        },
    }
    if trade_trace is not None:
        report["trade_trace"] = {
            "summary": trade_trace.get("summary") or {},
            "review": {
                "review_id": ((trade_trace.get("review") or {}).get("review_id") or ""),
                "outcome_label": ((trade_trace.get("review") or {}).get("outcome_label") or ""),
                "summary_text": ((trade_trace.get("review") or {}).get("summary_text") or ""),
            },
            "ledger_events": list(trade_trace.get("decision_ledger") or [])[-5:],
        }
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify Phase B unified risk closure.")
    parser.add_argument("--api-base", default=os.environ.get("QUANT_API_BASE", "").strip(), help="Remote API base URL")
    parser.add_argument(
        "--local-testclient",
        action="store_true",
        help="Use the local FastAPI app via TestClient instead of remote HTTP",
    )
    parser.add_argument("--username", default=os.environ.get("QUANT_AUTH_USER", "zhu").strip(), help="Login username")
    parser.add_argument("--password", default=os.environ.get("QUANT_AUTH_PASSWORD", ""), help="Login password")
    parser.add_argument("--token", default=os.environ.get("QUANT_BEARER_TOKEN", ""), help="Existing bearer token")
    parser.add_argument("--timeout", type=float, default=10.0, help="HTTP timeout in seconds")
    parser.add_argument("--verdict-limit", type=int, default=25, help="Recent verdict count to request")
    parser.add_argument("--position-id", default="", help="Optional position_id for trade trace lookup")
    parser.add_argument("--decision-id", default="", help="Optional decision_id for trade trace lookup")
    args = parser.parse_args()

    token = args.token.strip() or None
    password = args.password or None
    position_id = args.position_id.strip() or None
    decision_id = args.decision_id.strip() or None

    try:
        if args.local_testclient:
            payload = _local_fetch(args.username, password, token, args.verdict_limit, position_id, decision_id)
        else:
            if not args.api_base:
                raise RuntimeError("missing --api-base (or QUANT_API_BASE) for remote mode")
            if not token:
                if not password:
                    raise RuntimeError("missing password/token for remote mode")
                token = _remote_login(args.api_base, args.username, password, args.timeout)
            payload = _remote_fetch(args.api_base, token, args.timeout, args.verdict_limit, position_id, decision_id)
        report = _build_report(payload["summary"], payload["verdicts"], payload.get("trade_trace"))
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
