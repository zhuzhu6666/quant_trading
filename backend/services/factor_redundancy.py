"""Automatic redundancy grouping for live alpha factors."""
from __future__ import annotations

import gc
from pathlib import Path
from typing import Any

import numpy as np
from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.services.canonical_v2 import _payload_text_cache_clear
from backend.services.canonical_v2_reader import iter_decision_factor_values_by_factors
from backend.services.evolution_work_coordinator import release_free_memory


def _connect(db_path: str | Path = STATE_DB, *, read_only: bool = False):
    if is_state_db_path(db_path):
        return get_state_pg_conn(read_only=read_only)
    conn = connect_sqlite(db_path)
    conn.row_factory = __import__("sqlite3").Row
    return conn


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
        # Hoist float conversion + std source out of the O(N^2) pair loop.
        # Content-identical: same float64 values, same tail-min-n corr semantics.
        arrays: dict[str, Any] = {}
        for name, series in values.items():
            if len(series) >= min_samples:
                arrays[name] = np.asarray(series, dtype=float)
                # Release the Python-float list early; the ndarray holds the copy.
                values[name] = []
        del values
        groups: list[dict[str, Any]] = []
        used: set[str] = set()
        for i, left in enumerate(names):
            left_arr = arrays.get(left)
            if left_arr is None or left in used:
                continue
            members = [left]
            correlations: dict[str, float] = {}
            for right in names[i + 1:]:
                right_arr = arrays.get(right)
                if right_arr is None or right in used:
                    continue
                corr = self._corr_arrays(left_arr, right_arr)
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
                "sample_count": min(len(arrays[name]) for name in members),
                "corr_threshold": corr_threshold,
            })
        # Choke-point release: the scan just touched ~2500 payloads (each
        # cached as raw text process-globally) and held per-factor arrays
        # through the O(N^2) loop. Drop all of it before returning; the
        # report above is fully materialized. Cache miss = transparent
        # re-fetch, trim = best-effort (same mechanism as the compact
        # learning stages).
        del arrays
        gc.collect()
        _payload_text_cache_clear()
        release_free_memory()
        return {
            "schema_version": "factor_redundancy_report.v1",
            "groups": groups,
            "group_count": len(groups),
        }

    def _load_values(self, names: list[str], *, limit_per_factor: int) -> dict[str, list[float]]:
        if not names:
            return {}
        conn = _connect(self.db_path, read_only=True)
        try:
            # Newest-first floats from the lean projection; reverse to ASC
            # exactly like the previous dict-based path did.
            values_desc = iter_decision_factor_values_by_factors(
                conn,
                names,
                limit=int(limit_per_factor),
            )
            return {name: list(reversed(values_desc.get(name, []))) for name in names}
        finally:
            conn.close()

    @staticmethod
    def _corr_arrays(left: Any, right: Any) -> float:
        n = min(len(left), len(right))
        if n < 2:
            return 0.0
        a = np.asarray(left[-n:], dtype=float)
        b = np.asarray(right[-n:], dtype=float)
        if float(np.std(a)) < 1e-12 or float(np.std(b)) < 1e-12:
            return 0.0
        return float(np.corrcoef(a, b)[0, 1])

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
