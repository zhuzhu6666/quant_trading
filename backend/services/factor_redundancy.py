"""Automatic redundancy grouping for live alpha factors."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    if is_state_db_path(db_path):
        return get_state_pg_conn(read_only=read_only)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


def _p(db_path: str | Path, sql: str) -> str:
    return sql.replace("?", "%s") if is_state_db_path(db_path) else sql


class RedundancyDetector:
    def __init__(self, db_path: str | Path = STATE_DB):
        self.db_path = db_path

    def build_report(
        self,
        catalog: list[dict[str, Any]],
        *,
        min_samples: int = 200,
        corr_threshold: float = 0.85,
        limit_per_factor: int = 500,
    ) -> dict[str, Any]:
        alpha = [
            item for item in catalog
            if item.get("role") == "alpha"
            and item.get("enabled")
            and item.get("eligible_for_live")
        ]
        names = [str(item["factor_id"]) for item in alpha]
        values = self._load_values(names, limit_per_factor=limit_per_factor)
        groups: list[dict[str, Any]] = []
        used: set[str] = set()
        for i, left in enumerate(names):
            if left in used or len(values.get(left, [])) < min_samples:
                continue
            members = [left]
            correlations: dict[str, float] = {}
            for right in names[i + 1:]:
                if right in used or len(values.get(right, [])) < min_samples:
                    continue
                corr = self._corr(values[left], values[right])
                if abs(corr) >= corr_threshold:
                    members.append(right)
                    correlations[f"{left}:{right}"] = corr
            if len(members) <= 1:
                continue
            used.update(members)
            leader = self._choose_leader(members, catalog)
            group_id = f"redundancy:auto:{leader}"
            groups.append({
                "group_id": group_id,
                "leader": leader,
                "members": sorted(members),
                "correlations": correlations,
                "sample_count": min(len(values.get(name, [])) for name in members),
                "corr_threshold": corr_threshold,
            })
        return {
            "schema_version": "factor_redundancy_report.v1",
            "groups": groups,
            "group_count": len(groups),
        }

    def _load_values(self, names: list[str], *, limit_per_factor: int) -> dict[str, list[float]]:
        if not names:
            return {}
        values: dict[str, list[float]] = {name: [] for name in names}
        conn = _connect(self.db_path, read_only=True)
        try:
            for name in names:
                rows = []
                try:
                    from backend.services.canonical_v2_reader import iter_decision_factor_snapshots_by_factor
                    snapshots = iter_decision_factor_snapshots_by_factor(conn, name, limit=int(limit_per_factor))
                    if snapshots:
                        rows = [{"normalized_value": s.get("normalized_value")} for s in snapshots]
                except Exception:
                    pass
                if not rows:
                    try:
                        rows = conn.execute(
                            _p(self.db_path, "SELECT normalized_value FROM decision_factor_snapshot WHERE factor=? ORDER BY id DESC LIMIT ?"),
                            (name, int(limit_per_factor)),
                        ).fetchall()
                    except Exception:
                        rows = []
                series = []
                for row in rows:
                    try:
                        val = float(row["normalized_value"])
                    except Exception:
                        continue
                    if np.isfinite(val):
                        series.append(val)
                values[name] = list(reversed(series))
        finally:
            conn.close()
        return values

    @staticmethod
    def _corr(left: list[float], right: list[float]) -> float:
        n = min(len(left), len(right))
        if n < 2:
            return 0.0
        a = np.asarray(left[-n:], dtype=float)
        b = np.asarray(right[-n:], dtype=float)
        if float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

    @staticmethod
    def _choose_leader(members: list[str], catalog: list[dict[str, Any]]) -> str:
        by_name = {str(item.get("factor_id") or ""): item for item in catalog}

        def score(name: str) -> tuple[float, float, float]:
            item = by_name.get(name, {})
            health = float(item.get("health_score") or 0.0)
            weight = float(item.get("weight") or 0.0)
            positive = float(item.get("model_positive_score") or 0.0)
            return health, positive, weight

        return sorted(members, key=score, reverse=True)[0]
