"""Fail-closed command handoff gate for V16 specialist mutations.

V16 is the decision owner, while specialist agents remain execution owners.
``authorize`` is a read-only preflight; ``claim`` and ``consume`` make the
handoff single-use so two workers cannot apply the same command.  The caller
still owns RiskPolicy/DecisionPolicy and the actual mutation.
"""
from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path, state_table_exists
from backend.services._brain_helpers import connect, execute, loads, safe_float


class V16CommandGate:
    """Resolve a current V16 delegation before a system-owned mutation."""

    BROAD_SCOPE_KEYS = {"", "*", "alpha_weight_policy", "online_light", "threshold_and_sizing", "position_supervisor"}
    ACTION_ALIASES = {
        "update_weight": {"update_weight", "downweight", "boost_small", "boost"},
        "downweight": {"update_weight", "downweight"},
        "promote_factor": {"promote_factor", "update_weight", "factor_governance_cycle"},
        "retire_factor": {"retire_factor", "factor_governance_cycle"},
        "factor_governance_cycle": {
            "factor_governance_cycle",
            "update_weight",
            "promote_factor",
            "retire_factor",
            "update_redundancy_groups",
        },
        "switch_parameter_template": {"switch_parameter_template", "apply_parameter_template"},
        "switch_position_supervisor_template": {
            "switch_position_supervisor_template",
            "apply_switch",
        },
    }

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "v16_command_gate.v1",
            "v16_is_decision_owner": True,
            "specialist_is_execution_owner": True,
            "requires_recent_delegate": True,
            "command_claim_is_single_use": True,
            "evidence_binding_supported": True,
            "fail_closed_when_command_missing": True,
            "does_not_mutate_runtime": True,
            "does_not_submit_orders": True,
            "risk_reduction_may_use_existing_rollback_path": True,
        }

    @classmethod
    def authorize(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        target_agent: str,
        scope_type: str,
        scope_key: str = "",
        action: str = "",
        command_id: str = "",
        max_age_seconds: float | None = None,
        risk_reduction: bool = False,
    ) -> dict[str, Any]:
        now = time.time()
        if risk_reduction and not command_id:
            return {
                "ok": True,
                "allowed": True,
                "status": "risk_reduction_existing_path",
                "reason": "risk_reducing rollback/downsize path does not require a new V16 expansion command",
                "target_agent": target_agent,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "action": action,
                "boundary": cls.boundary(),
            }

        try:
            age_limit = float(
                max_age_seconds
                if max_age_seconds is not None
                else os.getenv("QUANT_V16_COMMAND_MAX_AGE_SECONDS", "1800")
            )
        except Exception:
            age_limit = 1800.0
        age_limit = max(60.0, age_limit)

        try:
            from backend.services.v16_brain_orchestrator import ensure_v16_brain_command_table

            ensure_v16_brain_command_table(db_path)
            conn = get_state_pg_conn(read_only=True) if is_state_db_path(db_path) else connect_sqlite(db_path, read_only=True)
            if not is_state_db_path(db_path):
                conn.row_factory = __import__("sqlite3").Row
        except Exception as exc:
            return cls._blocked(
                "v16_command_store_unavailable",
                target_agent=target_agent,
                scope_type=scope_type,
                scope_key=scope_key,
                action=action,
                error=f"{type(exc).__name__}: {exc}",
            )

        try:
            if not state_table_exists(conn, "v16_brain_command"):
                return cls._blocked(
                    "v16_command_table_missing",
                    target_agent=target_agent,
                    scope_type=scope_type,
                    scope_key=scope_key,
                    action=action,
                )
            sql = """
                SELECT command_id, candidate_id, target_agent, scope_type, scope_key,
                       action, decision, status, evidence_json, delegation_json,
                       claim_status, claim_token, claim_expires_at, apply_count,
                       max_apply_count, consumed_at, consumed_mutation_id,
                       posterior_fingerprint, evidence_fingerprint,
                       created_at, updated_at
                FROM v16_brain_command
                WHERE target_agent=? AND decision='delegate'
                ORDER BY updated_at DESC, created_at DESC
                LIMIT 200
            """
            if is_state_db_path(db_path):
                sql = sql.replace("?", "%s")
            rows = conn.execute(sql, (str(target_agent),)).fetchall()
            requested_id = str(command_id or "")
            for row in rows:
                item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
                if requested_id and str(item.get("command_id") or "") != requested_id:
                    continue
                if not cls._scope_matches(item, scope_type=scope_type, scope_key=scope_key):
                    continue
                if action and not cls._action_matches(item, action):
                    # A global specialist delegation authorizes the specialist
                    # control surface; the specialist still applies its own
                    # narrower RiskPolicy/DecisionPolicy action gate.
                    command_scope = str(item.get("scope_type") or "")
                    if command_scope not in {"factor_weight", "parameter_template", "context_policy", "supervisor_template"}:
                        continue
                updated_at = float(item.get("updated_at") or item.get("created_at") or 0.0)
                age = max(0.0, now - updated_at) if updated_at else float("inf")
                if age > age_limit:
                    continue
                if str(item.get("claim_status") or "available") == "consumed":
                    continue
                if safe_float(item.get("apply_count")) >= max(1, int(item.get("max_apply_count") or 1)):
                    continue
                return {
                    "ok": True,
                    "allowed": True,
                    "status": "v16_command_authorized",
                    "command_id": str(item.get("command_id") or ""),
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "target_agent": str(item.get("target_agent") or ""),
                    "scope_type": str(item.get("scope_type") or ""),
                    "scope_key": str(item.get("scope_key") or ""),
                    "command_action": str(item.get("action") or ""),
                    "requested_action": action,
                    "age_seconds": round(age, 3),
                    "max_age_seconds": age_limit,
                    "evidence": loads(item.get("evidence_json"), {}),
                    "delegation": loads(item.get("delegation_json"), {}),
                    "claim_status": str(item.get("claim_status") or "available"),
                    "apply_count": int(item.get("apply_count") or 0),
                    "max_apply_count": max(1, int(item.get("max_apply_count") or 1)),
                    "posterior_fingerprint": str(item.get("posterior_fingerprint") or ""),
                    "evidence_fingerprint": str(item.get("evidence_fingerprint") or ""),
                    "boundary": cls.boundary(),
                }
            return cls._blocked(
                "v16_command_required",
                target_agent=target_agent,
                scope_type=scope_type,
                scope_key=scope_key,
                action=action,
                max_age_seconds=age_limit,
                requested_command_id=requested_id,
            )
        finally:
            conn.close()

    @classmethod
    def claim(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        target_agent: str,
        scope_type: str,
        scope_key: str = "",
        action: str = "",
        command_id: str = "",
        candidate_id: str = "",
        posterior_fingerprint: str = "",
        evidence_fingerprint: str = "",
        claim_ttl_seconds: float = 120.0,
        risk_reduction: bool = False,
    ) -> dict[str, Any]:
        """Atomically claim one current V16 command for a mutation attempt."""
        if risk_reduction and not command_id:
            return {
                "ok": True,
                "allowed": True,
                "status": "risk_reduction_existing_path",
                "reason": "risk reducing rollback/downsize path does not require a new V16 expansion command",
                "target_agent": target_agent,
                "scope_type": scope_type,
                "scope_key": scope_key,
                "action": action,
                "boundary": cls.boundary(),
            }

        try:
            from backend.services.v16_brain_orchestrator import ensure_v16_brain_command_table

            ensure_v16_brain_command_table(db_path)
            conn = connect(db_path)
        except Exception as exc:
            return cls._blocked(
                "v16_command_store_unavailable",
                target_agent=target_agent,
                scope_type=scope_type,
                scope_key=scope_key,
                action=action,
                error=f"{type(exc).__name__}: {exc}",
            )

        now = time.time()
        ttl = max(15.0, min(float(claim_ttl_seconds or 120.0), 900.0))
        try:
            if is_state_db_path(db_path):
                execute(conn, "SELECT pg_advisory_xact_lock(821640242)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            execute(
                conn,
                """UPDATE v16_brain_command
                   SET claim_status='available', claim_token='', claimed_at=0.0,
                       claim_expires_at=0.0, last_release_reason='claim_expired', updated_at=?
                   WHERE claim_status='claimed' AND claim_expires_at<=?""",
                (now, now),
            )
            where = "target_agent=? AND decision='delegate' AND updated_at>=?"
            params: list[Any] = [str(target_agent), now - cls._max_age_seconds()]
            if command_id:
                where += " AND command_id=?"
                params.append(str(command_id))
            rows = execute(
                conn,
                f"""SELECT command_id, candidate_id, target_agent, scope_type, scope_key,
                           action, decision, status, evidence_json, delegation_json,
                           claim_status, claim_token, claim_expires_at, apply_count,
                           max_apply_count, consumed_at, consumed_mutation_id,
                           posterior_fingerprint, evidence_fingerprint,
                           created_at, updated_at
                    FROM v16_brain_command
                    WHERE {where}
                    ORDER BY updated_at DESC, created_at DESC
                    LIMIT 200""",
                tuple(params),
            ).fetchall()
            for row in rows:
                item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
                if not cls._scope_matches(item, scope_type=scope_type, scope_key=scope_key):
                    continue
                if action and not cls._action_matches(item, action):
                    continue
                if candidate_id and str(item.get("candidate_id") or "") != str(candidate_id):
                    continue
                if posterior_fingerprint and str(item.get("posterior_fingerprint") or "") != str(posterior_fingerprint):
                    continue
                if evidence_fingerprint and str(item.get("evidence_fingerprint") or "") != str(evidence_fingerprint):
                    continue
                claim_state = str(item.get("claim_status") or "available")
                claim_expires_at = safe_float(item.get("claim_expires_at"))
                if claim_state == "consumed":
                    continue
                if claim_state == "claimed" and claim_expires_at > now:
                    continue
                apply_count = int(item.get("apply_count") or 0)
                max_apply_count = max(1, int(item.get("max_apply_count") or 1))
                if apply_count >= max_apply_count:
                    continue
                token = f"v16claim_{uuid.uuid4().hex}"
                claimed_until = now + ttl
                execute(
                    conn,
                    """UPDATE v16_brain_command
                       SET claim_status='claimed', claim_token=?, claimed_at=?,
                           claim_expires_at=?, updated_at=?
                       WHERE command_id=? AND decision='delegate'
                         AND claim_status='available' AND apply_count < max_apply_count""",
                    (token, now, claimed_until, now, str(item.get("command_id") or "")),
                )
                conn.commit()
                return {
                    "ok": True,
                    "allowed": True,
                    "status": "v16_command_claimed",
                    "command_id": str(item.get("command_id") or ""),
                    "claim_token": token,
                    "candidate_id": str(item.get("candidate_id") or ""),
                    "target_agent": str(item.get("target_agent") or ""),
                    "scope_type": str(item.get("scope_type") or ""),
                    "scope_key": str(item.get("scope_key") or ""),
                    "command_action": str(item.get("action") or ""),
                    "requested_action": action,
                    "claim_expires_at": claimed_until,
                    "apply_count": apply_count,
                    "max_apply_count": max_apply_count,
                    "posterior_fingerprint": str(item.get("posterior_fingerprint") or ""),
                    "evidence_fingerprint": str(item.get("evidence_fingerprint") or ""),
                    "evidence": loads(item.get("evidence_json"), {}),
                    "delegation": loads(item.get("delegation_json"), {}),
                    "boundary": cls.boundary(),
                }
            conn.rollback()
            return cls._blocked(
                "v16_command_required" if not command_id else "v16_command_unavailable",
                target_agent=target_agent,
                scope_type=scope_type,
                scope_key=scope_key,
                action=action,
                requested_command_id=str(command_id or ""),
            )
        except Exception as exc:
            conn.rollback()
            return cls._blocked(
                "v16_command_claim_failed",
                target_agent=target_agent,
                scope_type=scope_type,
                scope_key=scope_key,
                action=action,
                error=f"{type(exc).__name__}: {exc}",
            )
        finally:
            conn.close()

    @classmethod
    def consume(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        command_id: str,
        claim_token: str,
        mutation_id: str,
    ) -> dict[str, Any]:
        """Consume a claimed command exactly once before applying the mutation."""
        if not command_id or not claim_token:
            return cls._blocked("v16_command_claim_missing", command_id=command_id)
        try:
            conn = connect(db_path)
        except Exception as exc:
            return cls._blocked("v16_command_store_unavailable", error=f"{type(exc).__name__}: {exc}")
        try:
            if is_state_db_path(db_path):
                execute(conn, "SELECT pg_advisory_xact_lock(821640242)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            result = execute(
                conn,
                """UPDATE v16_brain_command
                   SET claim_status='consumed', apply_count=apply_count+1,
                       consumed_at=?, consumed_mutation_id=?, claim_token='',
                       claim_expires_at=0.0, updated_at=?
                   WHERE command_id=? AND claim_status='claimed' AND claim_token=?
                     AND claim_expires_at>? AND apply_count < max_apply_count""",
                (now, str(mutation_id or ""), now, str(command_id), str(claim_token), now),
            )
            if int(getattr(result, "rowcount", 0) or 0) != 1:
                conn.rollback()
                return cls._blocked("v16_command_consume_failed", command_id=command_id)
            conn.commit()
            return {
                "ok": True,
                "allowed": True,
                "status": "v16_command_consumed",
                "command_id": str(command_id),
                "mutation_id": str(mutation_id or ""),
                "consumed_at": now,
                "boundary": cls.boundary(),
            }
        except Exception as exc:
            conn.rollback()
            return cls._blocked("v16_command_consume_failed", command_id=command_id, error=f"{type(exc).__name__}: {exc}")
        finally:
            conn.close()

    @classmethod
    def release(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        command_id: str,
        claim_token: str,
        reason: str = "",
    ) -> dict[str, Any]:
        if not command_id or not claim_token:
            return cls._blocked("v16_command_claim_missing", command_id=command_id)
        conn = connect(db_path)
        try:
            if is_state_db_path(db_path):
                execute(conn, "SELECT pg_advisory_xact_lock(821640242)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            result = execute(
                conn,
                """UPDATE v16_brain_command
                   SET claim_status='available', claim_token='', claimed_at=0.0,
                       claim_expires_at=0.0, last_release_reason=?, updated_at=?
                   WHERE command_id=? AND claim_status='claimed' AND claim_token=?""",
                (str(reason or "released"), time.time(), str(command_id), str(claim_token)),
            )
            conn.commit()
            changed = int(getattr(result, "rowcount", 0) or 0) == 1
            return {
                "ok": changed,
                "allowed": changed,
                "status": "v16_command_released" if changed else "v16_command_release_not_found",
                "command_id": command_id,
                "boundary": cls.boundary(),
            }
        finally:
            conn.close()

    @staticmethod
    def _max_age_seconds() -> float:
        try:
            return max(60.0, float(os.getenv("QUANT_V16_COMMAND_MAX_AGE_SECONDS", "1800")))
        except Exception:
            return 1800.0

    @classmethod
    def _scope_matches(cls, row: dict[str, Any], *, scope_type: str, scope_key: str) -> bool:
        command_scope = str(row.get("scope_type") or "")
        requested_scope = str(scope_type or "")
        aliases = {
            "factor": {"factor", "factor_weight", "runtime", "alpha_weight_policy"},
            "factor_weight": {"factor", "factor_weight", "runtime", "alpha_weight_policy"},
            "parameter_template": {"parameter_template", "context_policy"},
            "context_policy": {"context_policy", "parameter_template"},
            "supervisor_template": {"supervisor_template", "position_supervisor_template"},
        }
        if command_scope not in aliases.get(requested_scope, {requested_scope}):
            return False
        command_key = str(row.get("scope_key") or "")
        requested_key = str(scope_key or "")
        return not command_key or command_key in cls.BROAD_SCOPE_KEYS or not requested_key or command_key == requested_key

    @classmethod
    def _action_matches(cls, row: dict[str, Any], action: str) -> bool:
        command_action = str(row.get("action") or "")
        accepted = cls.ACTION_ALIASES.get(str(action), {str(action)})
        return not command_action or command_action in accepted

    @staticmethod
    def _blocked(reason: str, **payload: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "allowed": False,
            "status": reason,
            "reason": reason,
            **payload,
            "boundary": V16CommandGate.boundary(),
        }
