#!/usr/bin/env python3
"""Canonical_v2 position->entry decision index projection (A2/A3 materialized file).

Builds a small, deterministic, canonical-only projection consumed by the
live hot path (``live_service``) and the ledger (``get_latest_entry_decision``):

    for every canonical risk_decision event with event_type='open' and a
    non-empty position_id: keep the NEWEST decision per position (ordered by
    decision_ts, then event_id), recording:

        position_id -> {decision_id, parent_decision_id, decision_ts,
                        timeframe, event_id}

    parent_decision_id = payload action.parent_decision_id (when present),
    which is what the live loop's entry-decision lookup actually returns.

Materialization:
    - writes ONLY a canonical_v2.projection_run audit record
      (run_kind=projection, idempotent by run identity) and a JSON file
      (deletable-by-absence, fully rebuildable from canonical facts).
    - same canonical input rerun yields the identical output digest.

Modes:
    --rebuild   compute index, write projection_run(completed), write file.
    --verify    recompute and compare against recorded output_digest + file.
    --rollback  remove the projection_run record and the output file
                (checkpoint guarded).
    (default dry-run: compute + digests, no writes)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Mapping

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2 import (  # noqa: E402
    _db_time,
    finish_projection_run,
    start_projection_run,
)
from backend.services.canonical_v2_reader import iter_decision_rows  # noqa: E402
from backend.services.state_payloads import stable_json  # noqa: E402


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


PROJECTION_NAME = "canonical_v2.position_decision_index.v1"
DEFAULT_OUTPUT = "/home/ubuntu/quant_trading/run_artifacts/canonical_v2_position_decision_index.json"
SCHEMA_VERSION = "position_decision_index.v1"
# same-bar dedup window used by the live hot path (seconds)
BAR_DEDUP_WINDOW_SEC = 5.0


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _text(value: Any) -> str:
    return str(value or "")


def _build_entries(conn: Any) -> tuple[dict[str, dict[str, Any]], str, str]:
    """Return (entries, watermark, digest) from canonical risk_decision events."""
    entries: dict[str, dict[str, Any]] = {}
    count = 0
    open_count = 0
    digest = hashlib.sha256()
    for row in iter_decision_rows(conn, limit=0):
        count += 1
        event_type = _text(row.get("event_type"))
        position_id = _text(row.get("position_id"))
        if event_type != "open" or not position_id:
            continue
        decision_id = _text(row.get("decision_id"))
        decision_ts = _num(row.get("decision_ts"))
        action = _json_load(row.get("action_json"))
        parent_decision_id = _text((action or {}).get("parent_decision_id"))
        entry = {
            "decision_id": decision_id,
            "parent_decision_id": parent_decision_id or "",
            "decision_ts": decision_ts,
            "timeframe": _text(row.get("timeframe")),
            "event_id": _text(row.get("event_id")),
        }
        existing = entries.get(position_id)
        if existing is None or (decision_ts, _text(entry["event_id"])) > (
            _num(existing.get("decision_ts")),
            _text(existing.get("event_id")),
        ):
            entries[position_id] = entry
            open_count += 1
    digest.update(stable_json(entries).encode("utf-8"))
    watermark = f"risk_decision={count} open_with_position={open_count} positions={len(entries)}"
    return entries, watermark, digest.hexdigest()


def _json_load(raw: Any) -> Any:
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {}


def _projection_run_exists(conn: Any, projection_run_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM canonical_v2.projection_run WHERE projection_run_id=%s",
        (projection_run_id,),
    ).fetchone()
    return row is not None


def main() -> int:
    parser = argparse.ArgumentParser(description="Canonical_v2 position->entry decision index projection")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--rebuild", action="store_true")
    group.add_argument("--verify", action="store_true")
    group.add_argument("--rollback", action="store_true")
    parser.add_argument("--projection-run-id", default="canonical_v2_position_decision_index_20260816")
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    conn = None
    try:
        write = bool(args.rebuild or args.rollback)
        conn = get_state_pg_conn(read_only=not write)

        if args.rollback:
            out_path = Path(args.output)
            if out_path.exists():
                out_path.unlink()
            conn.execute(
                "DELETE FROM canonical_v2.projection_run WHERE projection_run_id=%s",
                (args.projection_run_id,),
            )
            conn.commit()
            print(
                json.dumps(
                    {
                        "schema_version": "canonical_v2_position_decision_index.rollback.v1",
                        "ok": True,
                        "projection_run_id": args.projection_run_id,
                        "output_removed": str(out_path),
                        "writes_performed": True,
                    },
                    ensure_ascii=False, sort_keys=True,
                )
            )
            return 0

        entries, watermark, digest = _build_entries(conn)

        if args.verify:
            out_path = Path(args.output)
            file_digest = None
            if out_path.exists():
                try:
                    payload = json.loads(out_path.read_text(encoding="utf-8"))
                    file_digest = str(payload.get("digest") or "")
                except Exception:  # noqa: BLE001
                    file_digest = None
            rec = conn.execute(
                "SELECT output_digest, status FROM canonical_v2.projection_run WHERE projection_run_id=%s",
                (args.projection_run_id,),
            ).fetchone()
            recorded = str(rec["output_digest"]) if rec else None
            ok = bool(
                digest == file_digest
                and recorded == digest
                and (rec is None or str(rec["status"]) == "completed")
            )
            print(
                json.dumps(
                    {
                        "schema_version": "canonical_v2_position_decision_index.verify.v1",
                        "ok": ok,
                        "entries": len(entries),
                        "digest": digest,
                        "file_digest": file_digest,
                        "recorded": recorded,
                        "watermark": watermark,
                        "writes_performed": False,
                    },
                    ensure_ascii=False, indent=2, sort_keys=True,
                )
            )
            return 0 if ok else 2

        if args.rebuild:
            before = int(
                conn.execute("SELECT count(*) AS n FROM canonical_v2.projection_run").fetchone()["n"] or 0
            )
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
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(
                json.dumps(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "built_at": _db_time(conn, None).isoformat(),
                        "watermark": watermark,
                        "digest": digest,
                        "projection_run_id": args.projection_run_id,
                        "entries": entries,
                    },
                    ensure_ascii=False, indent=2, sort_keys=True,
                ),
                encoding="utf-8",
            )
            print(
                json.dumps(
                    {
                        "schema_version": "canonical_v2_position_decision_index.rebuild.v1",
                        "ok": True,
                        "entries": len(entries),
                        "digest": digest,
                        "watermark": watermark,
                        "projection_run_id": args.projection_run_id,
                        "projection_run_before": before,
                        "output_file": str(out_path),
                        "writes_performed": True,
                    },
                    ensure_ascii=False, indent=2, sort_keys=True,
                )
            )
            return 0

        # dry-run
        print(
            json.dumps(
                {
                    "schema_version": "canonical_v2_position_decision_index.plan.v1",
                    "mode": "dry_run",
                    "entries": len(entries),
                    "digest": digest,
                    "watermark": watermark,
                    "writes_performed": False,
                },
                ensure_ascii=False, indent=2, sort_keys=True,
            )
        )
        return 0
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                {
                    "schema_version": "canonical_v2_position_decision_index.error.v1",
                    "ok": False,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
                ensure_ascii=False, sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2
    finally:
        if conn is not None:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
