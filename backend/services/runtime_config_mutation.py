"""Unified runtime config mutation path for autonomous services."""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, is_state_db_path
from backend.services.mutation_audit import record_api_mutation
from backend.services.runtime_config_overlay import RuntimeConfigOverlayService
from config import runtime_config


def _slice_config(config_dict: dict[str, Any], keys: list[str]) -> dict[str, Any]:
    return {key: config_dict.get(key) for key in keys if key in config_dict}


class RuntimeConfigMutationService:
    """Apply autonomous runtime config patches through overlay + snapshot.

    DecisionPolicy remains the policy engine for weight decisions. This service
    is only the persistence/audit boundary for runtime config mutations.
    """

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        overlay: RuntimeConfigOverlayService | None = None,
    ):
        self.db_path = db_path
        self.overlay = overlay or RuntimeConfigOverlayService(db_path)

    def apply_patch(
        self,
        patch: dict[str, Any],
        *,
        source: str,
        run_id: str = "",
        actor: str = "system:runtime_config_mutation",
        action: str | None = None,
        reason: str = "",
        audit: bool | None = None,
    ) -> dict[str, Any]:
        keys = sorted((patch or {}).keys())
        before = _slice_config(runtime_config.shared().to_dict(), keys)
        result = self.overlay.apply_patch(patch, source=source, run_id=run_id)
        after = _slice_config(runtime_config.shared().to_dict(), keys)

        should_audit = is_state_db_path(self.db_path) if audit is None else bool(audit)
        if should_audit:
            record_api_mutation(
                user=actor,
                endpoint="backend.services.runtime_config_mutation",
                action=action or source,
                status=str(result.get("status") or ("applied" if result.get("ok") else "failed")),
                before=before,
                after=after,
                result=result,
                reason=reason or source,
                required_confirm="autonomous-runtime-config",
                confirm_ok=bool(result.get("ok")),
            )
        return {
            **result,
            "mutation_source": source,
            "mutation_action": action or source,
            "mutated_at": time.time(),
        }
