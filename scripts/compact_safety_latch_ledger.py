"""Compact the append-only no-new-risk latch ledger without changing its state.

The ledger (``data/safety/no_new_risk_latch.jsonl``) is the no-new-risk latch
authority.  It is append-only by design, so it only ever grows: by 2026-09-10
it held 3.94GB / 13,556 records, 10,260 of them carrying a ~400KB overlay
authority report that a failed refresh persisted on every poll.

Compaction keeps the fold result identical (active causes + the last valid
record), archives the original verbatim under ``data/safety/archive/`` and
rewrites the ledger with only the records needed to reproduce that state.
The archive is never deleted: it is the historical latch evidence.

Usage:
    python scripts/compact_safety_latch_ledger.py            # dry run
    python scripts/compact_safety_latch_ledger.py --apply    # archive + rewrite
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backend.services import live_safety_state as lss  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
STATE_DIR = REPO_ROOT / "data" / "safety"
ARCHIVE_DIR = STATE_DIR / "archive"


def _fold(records: Iterable[Any]) -> tuple[dict, bool, dict, int]:
    """Fold latch records exactly as the reader does (same helper, same order)."""

    active: dict[tuple[str, str], dict[str, Any]] = {}
    legacy_records = False
    latest: dict[str, Any] = {}
    count = 0
    for payload in records:
        if not isinstance(payload, dict) or payload.get("schema_version") != lss._LATCH_SCHEMA_V1:
            continue
        latest = payload
        legacy_records = lss._apply_latch_event(active, payload) or legacy_records
        count += 1
    return active, legacy_records, latest, count


def _ledger_records(handle: Iterable[bytes]) -> Iterator[dict[str, Any]]:
    for raw in handle:
        if not raw.strip():
            continue
        yield json.loads(raw.decode("utf-8"))


def _state_key(state: tuple[dict, bool, dict]) -> str:
    active, legacy_records, latest = state
    return json.dumps(
        {
            "active": sorted(
                [[cause, cause_id, record] for (cause, cause_id), record in active.items()],
                key=lambda item: (item[0], item[1]),
            ),
            "legacy": bool(legacy_records),
            "latest": latest,
        },
        sort_keys=True,
        default=str,
    )


def _compact_records(state: tuple[dict, bool, dict]) -> list[dict[str, Any]]:
    """Records needed to reproduce ``state`` from an empty ledger."""

    active, _legacy, latest = state
    records = [dict(record) for _key, record in sorted(active.items())]
    if latest:
        if not records or str(records[-1].get("event_id") or "") != str(latest.get("event_id") or ""):
            records.append(dict(latest))
    for record in records:
        if record.get("schema_version") != lss._LATCH_SCHEMA_V1:
            raise SystemExit("refusing to compact: record without the latch schema")
    return records


def _write_records(path: Path, records: Iterable[dict[str, Any]]) -> int:
    temporary = path.with_name(path.name + ".tmp")
    written = 0
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as handle:
        for record in records:
            line = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            handle.write(line)
            written += len(line.encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    return written


def _archive(ledger: Path, stamp: str) -> tuple[Path, int, int, bool]:
    """Stream-copy the ledger into a gzip archive; returns (path, bytes, lines, stable)."""

    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    target = ARCHIVE_DIR / f"no_new_risk_latch.{stamp}.jsonl.gz"
    size_before = ledger.stat().st_size
    lines = 0
    with ledger.open("rb") as source, open(target, "wb") as raw_sink:
        with gzip.GzipFile(fileobj=raw_sink, mode="wb", compresslevel=6, mtime=0) as sink:
            for raw in source:
                sink.write(raw)
                lines += 1
        raw_sink.flush()
        os.fsync(raw_sink.fileno())
    size_after = ledger.stat().st_size
    return target, size_before, lines, size_before == size_after


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="archive and rewrite the ledger")
    args = parser.parse_args()

    ledger = lss.safety_latch_path()
    if not ledger.exists():
        print(f"no ledger at {ledger}; nothing to compact")
        return 0

    size = ledger.stat().st_size
    print(f"ledger: {ledger}  {size / 1048576:.1f}MB")

    with ledger.open("rb") as handle:
        reference = _fold(_ledger_records(handle))
    active, legacy_records, latest, count = reference
    print(f"records: {count}  active causes: {len(active)}  legacy: {legacy_records}")
    if legacy_records:
        print("refusing to compact: legacy records would not survive the rewrite")
        return 2

    compacted = _compact_records(reference[0:3])
    planned = _fold(compacted)[0:3]
    if _state_key(planned) != _state_key(reference[0:3]):
        print("refusing to compact: rewritten records fold to a different state")
        return 2
    planned_bytes = sum(len(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)) + 1 for item in compacted)
    print(f"planned rewrite: {len(compacted)} record(s), {planned_bytes} bytes")

    if not args.apply:
        print("dry run only; re-run with --apply")
        return 0

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    for attempt in range(5):
        archive, archived_bytes, archived_lines, stable = _archive(ledger, stamp)
        if stable:
            break
        print(f"ledger grew during archiving (attempt {attempt + 1}); retrying")
    else:
        print("refusing to compact: ledger keeps growing while archiving")
        return 3

    with gzip.open(archive, "rb") as handle:
        archived_state = _fold(_ledger_records(handle))
    if _state_key(archived_state[0:3]) != _state_key(reference[0:3]) or archived_state[3] != count:
        print("refusing to compact: archive does not fold to the same state")
        return 3
    print(f"archive: {archive}  {archive.stat().st_size / 1048576:.1f}MB (from {archived_bytes / 1048576:.1f}MB, {archived_lines} lines, verified)")

    written = _write_records(ledger, compacted)
    cursor = lss._latch_replay_checkpoint_path()
    if cursor.exists():
        cursor.unlink()

    with ledger.open("rb") as handle:
        final = _fold(_ledger_records(handle))
    if _state_key(final[0:3]) != _state_key(reference[0:3]):
        print("ERROR: rewritten ledger folds to a different state; archive kept")
        return 4

    lss._invalidate_latch_cache()
    print(f"rewritten: {ledger}  {written} bytes (was {size} bytes)")
    print("keep the archive: it is the only copy of the historical latch evidence")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
