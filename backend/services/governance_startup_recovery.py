"""Crash recovery shared by backend and learning-worker startup.

Only abandoned pre-commit ownership and expired V16 claims are released here.
Committed controls are never rolled back implicitly; their projection recovery
remains owned by the backend process and the domain-specific publishers.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB
from backend.services.governance_mutation_coordinator import (
    GovernanceMutationCoordinator,
)
from backend.services.v16_command_gate import V16CommandGate


class GovernanceStartupRecoveryService:
    def __init__(self, db_path: str | Path = STATE_DB) -> None:
        self.db_path = Path(db_path)

    @staticmethod
    def _stale_after_seconds() -> float:
        try:
            return max(
                15.0,
                float(
                    os.getenv(
                        "QUANT_GOVERNANCE_INTENT_STALE_AFTER_SECONDS",
                        "300",
                    )
                    or "300"
                ),
            )
        except Exception:
            return 300.0

    def run(self, *, process_role: str) -> dict[str, Any]:
        role = str(process_role or "").strip().lower()
        if role not in {"backend", "learning_worker"}:
            return {
                "ok": False,
                "status": "governance_recovery_process_role_invalid",
                "process_role": role,
            }
        stale = GovernanceMutationCoordinator(self.db_path).recover_stale_intents(
            stale_after_seconds=self._stale_after_seconds()
        )
        claims = V16CommandGate.recover_expired_claims(self.db_path)
        ok = bool(stale.get("ok")) and bool(claims.get("ok"))
        return {
            "ok": ok,
            "status": (
                "governance_startup_recovery_complete"
                if ok
                else "governance_startup_recovery_failed"
            ),
            "process_role": role,
            "stale_intents": stale,
            "expired_v16_claims": claims,
            "aborted_intent_count": int(stale.get("aborted_count") or 0),
            "released_claim_count": int(claims.get("released_count") or 0),
        }
