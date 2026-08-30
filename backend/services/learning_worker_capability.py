"""Learning worker boot and mutation-capability projection.

The worker has two independent responsibilities:

* observation/research keeps producing evidence;
* governed mutation may be disabled after repeated dependency failures.

This module deliberately does not own any governance decision.  It only
tracks whether the worker is able to execute an already-authorized mutation
and publishes that operational fact through ``runtime_kv``.
"""
from __future__ import annotations

import copy
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path


STATUS_KEY = "learning_worker.capability.v2"
MUTATION_FAILURE_THRESHOLD = 3


class LearningWorkerCapability:
    """Thread-safe worker capability state with a latched mutation circuit."""

    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        boot_id: str | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = Path(db_path)
        self._now = now
        self._lock = threading.RLock()
        started_at = float(now())
        from backend.core.release_identity import release_identity_contract

        from backend.core.static_feature_flags import (
            shared_static_feature_flags,
            static_feature_flags_fingerprint,
        )

        process_flags = shared_static_feature_flags().to_dict()
        self._state: dict[str, Any] = {
            "schema_version": "learning_worker_capability.v2",
            "boot_id": str(boot_id or uuid.uuid4()),
            "pid": int(os.getpid()),
            "boot_status": "starting",
            "started_at": started_at,
            "updated_at": started_at,
            "release_identity": release_identity_contract(),
            "process_static_feature_flags": {
                "schema_version": "static_feature_flags.v1",
                "values": process_flags,
                "fingerprint": static_feature_flags_fingerprint(process_flags),
                "pid": int(os.getpid()),
                "process_started_at": started_at,
            },
            "config_hash": "",
            "overlay_hash": "",
            "recovery_status": "pending",
            "observation_capability": {
                "available": False,
                "status": "booting",
            },
            "research_capability": {
                "available": False,
                "status": "booting",
            },
            "mutation_capability": {
                "available": False,
                "status": "booting",
                "circuit_state": "closed",
                "failure_threshold": MUTATION_FAILURE_THRESHOLD,
                "consecutive_failures": 0,
                "last_failure_at": 0.0,
                "last_success_at": 0.0,
                "last_error": "",
                "opened_at": 0.0,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def mark_ready(
        self,
        *,
        config_hash: str,
        overlay_hash: str,
        recovery_status: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._state.update(
                {
                    "boot_status": "ready",
                    "config_hash": str(config_hash or ""),
                    "overlay_hash": str(overlay_hash or ""),
                    "recovery_status": str(recovery_status or "complete"),
                    "updated_at": float(self._now()),
                }
            )
            self._state["observation_capability"] = {
                "available": True,
                "status": "available",
            }
            self._state["research_capability"] = {
                "available": True,
                "status": "available",
            }
            mutation = self._state["mutation_capability"]
            mutation.update(
                {
                    "available": mutation.get("circuit_state") != "open",
                    "status": "available" if mutation.get("circuit_state") != "open" else "circuit_open",
                }
            )
            return copy.deepcopy(self._state)

    def mark_boot_failed(self, *, stage: str, error: BaseException | str) -> dict[str, Any]:
        with self._lock:
            now = float(self._now())
            self._state.update(
                {
                    "boot_status": "failed",
                    "boot_failure_stage": str(stage or "unknown"),
                    "boot_error": (
                        f"{type(error).__name__}: {error}"
                        if isinstance(error, BaseException)
                        else str(error or "")
                    ),
                    "updated_at": now,
                }
            )
            for key in ("observation_capability", "research_capability"):
                self._state[key] = {"available": False, "status": "boot_failed"}
            mutation = self._state["mutation_capability"]
            mutation.update({"available": False, "status": "boot_failed"})
            return copy.deepcopy(self._state)

    def mutation_allowed(self) -> bool:
        with self._lock:
            mutation = self._state["mutation_capability"]
            return bool(
                self._state.get("boot_status") == "ready"
                and mutation.get("available")
                and mutation.get("circuit_state") == "closed"
            )

    def record_mutation_success(self, *, job_name: str) -> dict[str, Any]:
        with self._lock:
            now = float(self._now())
            mutation = self._state["mutation_capability"]
            # An opened circuit is latched until the worker is restarted or an
            # operator explicitly repairs it.  A late success from another
            # in-flight job must not silently reopen mutation authority.
            if mutation.get("circuit_state") != "open":
                mutation.update(
                    {
                        "available": True,
                        "status": "available",
                        "consecutive_failures": 0,
                        "last_error": "",
                    }
                )
            mutation.update(
                {
                    "last_job": str(job_name or ""),
                    "last_success_at": now,
                }
            )
            self._state["updated_at"] = now
            return copy.deepcopy(self._state)

    def record_mutation_failure(
        self,
        *,
        job_name: str,
        error: BaseException | str,
    ) -> dict[str, Any]:
        with self._lock:
            now = float(self._now())
            mutation = self._state["mutation_capability"]
            failures = int(mutation.get("consecutive_failures") or 0) + 1
            error_text = (
                f"{type(error).__name__}: {error}"
                if isinstance(error, BaseException)
                else str(error or "")
            )
            mutation.update(
                {
                    "consecutive_failures": failures,
                    "last_failure_at": now,
                    "last_job": str(job_name or ""),
                    "last_error": error_text,
                }
            )
            if failures >= MUTATION_FAILURE_THRESHOLD:
                mutation.update(
                    {
                        "available": False,
                        "status": "circuit_open",
                        "circuit_state": "open",
                        "opened_at": float(mutation.get("opened_at") or now),
                    }
                )
            else:
                mutation.update(
                    {
                        "available": True,
                        "status": "degraded",
                    }
                )
            self._state["updated_at"] = now
            return copy.deepcopy(self._state)

    def heartbeat(self) -> dict[str, Any]:
        with self._lock:
            self._state["updated_at"] = float(self._now())
            return copy.deepcopy(self._state)

    def mark_stopped(self) -> dict[str, Any]:
        with self._lock:
            now = float(self._now())
            self._state.update({"boot_status": "stopped", "updated_at": now})
            for key in ("observation_capability", "research_capability"):
                self._state[key] = {"available": False, "status": "stopped"}
            self._state["mutation_capability"].update(
                {"available": False, "status": "stopped"}
            )
            return copy.deepcopy(self._state)

    def update_runtime_hashes(
        self,
        *,
        config_hash: str,
        overlay_hash: str,
    ) -> dict[str, Any]:
        with self._lock:
            self._state.update(
                {
                    "config_hash": str(config_hash or ""),
                    "overlay_hash": str(overlay_hash or ""),
                    "updated_at": float(self._now()),
                }
            )
            return copy.deepcopy(self._state)

    def refresh_runtime_hashes(self) -> dict[str, Any]:
        """Refresh worker-side config projections from the active config."""
        from backend.services.evolution_ledger import current_runtime_config_snapshot
        from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
        from config import runtime_config

        active_config = runtime_config.shared()
        active_hash = runtime_config.runtime_config_hash(active_config)
        snapshot = current_runtime_config_snapshot(
            db_path=self.db_path,
            create_if_missing=False,
        )
        snapshot_hash = str(snapshot.get("config_hash") or "")
        if not snapshot_hash:
            raise RuntimeError("learning_worker_runtime_config_snapshot_missing")
        if snapshot_hash != active_hash:
            raise RuntimeError(
                "learning_worker_runtime_config_hash_mismatch:"
                f"active={active_hash}:snapshot={snapshot_hash}"
            )
        overlay = RuntimeConfigOverlayService(self.db_path).status()
        if overlay.get("ok") is not True and str(overlay.get("status") or "") != "missing":
            raise RuntimeError(f"learning_worker_runtime_overlay_unavailable:{overlay}")
        return self.update_runtime_hashes(
            config_hash=active_hash,
            overlay_hash=str(overlay.get("overlay_hash") or ""),
        )

    def refresh_and_publish_heartbeat(
        self,
        *,
        job_name: str = "capability_heartbeat",
    ) -> dict[str, Any]:
        """Refresh and publish one dependency-accounted capability heartbeat.

        Runtime-config refresh and the PostgreSQL projection are both mutation
        dependencies: a worker that cannot prove its committed config hashes
        to the backend must eventually stop mutating.  Count at most one
        failure per heartbeat attempt, while preserving the existing latched
        circuit semantics.

        ``record_mutation_success`` is applied before publishing so the
        projected payload reflects recovery immediately.  If publication
        fails, the prior mutation state is restored before recording the
        failure; otherwise repeated publish failures would be reset to one by
        the optimistic success on every attempt.
        """
        try:
            self.refresh_runtime_hashes()
        except Exception as exc:
            self.record_mutation_failure(job_name=job_name, error=exc)
            try:
                # Best effort only: when refresh failed but PG is still
                # reachable, publish the degraded/circuit-open fact.
                self.publish()
            except Exception:
                pass
            raise

        with self._lock:
            prior_mutation = copy.deepcopy(self._state["mutation_capability"])
        self.record_mutation_success(job_name=job_name)
        try:
            return self.publish()
        except Exception as exc:
            with self._lock:
                self._state["mutation_capability"] = prior_mutation
            self.record_mutation_failure(job_name=job_name, error=exc)
            raise

    def publish(self) -> dict[str, Any]:
        payload = self.heartbeat()
        conn = (
            get_state_pg_conn()
            if is_state_db_path(self.db_path)
            else connect_sqlite(self.db_path)
        )
        try:
            from backend.services.runtime_kv_store import set_on_conn

            set_on_conn(
                conn,
                STATUS_KEY,
                payload,
                updated_at=float(payload["updated_at"]),
                ensure=False,
            )
            conn.commit()
        except Exception:
            try:
                conn.rollback()
            except Exception:
                pass
            raise
        finally:
            conn.close()
        return payload


def mutation_result_state(result: Any) -> str:
    """Classify a mutation result as success, failure or neutral policy skip."""
    if result is None:
        return "success"
    if not isinstance(result, dict):
        to_dict = getattr(result, "to_dict", None)
        if not callable(to_dict):
            return "success"
        result = to_dict()
        if not isinstance(result, dict):
            return "success"
    status = str(result.get("status") or "").strip().lower()
    failure_statuses = {
        "error",
        "failed",
        "mutation_failed",
        "completed_with_errors",
        "governance_coordinator_flag_invalid",
        "committed_projection_degraded",
    }
    if status in failure_statuses:
        return "failure"
    neutral_statuses = {
        "blocked",
        "disabled",
        "observation_only",
        "skipped_busy",
        "waiting_v16_command",
        "mutation_circuit_open",
        "completed_with_blockers",
    }
    if status in neutral_statuses or status.startswith(("blocked_", "skipped_", "waiting_")):
        return "neutral"
    if result.get("ok") is False:
        # Unknown negative results remain conservative: a new dependency error
        # must not evade the circuit just because it introduced a new status.
        return "failure"
    return "success"


def mutation_result_failed(result: Any) -> bool:
    """Compatibility predicate for explicit dependency/mutation failures."""
    return mutation_result_state(result) == "failure"


def mutation_stage_allowed(capability: LearningWorkerCapability) -> bool:
    """Return the current worker + operator gate for governed mutation."""
    from config.runtime_config import governance_expansion_is_paused

    return bool(
        capability.mutation_allowed()
        and not governance_expansion_is_paused()
    )


def guarded_mutation_job(
    capability: LearningWorkerCapability,
    job_name: str,
    fn: Callable[[], Any],
    *,
    publish: bool = True,
) -> Callable[[], Any]:
    """Wrap a mutation job while leaving observation/research jobs untouched."""

    def _run() -> Any:
        from config.runtime_config import governance_expansion_is_paused

        if governance_expansion_is_paused():
            return {
                "ok": True,
                "status": "observation_only",
                "job_name": job_name,
                "reason": "governance_expansion_paused",
            }
        if not capability.mutation_allowed():
            return {
                "ok": False,
                "status": "mutation_circuit_open",
                "job_name": job_name,
                "reason": "three_consecutive_mutation_dependency_failures",
            }
        failure_recorded = False
        try:
            result = fn()
            result_state = mutation_result_state(result)
            if result_state == "failure":
                capability.record_mutation_failure(job_name=job_name, error=str(result))
                failure_recorded = True
            elif result_state == "success":
                capability.record_mutation_success(job_name=job_name)
            if publish:
                capability.refresh_runtime_hashes()
                capability.publish()
            return result
        except Exception as exc:
            # One guarded invocation is one dependency attempt.  A mutation
            # result may already have recorded the failure before the
            # capability projection fails; counting both would open the
            # three-strike circuit after only two job attempts.
            if not failure_recorded:
                capability.record_mutation_failure(job_name=job_name, error=exc)
            if publish:
                try:
                    capability.publish()
                except Exception:
                    pass
            raise

    _run.__name__ = f"mutation_guarded_{job_name}"
    return _run
