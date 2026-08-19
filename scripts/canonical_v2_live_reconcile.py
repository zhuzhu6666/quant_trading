#!/usr/bin/env python3
"""实时镜像对账：canonical 增量 vs legacy 增量（开盘后监控用）。

用法：./.venv/bin/python scripts/canonical_v2_live_reconcile.py [--since-epoch <epoch>]
默认窗口 = 最近 6 小时。输出每域 legacy 行数 vs canonical 事件数（决策/订单/仓位/
治理变更/进化命令）与样本行数，缺额 = 镜像缺口（应由增量回填补齐）。
只读。"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from backend.core.db import get_state_pg_conn  # noqa: E402

DOMAINS = (
    # (legacy 表, 时间列, canonical 事件类型/表, 比较方式)
    ("state_v1.decision_ledger", "decision_ts", ("risk_decision",), "event"),
    ("state_v1.order_lifecycle_event", "event_ts", ("broker_execution",), "event"),
    ("state_v1.position_lifecycle_event", "event_ts", ("position_transition",), "event"),
    ("state_v1.governance_mutation_intent", "created_at", ("governance_effect",), "event"),
    ("state_v1.evolution_decision", "created_at", ("governance_command",), "event"),
    ("state_v1.autonomous_learning_sample", "updated_at", ("canonical_v2.training_sample_row",), "row"),
)


def main() -> int:
    parser = argparse.ArgumentParser(description="实时镜像对账")
    parser.add_argument("--since-epoch", type=float, default=time.time() - 6 * 3600)
    args = parser.parse_args()

    conn = get_state_pg_conn()
    try:
        report: dict[str, Any] = {}
        for legacy_table, ts_col, target, mode in DOMAINS:
            legacy_n = conn.execute(
                f"SELECT count(*) AS c FROM {legacy_table} WHERE {ts_col} > %s",
                (args.since_epoch,),
            ).fetchone()["c"]
            if mode == "event":
                canonical_n = conn.execute(
                    "SELECT count(*) AS c FROM canonical_v2.event "
                    "WHERE event_type = ANY(%s) AND observed_at > to_timestamp(%s)",
                    (list(target), args.since_epoch),
                ).fetchone()["c"]
            else:
                canonical_n = conn.execute(
                    f"SELECT count(*) AS c FROM {target[0]} WHERE updated_at > %s",
                    (args.since_epoch,),
                ).fetchone()["c"]
            report[legacy_table.split(".")[-1]] = {
                "legacy": int(legacy_n),
                "canonical": int(canonical_n),
                "gap": int(legacy_n - canonical_n),
            }
        ok = all(item["gap"] <= 0 for item in report.values())
        print(json.dumps({
            "schema_version": "canonical_v2_live_reconcile.v1",
            "ok": ok,
            "since_epoch": args.since_epoch,
            "domains": report,
        }, ensure_ascii=False, indent=1))
    finally:
        conn.close()
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
