#!/usr/bin/env python3
"""Canonical_v2 projection rebuild minimal pilot (read-only except projection_run).

Rebuilds a small, deterministic, canonical-only fact view projection:

    "decision -> trade_outcome fact view"
    for every canonical trade_review event: outcome fields (review payload) joined
    through event_relation(derived_from) to its entry risk_decision event, taking
    decision identity + symbol/timeframe/regime/action fields from the decision
    payload.  Everything is restored from canonical_v2 (payload blob -> event ->
    relation); state_v1 is only used in --verify/--reconcile as an independent
    cross-check and is never written.

Rebuild semantics:
    - writes ONLY a canonical_v2.projection_run audit record (run_kind=projection)
      via start/finish_projection_run (idempotent by run identity).
    - produces the materialized projection to a JSON file (no new canonical table,
      no old projection table is touched) -> the projection is deletable-by-absence
      and fully rebuildable from canonical facts.
    - same input/watermark rerun yields the identical output digest (idempotent).

Modes:
    --rebuild   compute projection, write projection_run(completed), write file.
    --verify    recompute projection, compare against recorded output_digest.
    --reconcile read-only cross-check vs state_v1 (count + key set + digest).
    (default dry-run: compute plane + digests, no writes)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import (  # noqa: E402
    _db_time,
    _utc,
    finish_projection_run,
    read_payload,
    start_projection_run,
)


def _code_version() -> str:
    """Git short HEAD (or ``workspace@main``) stamped on projection runs."""
    try:
        import subprocess
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        )
        if head.returncode == 0 and head.stdout.strip():
            return "git:" + head.stdout.strip()
    except Exception:  # noqa: BLE001
        pass
    return "workspace@main"


PROJECTION_NAME = "canonical_v2.trade_outcome_fact_view.v1"


def _reviews_events(conn: Any) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_id, entity_id, payload_hash, observed_at FROM canonical_v2.event WHERE event_type='trade_review' ORDER BY observed_at ASC, event_id ASC"
    ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        events.append(
            {
                "event_id": str(row["event_id"]),
                "entity_id": str(row["entity_id"] or ""),
                "payload_hash": str(row["payload_hash"] or ""),
                "observed_at": row["observed_at"],
            }
        )
    return events


def _decision_events(conn: Any) -> dict[str, dict[str, Any]]:
    rows = conn.execute(
        "SELECT event_id, entity_id, payload_hash FROM canonical_v2.event WHERE event_type='risk_decision'"
    ).fetchall()
    return {
        str(row["event_id"]): {"entity_id": str(row["entity_id"] or ""), "payload_hash": str(row["payload_hash"] or "")}
        for row in rows
    }


def _derived_relations(conn: Any) -> list[tuple[str, str]]:
    rows = conn.execute(
        "SELECT from_event_id, to_event_id FROM canonical_v2.event_relation WHERE relation_type='derived_from'"
    ).fetchall()
    return [(str(r["from_event_id"]), str(r["to_event_id"])) for r in rows]


def _build_projection(conn: Any) -> tuple[list[dict[str, Any]], str, str]:
    reviews = {e["event_id"]: e for e in _reviews_events(conn)}
    decisions = _decision_events(conn)
    relations = _derived_relations(conn)
    review_payloads: dict[str, Any] = {}
    payload_cache: dict[str, Any] = {}

    def _restore(payload_hash: str) -> Any:
        if payload_hash not in payload_cache:
            payload_cache[payload_hash] = read_payload(conn, payload_hash)
        return payload_cache[payload_hash]

    rows: list[dict[str, Any]] = []
    for review_event_id, decision_event_id in relations:
        review = reviews.get(review_event_id)
        decision = decisions.get(decision_event_id)
        if review is None or decision is None:
            continue
        rp = _restore(decision["payload_hash"]) or {}
        rpv = _restore(review["payload_hash"]) or {}
        rows.append(
            {
                "review_id": review["entity_id"],
                "entry_decision_id": decision["entity_id"],
                "outcome_label": str(rpv.get("outcome_label") or ""),
                "pnl": _num(rpv.get("pnl")),
                "trade_id": str(rpv.get("trade_id") or ""),
                "review_created_at": _iso(rpv.get("created_at")),
                "decision_symbol": str(rp.get("symbol") or ""),
                "decision_timeframe": str(rp.get("timeframe") or ""),
                "decision_regime_id": str(rp.get("regime_id") or ""),
                "decision_action_score": _num(rp.get("action_score")),
                "decision_event_type": str(rp.get("event_type") or ""),
            }
        )
    rows.sort(key=lambda r: (r.get("review_created_at") or "", r.get("review_id") or ""))
    digest = _projection_digest(rows)
    watermark = _watermark(reviews, decisions, relations)
    return rows, digest, watermark


def _projection_digest(rows: Iterable[Mapping[str, Any]]) -> str:
    digest = hashlib.sha256()
    for row in rows:
        digest.update(json.dumps(row, ensure_ascii=False, sort_keys=True).encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def _watermark(reviews: Mapping[str, Any], decisions: Mapping[str, Any], relations: list[tuple[str, str]]) -> str:
    parts = [
        f"trade_review={len(reviews)}",
        f"risk_decision={len(decisions)}",
        f"derived_from={len(relations)}",
    ]
    return "|".join(parts)


def _num(value: Any) -> float:
    try:
        return float(value) if value is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _iso(value: Any) -> str:
    if value is None:
        return ""
    try:
        return _utc(value).isoformat()
    except Exception:  # noqa: BLE001
        return str(value)


def _reconcile_source(conn: Any, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Independent cross-check against state_v1 (read-only)."""
    source_rows = conn.execute(
        """
        SELECT r.review_id AS review_id, d.decision_id AS decision_id,
               r.outcome_label AS outcome_label, r.pnl AS pnl,
               r.trade_id AS trade_id, d.symbol AS symbol
        FROM state_v1.trade_outcome_review r
        LEFT JOIN state_v1.decision_ledger d ON d.decision_id=r.entry_decision_id
        WHERE NULLIF(r.entry_decision_id,'') IS NOT NULL
        """,
    ).fetchall()
    src_map = {
        f"{str(r['review_id']) or ''}|{str(r['decision_id']) or ''}": r
        for r in source_rows
        if str(r["review_id"] or "") and str(r["decision_id"] or "")
    }
    matched = 0
    mismatched: list[dict[str, Any]] = []
    for row in rows:
        key = f"{row['review_id']}|{row['entry_decision_id']}"
        src = src_map.get(key)
        if src is None:
            mismatched.append({"key": key, "reason": "missing_in_source"})
            continue
        if (str(src["outcome_label"] or "") == row["outcome_label"]
                and abs(float(src["pnl"] or 0.0) - float(row.get("pnl") or 0.0)) < 1e-6
                and str(src["symbol"] or "") == row["decision_symbol"]):
            matched += 1
        else:
            mismatched.append(
                {
                    "key": key,
                    "reason": "field_mismatch",
                    "canonical": {"outcome_label": row["outcome_label"], "pnl": row["pnl"], "symbol": row["decision_symbol"]},
                    "source": {"outcome_label": str(src["outcome_label"] or ""), "pnl": float(src["pnl"] or 0.0), "symbol": str(src["symbol"] or "")},
                }
            )
    return {"source_keys": len(src_map), "matched": matched, "mismatched": len(mismatched), "sample": mismatched[:10]}


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical_v2 projection rebuild minimal pilot")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rebuild", action="store_true", help="Compute projection and write a completed projection_run record.")
    group.add_argument("--verify", action="store_true", help="Recompute and compare against last projection_run output_digest.")
    group.add_argument("--reconcile", action="store_true", help="Cross-check rebuilt rows against state_v1 (read-only).")
    parser.add_argument("--projection-run-id", default="canonical_v2_trade_outcome_view_pilot")
    parser.add_argument("--output", default="/var/tmp/canonical_v2_trade_outcome_fact_view.json")
    args = parser.parse_args()

    conn = None
    try:
        write = bool(args.rebuild)
        conn = get_state_pg_conn(read_only=not write)
        rows, digest, watermark = _build_projection(conn)

        if args.verify:
            rec = conn.execute(
                "SELECT output_digest, status, projection_name FROM canonical_v2.projection_run WHERE projection_run_id=%s",
                (args.projection_run_id,),
            ).fetchone()
            match = bool(rec and str(rec["output_digest"]) == digest and str(rec["status"]) == "completed")
            print(
                json.dumps(
                    {
                        "schema_version": "canonical_v2_projection_rebuild.verify.v1",
                        "ok": match,
                        "rows": len(rows),
                        "digest": digest,
                        "recorded": str(rec["output_digest"]) if rec else None,
                        "watermark": watermark,
                        "writes_performed": False,
                    },
                    ensure_ascii=False, sort_keys=True,
                )
            )
            return 0 if match else 2

        if args.reconcile:
            result = _reconcile_source(conn, rows)
            result.update({"rows": len(rows), "digest": digest, "writes_performed": False, "schema_version": "canonical_v2_projection_rebuild.reconcile.v1"})
            print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
            return 0

        if args.rebuild:
            before = conn.execute("SELECT count(*) AS n FROM canonical_v2.projection_run").fetchone()["n"]
            start_projection_run(
                conn,
                projection_run_id=args.projection_run_id,
                run_kind="projection",
                projection_name=PROJECTION_NAME,
                source_watermark=watermark,
                code_version=_code_version(),
                input_digest=watermark,
                started_at=_db_time(conn, None),
            )
            finish_projection_run(
                conn,
                projection_run_id=args.projection_run_id,
                status="completed",
                output_digest=digest,
            )
            conn.commit()
            out_path = Path(args.output)
            out_path.write_text(json.dumps({**{"watermark": watermark, "rows": rows}, "digest": digest}, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
            print(
                json.dumps(
                    {
                        "schema_version": "canonical_v2_projection_rebuild.rebuild.v1",
                        "ok": True,
                        "rows": len(rows),
                        "digest": digest,
                        "watermark": watermark,
                        "projection_run_id": args.projection_run_id,
                        "projection_run_before": before,
                        "output_file": str(out_path),
                        "writes_performed": True,
                    },
                    ensure_ascii=False, sort_keys=True,
                )
            )
            return 0

        # dry-run
        print(
            json.dumps(
                {
                    "schema_version": "canonical_v2_projection_rebuild.plan.v1",
                    "mode": "dry_run",
                    "rows": len(rows),
                    "digest": digest,
                    "watermark": watermark,
                    "writes_performed": False,
                },
                ensure_ascii=False, indent=2, sort_keys=True,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"schema_version": "canonical_v2_projection_rebuild.error.v1", "ok": False, "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
