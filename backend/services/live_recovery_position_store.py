"""Persistence boundary for live recovery-position state."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import json
from typing import Any


@dataclass(frozen=True)
class RecoveryPositionStoreRuntime:
    get_read_connection: Callable[[], Any]
    get_write_connection: Callable[[], Any]
    execute: Callable[..., Any]
    normalize_position: Callable[[Any], dict[str, Any]]
    normalize_row: Callable[[Any], dict[str, Any]]
    lookup_entry_decision_id: Callable[[int], str]
    build_meta_update_payload: Callable[..., dict[str, Any]]
    build_closed_update_payload: Callable[..., dict[str, Any]]
    now: Callable[[], float]
    local_open_volumes: Mapping[int, float]
    full_context: str = "full"
    partial_context: str = "partial"


class RecoveryPositionStore:
    """CRUD owner for ``recovery_position_state`` with injected DB mechanics."""

    def __init__(self, runtime: RecoveryPositionStoreRuntime) -> None:
        self.runtime = runtime

    def load(self, position_id: int) -> dict[str, Any]:
        if int(position_id or 0) <= 0:
            return {}
        position_key = str(int(position_id))
        conn = self.runtime.get_read_connection()
        try:
            row = self.runtime.execute(
                conn,
                """
                SELECT *
                FROM recovery_position_state
                WHERE position_id=?
                LIMIT 1
                """,
                (position_key,),
            ).fetchone()
            return self.runtime.normalize_row(row)
        finally:
            conn.close()

    def merge_meta(self, position_id: int, meta: Mapping[str, Any] | None) -> None:
        if int(position_id or 0) <= 0 or not meta:
            return
        position_key = str(int(position_id))
        conn = self.runtime.get_write_connection()
        try:
            row = self.runtime.execute(
                conn,
                "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
                (position_key,),
            ).fetchone()
            if row is None:
                return
            payload = self.runtime.build_meta_update_payload(
                position_id=position_id,
                existing_meta_json=row["recovery_meta_json"],
                meta=dict(meta),
                now_ts=self.runtime.now(),
            )
            self.runtime.execute(
                conn,
                """
                UPDATE recovery_position_state
                SET recovery_meta_json=?
                WHERE position_id=?
                """,
                (
                    json.dumps(
                        payload["recovery_meta"],
                        ensure_ascii=False,
                        default=str,
                    ),
                    str(payload["position_id"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def replace_meta(
        self,
        position_id: int,
        meta: Mapping[str, Any],
        *,
        expected_meta: Mapping[str, Any] | None = None,
    ) -> bool:
        """Replace the complete metadata object with an optional compare-and-set."""

        if int(position_id or 0) <= 0:
            return False
        position_key = str(int(position_id))
        conn = self.runtime.get_write_connection()
        try:
            row = self.runtime.execute(
                conn,
                "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
                (position_key,),
            ).fetchone()
            if row is None:
                return False
            raw = row["recovery_meta_json"] or "{}"
            try:
                current = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                current = {}
            if not isinstance(current, dict):
                current = {}
            if expected_meta is not None and current != dict(expected_meta):
                return False
            result = self.runtime.execute(
                conn,
                """
                UPDATE recovery_position_state
                SET recovery_meta_json=?
                WHERE position_id=? AND recovery_meta_json=?
                """,
                (
                    json.dumps(dict(meta), ensure_ascii=False, default=str),
                    position_key,
                    raw,
                ),
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        finally:
            conn.close()

    def upsert(
        self,
        raw_position: Any,
        *,
        broker: str,
        strategy_name: str,
        status: str = "open",
        context_integrity: str | None = None,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        snapshot = self.runtime.normalize_position(raw_position)
        position_id = int(snapshot["position_id"] or 0)
        if position_id <= 0:
            return
        position_key = str(position_id)
        now = float(self.runtime.now())
        # Prefer the caller-supplied entry_decision_id (the live ledger return)
        # over the stale file index. The file index from 2026-08-16 has no
        # 284* positions and would wash a freshly-created decision id to "".
        raw_payload = snapshot.get("raw")
        raw_payload = raw_payload if isinstance(raw_payload, dict) else {}
        snapshot_entry = str(
            snapshot.get("entry_decision_id")
            or raw_payload.get("entry_decision_id")
            or ""
        )
        lookup_entry = str(
            self.runtime.lookup_entry_decision_id(position_id) or ""
        )
        entry_decision_id = snapshot_entry or lookup_entry
        desired_integrity = context_integrity or (
            self.runtime.full_context
            if entry_decision_id
            else self.runtime.partial_context
        )
        conn = self.runtime.get_write_connection()
        try:
            prev = self.runtime.execute(
                conn,
                "SELECT * FROM recovery_position_state WHERE position_id=?",
                (position_key,),
            ).fetchone()
            first_seen_at = float(prev["first_seen_at"]) if prev else now
            stored_meta: dict[str, Any] = {}
            if prev and prev["recovery_meta_json"]:
                try:
                    candidate = json.loads(prev["recovery_meta_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    candidate = None
                if isinstance(candidate, dict):
                    stored_meta = candidate
            next_meta = dict(stored_meta)
            if meta:
                next_meta.update(dict(meta))
            prev_integrity = (
                str(prev["context_integrity"])
                if prev and prev["context_integrity"]
                else ""
            )
            if prev_integrity == self.runtime.full_context:
                desired_integrity = self.runtime.full_context
            self.runtime.execute(
                conn,
                """
                INSERT INTO recovery_position_state
                (position_id, broker, symbol, direction, open_price, volume,
                 first_seen_at, last_seen_at, status, strategy_name,
                 entry_decision_id, context_integrity, recovery_meta_json,
                 closed_at, close_reason, close_pnl)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0.0, '', 0.0)
                ON CONFLICT(position_id) DO UPDATE SET
                    broker=excluded.broker,
                    symbol=excluded.symbol,
                    direction=excluded.direction,
                    open_price=excluded.open_price,
                    volume=CASE
                        WHEN excluded.volume > 0 THEN excluded.volume
                        WHEN recovery_position_state.volume > 0 THEN recovery_position_state.volume
                        ELSE excluded.volume
                    END,
                    last_seen_at=excluded.last_seen_at,
                    status=excluded.status,
                    strategy_name=excluded.strategy_name,
                    entry_decision_id=CASE
                        WHEN recovery_position_state.entry_decision_id='' THEN excluded.entry_decision_id
                        ELSE recovery_position_state.entry_decision_id
                    END,
                    context_integrity=CASE
                        WHEN recovery_position_state.context_integrity='full' THEN 'full'
                        ELSE excluded.context_integrity
                    END,
                    recovery_meta_json=excluded.recovery_meta_json,
                    closed_at=CASE
                        WHEN excluded.status IN ('open', 'recovered') THEN 0.0
                        ELSE recovery_position_state.closed_at
                    END,
                    close_reason=CASE
                        WHEN excluded.status IN ('open', 'recovered') THEN ''
                        ELSE recovery_position_state.close_reason
                    END,
                    close_pnl=CASE
                        WHEN excluded.status IN ('open', 'recovered') THEN 0.0
                        ELSE recovery_position_state.close_pnl
                    END
                """,
                (
                    position_key,
                    broker,
                    snapshot["symbol"],
                    snapshot["direction"],
                    snapshot["open_price"],
                    snapshot["volume"],
                    first_seen_at,
                    now,
                    status,
                    strategy_name,
                    entry_decision_id,
                    desired_integrity,
                    json.dumps(next_meta, ensure_ascii=False, default=str),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def list_active(self, broker: str) -> list[dict[str, Any]]:
        conn = self.runtime.get_read_connection()
        try:
            rows = self.runtime.execute(
                conn,
                """
                SELECT * FROM recovery_position_state
                WHERE broker=? AND status IN ('open', 'recovered')
                ORDER BY last_seen_at ASC
                """,
                (broker,),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def purge_unbrokered(
        self,
        position_ids: set[int],
        *,
        broker: str,
        broker_position_ids: set[int],
    ) -> list[int]:
        """Delete explicitly verified local rows absent from the broker.

        This is a repair boundary for orphaned/test recovery rows, not a
        generic close path.  The caller must provide a fresh broker identity
        set; an overlap aborts the operation.  Rows with entry lineage are
        never deleted here because they may describe a real trade whose
        broker close evidence is merely delayed.
        """

        normalized = {
            int(position_id)
            for position_id in position_ids or set()
            if int(position_id or 0) > 0
        }
        broker_ids = {
            int(position_id)
            for position_id in broker_position_ids or set()
            if int(position_id or 0) > 0
        }
        overlap = sorted(normalized & broker_ids)
        if overlap:
            raise ValueError(
                "cannot purge recovery rows present at broker: "
                + ",".join(str(position_id) for position_id in overlap)
            )
        if not normalized:
            return []

        broker_name = str(broker or "")
        conn = self.runtime.get_write_connection()
        deleted: list[int] = []
        try:
            for position_id in sorted(normalized):
                position_key = str(position_id)
                row = self.runtime.execute(
                    conn,
                    """
                    SELECT broker, status, entry_decision_id
                    FROM recovery_position_state
                    WHERE position_id=?
                    LIMIT 1
                    """,
                    (position_key,),
                ).fetchone()
                if row is None:
                    continue
                if str(row["broker"] or "") != broker_name:
                    continue
                if str(row["status"] or "") not in {"open", "recovered"}:
                    continue
                if str(row["entry_decision_id"] or ""):
                    continue
                self.runtime.execute(
                    conn,
                    "DELETE FROM recovery_position_state WHERE position_id=?",
                    (position_key,),
                )
                deleted.append(position_id)
            conn.commit()
            return deleted
        finally:
            conn.close()

    def last_seen_by_position(self, position_ids: set[int]) -> dict[int, float]:
        normalized = sorted(
            {int(pid) for pid in position_ids if int(pid or 0) > 0}
        )
        if not normalized:
            return {}
        conn = self.runtime.get_read_connection()
        try:
            result: dict[int, float] = {}
            for position_id in normalized:
                position_key = str(position_id)
                row = self.runtime.execute(
                    conn,
                    "SELECT last_seen_at FROM recovery_position_state WHERE position_id=?",
                    (position_key,),
                ).fetchone()
                if row and float(row["last_seen_at"] or 0.0) > 0.0:
                    result[position_id] = max(
                        0.0,
                        float(row["last_seen_at"]) - 5.0,
                    )
            return result
        finally:
            conn.close()

    def remaining_volume_by_position(
        self,
        position_ids: set[int],
    ) -> dict[int, float]:
        normalized = sorted(
            {int(pid) for pid in position_ids if int(pid or 0) > 0}
        )
        if not normalized:
            return {}
        conn = self.runtime.get_read_connection()
        try:
            result: dict[int, float] = {}
            for position_id in normalized:
                position_key = str(position_id)
                row = self.runtime.execute(
                    conn,
                    "SELECT volume FROM recovery_position_state WHERE position_id=?",
                    (position_key,),
                ).fetchone()
                if row and float(row["volume"] or 0.0) > 0.0:
                    result[position_id] = float(row["volume"])
                    continue
                local_volume = float(
                    self.runtime.local_open_volumes.get(position_id, 0.0) or 0.0
                )
                if local_volume > 0.0:
                    result[position_id] = local_volume
            return result
        finally:
            conn.close()

    def mark_closed(
        self,
        position_id: int,
        *,
        close_reason: str,
        close_pnl: float,
        closed_at: float,
        meta: Mapping[str, Any] | None = None,
    ) -> None:
        position_key = str(int(position_id))
        conn = self.runtime.get_write_connection()
        try:
            row = self.runtime.execute(
                conn,
                "SELECT recovery_meta_json FROM recovery_position_state WHERE position_id=?",
                (position_key,),
            ).fetchone()
            payload = self.runtime.build_closed_update_payload(
                position_id=position_id,
                existing_meta_json=row["recovery_meta_json"] if row else "",
                close_reason=close_reason,
                close_pnl=close_pnl,
                closed_at=closed_at,
                meta=dict(meta or {}),
            )
            self.runtime.execute(
                conn,
                """
                UPDATE recovery_position_state
                SET status='closed_replayed',
                    closed_at=?,
                    close_reason=?,
                    close_pnl=?,
                    recovery_meta_json=?
                WHERE position_id=?
                """,
                (
                    payload["closed_at"],
                    payload["close_reason"],
                    payload["close_pnl"],
                    json.dumps(
                        payload["recovery_meta"],
                        ensure_ascii=False,
                        default=str,
                    ),
                    str(payload["position_id"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def context_integrity(self, position_id: int, *, default: str) -> str:
        position_key = str(int(position_id))
        conn = self.runtime.get_read_connection()
        try:
            row = self.runtime.execute(
                conn,
                "SELECT context_integrity FROM recovery_position_state WHERE position_id=?",
                (position_key,),
            ).fetchone()
            return str(row["context_integrity"] or default) if row else default
        finally:
            conn.close()
