#!/usr/bin/env python3
"""Weekly invariant sweep: cross-source consistency checks (read-only).

Born from the 2026-08-29..09-05 bug week: every check below encodes one bug
class that already fired in production, so the sweep turns fixed bugs into
permanent detection. Run it as the first action of the weekly review.

Checks (each maps to a real incident):
  1. canary_lifecycle_terminal_lag  canary stage lags terminal lifecycle (1743-row spam, f3a46e83)
  2. canary_coverage_gap            non-terminal lifecycle factors absent from canary_state (78d79e02 class)
  3. overlay_mutation_binding       dangling overlay rows / unverified committed mutations (f9da796a)
  4. mutation_state_machine         illegal statuses, stuck prepared/reserved, pending projections (a2e7ab0d)
  5. jobs_stuck                     heartbeat-dead leases, stale pending jobs (367aa062 class)
  6. audit_write_rate_anomaly       audit-table write bursts vs 14-day median (1743 spam detector)
  7. projection_freshness           runtime_kv projections live code reads going stale
  8. sample_integrity_recent        recent training samples: integrity + contamination trend
  9. orphan_artifacts               model artifact dirs with no code references (meta_model_lightgbm)

Exit codes: 0 = all checks ok/warn, 2 = at least one fail. Never writes to the
databases; report goes to run_artifacts/invariant_sweep/.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402

NOW = time.time()
DAY = 86400.0


def _epoch(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.fromisoformat(str(value)).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        try:
            return datetime.fromtimestamp(float(value), tz=timezone.utc).timestamp()
        except (ValueError, OSError):
            return None


class Sweep:
    def __init__(self) -> None:
        self.conn = get_state_pg_conn()
        self.checks: list[dict[str, Any]] = []

    def query(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        return [dict(r) for r in (self.conn.execute(sql, params) if params else self.conn.execute(sql)).fetchall()]

    def scalar(self, sql: str) -> Any:
        row = self.query(sql)
        return list(row[0].values())[0] if row else None

    def add(self, name: str, description: str, status: str, detail: str, evidence: Any) -> None:
        self.checks.append(
            {"name": name, "description": description, "status": status, "detail": detail, "evidence": evidence}
        )

    def close(self) -> None:
        self.conn.close()


def check_canary_terminal_lag(s: Sweep) -> None:
    """Lifecycle says terminal (RETIRED/QUARANTINED) but canary_state still active."""
    rows = s.query(
        """
        SELECT c.factor_name, c.stage AS canary_stage, l.stage AS lifecycle_stage
        FROM runtime.canary_state c
        JOIN runtime.factor_lifecycle_state l ON l.factor_name = c.factor_name
        WHERE l.stage IN ('RETIRED', 'QUARANTINED')
          AND c.stage NOT IN ('RETIRED', 'QUARANTINED', 'DEAD')
        """
    )
    if not rows:
        s.add("canary_lifecycle_terminal_lag", "canary stage vs terminal lifecycle", "ok",
              "no terminal-lag rows", {"count": 0})
        return
    n = len(rows)
    status = "fail" if n > 50 else "warn"
    s.add("canary_lifecycle_terminal_lag", "canary stage vs terminal lifecycle", status,
          f"{n} factor(s) terminal in lifecycle but still active in canary projection "
          "(regression rollback will spam blocked_by_evidence audit rows)",
          {"count": n, "sample": rows[:5]})


def check_canary_coverage_gap(s: Sweep) -> None:
    """Non-terminal lifecycle factors that canary_state does not cover at all.

    Builtin/code-owned factors legitimately have no canary ladder row; only
    bar-class origins (dsl/shadow/discovered) are expected on the ladder.
    """
    rows = s.query(
        """
        SELECT l.factor_name, l.stage, l.origin
        FROM runtime.factor_lifecycle_state l
        LEFT JOIN runtime.canary_state c ON c.factor_name = l.factor_name
        WHERE l.stage IN ('SHADOW', 'PROMOTION_PREPARED')
          AND l.origin IN ('dsl', 'shadow', 'discovered')
          AND c.factor_name IS NULL
        """
    )
    n = len(rows)
    by_stage: dict[str, int] = {}
    for r in rows:
        by_stage[str(r["stage"])] = by_stage.get(str(r["stage"]), 0) + 1
    if n == 0:
        s.add("canary_coverage_gap", "non-terminal lifecycle factors missing from canary_state", "ok",
              "full coverage", {"count": 0})
        return
    status = "warn"
    s.add("canary_coverage_gap", "non-terminal lifecycle factors missing from canary_state", status,
          f"{n} non-terminal lifecycle row(s) have no canary_state row (unmanaged by ladder; "
          "backpressure counters derived from narrow projections will miscount)",
          {"count": n, "by_stage": by_stage, "sample": rows[:5]})


def check_overlay_binding(s: Sweep) -> None:
    """Every overlay row must bind a committed mutation (or a verified legacy manifest)."""
    rows = s.query(
        """
        SELECT overlay_id, mutation_id, overlay_hash, source, updated_at,
               (legacy_authority_json IS NULL OR legacy_authority_json::text IN ('', 'null')) AS no_legacy
        FROM runtime.runtime_config_overlay
        """
    )
    dangling = [
        r for r in rows
        if not r["mutation_id"] and r["no_legacy"]
    ]
    unverified = []
    for r in rows:
        mid = r["mutation_id"]
        if not mid:
            continue
        intent = s.query(
            "SELECT status FROM runtime.governance_mutation_intent WHERE mutation_id = %s",
            (str(mid),),
        )
        if not intent or str(intent[0]["status"]) != "committed":
            unverified.append({"overlay_id": str(r["overlay_id"]), "mutation_id": str(mid),
                               "intent_status": str(intent[0]["status"]) if intent else "missing"})
    if not dangling and not unverified:
        s.add("overlay_mutation_binding", "overlay rows bound to committed mutations", "ok",
              "all overlay rows verified", {"overlay_rows": len(rows)})
        return
    status = "fail" if dangling else "warn"
    s.add("overlay_mutation_binding", "overlay rows bound to committed mutations", status,
          f"{len(dangling)} overlay row(s) with no mutation and no legacy manifest; "
          f"{len(unverified)} overlay row(s) referencing non-committed/missing mutations "
          "(learning worker mutation quarantine is the visible symptom)",
          {"dangling": dangling[:5], "unverified": unverified[:5], "overlay_rows": len(rows)})


def check_mutation_state_machine(s: Sweep) -> None:
    """Illegal statuses, stuck prepared/reserved, committed-but-pending projections."""
    allowed = {"reserved", "prepared", "committed", "aborted", "rolled_back", "superseded"}
    rows = s.query(
        """
        SELECT mutation_id, status, projection_status, projection_attempts, created_at, updated_at
        FROM runtime.governance_mutation_intent
        WHERE status NOT IN ('committed', 'aborted', 'rolled_back', 'superseded')
           OR (status = 'committed' AND projection_status = 'pending')
        """
    )
    illegal = [r for r in rows if str(r["status"]) not in allowed]
    stuck_open = [
        r for r in rows
        if str(r["status"]) in {"reserved", "prepared"}
        and (_epoch(r["created_at"]) is None or NOW - (_epoch(r["created_at"]) or 0) > DAY)
    ]
    pending_projection = [r for r in rows if str(r["status"]) == "committed" and str(r["projection_status"]) == "pending"]
    status = "fail" if illegal else ("warn" if stuck_open or len(pending_projection) > 20 else "ok")
    s.add("mutation_state_machine", "governance intent state machine hygiene", status,
          f"illegal={len(illegal)} stuck_reserved_prepared={len(stuck_open)} committed_pending_projection={len(pending_projection)}",
          {"illegal": illegal[:5], "stuck": stuck_open[:5], "pending_projection": len(pending_projection)})


def check_jobs_stuck(s: Sweep) -> None:
    """Dead leases (heartbeat older than 30 min) and stale pending jobs."""
    rows = s.query(
        f"""
        SELECT id, kind, status, claimed_at, heartbeat_at, created_at, attempt_count, max_attempts
        FROM runtime.jobs
        WHERE status IN ('running', 'claimed')
           OR (status = 'pending' AND created_at < {NOW - DAY})
        """
    )
    dead_lease = []
    for r in rows:
        if str(r["status"]) in {"running", "claimed"}:
            hb = _epoch(r.get("heartbeat_at")) or _epoch(r.get("claimed_at")) or 0
            if hb and NOW - hb > 1800:
                dead_lease.append({"id": r["id"], "kind": r["kind"], "status": r["status"],
                                   "heartbeat_age_min": round((NOW - hb) / 60, 1)})
    stale_pending = [
        {"id": r["id"], "kind": r["kind"], "age_h": round((NOW - (_epoch(r["created_at"]) or 0)) / 3600, 1)}
        for r in rows if str(r["status"]) == "pending"
    ]
    status = "fail" if dead_lease else ("warn" if stale_pending else "ok")
    s.add("jobs_stuck", "job queue: dead leases and stale pending jobs", status,
          f"dead_leases={len(dead_lease)} stale_pending_24h={len(stale_pending)}",
          {"dead_lease": dead_lease[:5], "stale_pending": stale_pending[:5]})


def check_audit_rate_anomaly(s: Sweep) -> None:
    """Audit tables: last-3-day daily writes vs 14-day median (spam burst detector)."""
    tables = ["runtime.policy_suggestion", "runtime.governance_mutation_intent",
              "runtime.factor_governance_shadow_audit", "runtime.learning_application_log"]
    findings = []
    for table in tables:
        rows = s.query(
            f"""
            SELECT to_char(to_timestamp(created_at), 'YYYY-MM-DD') AS day, count(*) AS n
            FROM {table}
            WHERE created_at >= {NOW - 17 * DAY}
            GROUP BY 1 ORDER BY 1
            """
        )
        if len(rows) < 4:
            continue
        counts = [int(r["n"]) for r in rows[:-3]]
        recent = [int(r["n"]) for r in rows[-3:]]
        med = sorted(counts)[len(counts) // 2] if counts else 0
        for day_row, n in zip(rows[-3:], recent):
            if med > 0 and n > max(50, 5 * med):
                findings.append({"table": table, "day": str(day_row["day"]), "writes": n, "median_14d": med})
    status = "fail" if findings else "ok"
    s.add("audit_write_rate_anomaly", "audit table write bursts vs 14-day median", status,
          f"{len(findings)} burst day(s)" if findings else "no bursts",
          {"findings": findings[:5]})


def check_projection_freshness(s: Sweep) -> None:
    """runtime_kv projections that live code reads, with freshness budgets."""
    budgets = {
        "runtime_health_projection.v1": 900,
        "backend_readiness_snapshot.v1": 1800,
        "runtime_factor_selection.v1": 900,
        "position_supervisor_selection.v1": DAY * 3,
        "evolution_cycle_watermark.v1": DAY * 3,
    }
    stale = []
    for key, budget in budgets.items():
        row = s.query(
            f"SELECT updated_at FROM runtime.runtime_kv WHERE key = '{key}'"
        )
        if not row:
            stale.append({"key": key, "problem": "missing"})
            continue
        ts = _epoch(row[0]["updated_at"])
        if ts and NOW - ts > budget:
            stale.append({"key": key, "age_h": round((NOW - ts) / 3600, 1), "budget_h": round(budget / 3600, 1)})
    status = "fail" if any(x.get("problem") == "missing" for x in stale) else ("warn" if stale else "ok")
    s.add("projection_freshness", "runtime_kv projection freshness", status,
          f"{len(stale)} stale/missing projection(s)" if stale else "all fresh",
          {"stale": stale})


def check_sample_integrity(s: Sweep) -> None:
    """Last-7-day training samples: integrity mix and contamination rate."""
    rows = s.query(
        f"""
        SELECT sample_type, integrity, system_contaminated, count(*) AS n
        FROM canonical_v2.training_sample_row
        WHERE created_at >= {NOW - 7 * DAY}
        GROUP BY 1, 2, 3
        """
    )
    total = sum(int(r["n"]) for r in rows)
    contaminated = sum(int(r["n"]) for r in rows if int(r["system_contaminated"] or 0) == 1)
    non_full = sum(int(r["n"]) for r in rows if str(r["integrity"]) != "full")
    frac_contaminated = contaminated / total if total else 0.0
    contaminated_by_type = {
        str(r["sample_type"]): int(r["n"])
        for r in rows
        if int(r["system_contaminated"] or 0) == 1
    }
    status = "fail" if frac_contaminated > 0.05 else ("warn" if frac_contaminated > 0.01 else "ok")
    s.add("sample_integrity_recent", "last-7d training samples integrity/contamination", status,
          f"new_samples={total} contaminated={contaminated} ({frac_contaminated:.1%}) non_full_integrity={non_full}",
          {"total": total, "contaminated": contaminated, "contaminated_by_type": contaminated_by_type,
           "non_full_integrity": non_full})


def check_orphan_artifacts(s: Sweep) -> None:
    """Model artifact dirs with no code references."""
    base = PROJECT_ROOT / "data" / "model_artifacts"
    orphans = []
    if base.exists():
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            probe = subprocess.run(
                ["grep", "-rl", d.name,
                 str(PROJECT_ROOT / "backend"), str(PROJECT_ROOT / "research")],
                capture_output=True, text=True, timeout=60,
            )
            if not probe.stdout.strip():
                orphans.append(d.name)
    status = "warn" if orphans else "ok"
    s.add("orphan_artifacts", "model artifact dirs without code references", status,
          f"{len(orphans)} orphan(s)" if orphans else "all referenced",
          {"orphans": orphans})


def main() -> int:
    sweep = Sweep()
    try:
        check_canary_terminal_lag(sweep)
        check_canary_coverage_gap(sweep)
        check_overlay_binding(sweep)
        check_mutation_state_machine(sweep)
        check_jobs_stuck(sweep)
        check_audit_rate_anomaly(sweep)
        check_projection_freshness(sweep)
        check_sample_integrity(sweep)
        check_orphan_artifacts(sweep)
    finally:
        sweep.close()

    fails = sum(1 for c in sweep.checks if c["status"] == "fail")
    warns = sum(1 for c in sweep.checks if c["status"] == "warn")
    report = {
        "schema_version": "invariant_sweep.v1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {"checks": len(sweep.checks), "fail": fails, "warn": warns, "ok": len(sweep.checks) - fails - warns},
        "checks": sweep.checks,
    }
    out_dir = PROJECT_ROOT / "run_artifacts" / "invariant_sweep"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"sweep_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
    out_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    for c in sweep.checks:
        marker = {"fail": "FAIL", "warn": "WARN", "ok": " ok "}[c["status"]]
        print(f"[{marker}] {c['name']}: {c['detail']}")
    print(f"\nsummary: {report['summary']}")
    print(f"written: {out_path}")
    return 2 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
