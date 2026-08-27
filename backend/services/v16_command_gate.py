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

from backend.core.db import (
    STATE_DB,
    connect_sqlite,
    get_state_pg_conn,
    is_state_db_path,
    state_table_columns,
    state_table_exists,
)
from backend.services._brain_helpers import connect, execute, loads, safe_float
from backend.services.brain_governance_candidates import (
    CANDIDATE_EXECUTION_PENDING_STATUSES,
    CANDIDATE_REVIEWABLE_STATUSES,
)

class V16CommandGate:
    """Resolve a current V16 delegation before a system-owned mutation."""

    BROAD_SCOPE_KEYS = {
        "",
        "*",
        "alpha_weight_policy",
        "online_light",
        "threshold_and_sizing",
        "position_supervisor",
        "position_supervisor_selection",
    }
    ACTION_ALIASES = {
        "update_weight": {"update_weight", "downweight", "boost_small", "boost"},
        "downweight": {"update_weight", "downweight"},
        "promote_factor": {"promote_factor", "update_weight", "factor_governance_cycle"},
        "register_shadow_factor": {
            "register_shadow_factor",
            "promote_factor",
            "factor_governance_cycle",
        },
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
        "switch_position_supervisor_selection_mode": {
            "switch_position_supervisor_selection_mode",
        },
    }

    @staticmethod
    def boundary() -> dict[str, Any]:
        return {
            "schema_version": "v16_command_gate.v2",
            "v16_is_decision_owner": True,
            "specialist_is_execution_owner": True,
            "requires_recent_delegate": True,
            "command_claim_is_single_use": True,
            "single_actionable_predicate": True,
            "command_state_machine": ["available", "claimed", "finalized"],
            "apply_count_increments_on_finalize_only": True,
            "evidence_binding_supported": True,
            "candidate_lifecycle_binding": True,
            "factor_authorization_granularity": "run_batch_fixed_manifest",
            "fail_closed_when_command_missing": True,
            "does_not_mutate_runtime": True,
            "does_not_submit_orders": True,
            "risk_reduction_may_use_existing_rollback_path": True,
        }

    @classmethod
    def is_actionable(
        cls,
        item: dict[str, Any],
        *,
        now: float | None = None,
        max_age_seconds: float | None = None,
    ) -> bool:
        checked_at = float(now if now is not None else time.time())
        age_limit = max(
            60.0,
            float(
                max_age_seconds
                if max_age_seconds is not None
                else cls._max_age_seconds()
            ),
        )
        authority_issued_at = cls._authority_issued_at(item)
        return bool(
            str(item.get("decision") or "") == "delegate"
            and str(item.get("claim_status") or "available") == "available"
            and authority_issued_at > 0.0
            and checked_at - authority_issued_at <= age_limit
            and int(item.get("apply_count") or 0)
            < max(1, int(item.get("max_apply_count") or 1))
        )

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
        candidate_id: str = "",
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

        age_limit = max(
            60.0,
            float(
                max_age_seconds
                if max_age_seconds is not None
                else cls._max_age_seconds()
            ),
        )

        try:
            cls.ensure_finalize_schema(db_path)
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
                       authority_issued_at, created_at, updated_at
                FROM v16_brain_command
                WHERE target_agent=? AND decision='delegate'
                  AND authority_issued_at>=?
                ORDER BY authority_issued_at DESC, created_at DESC
            """
            if is_state_db_path(db_path):
                sql = sql.replace("?", "%s")
            rows = conn.execute(
                sql,
                (str(target_agent), now - age_limit),
            ).fetchall()
            requested_id = str(command_id or "")
            for row in rows:
                item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
                if requested_id and str(item.get("command_id") or "") != requested_id:
                    continue
                if candidate_id and str(item.get("candidate_id") or "") != str(candidate_id):
                    continue
                if not cls.is_actionable(
                    item,
                    now=now,
                    max_age_seconds=age_limit,
                ):
                    continue
                if not cls._candidate_binding_is_valid(conn, item):
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
                authority_issued_at = cls._authority_issued_at(item)
                age = max(0.0, now - authority_issued_at)
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
                    "authority_issued_at": authority_issued_at,
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
            cls.ensure_finalize_schema(db_path)
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
            where = "target_agent=? AND decision='delegate' AND authority_issued_at>=?"
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
                           authority_issued_at, created_at, updated_at
                    FROM v16_brain_command
                    WHERE {where}
                    ORDER BY authority_issued_at DESC, created_at DESC
                    """,
                tuple(params),
            ).fetchall()
            for row in rows:
                item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
                if not cls.is_actionable(item, now=now):
                    continue
                if not cls._candidate_binding_is_valid(conn, item):
                    continue
                if not cls._scope_matches(item, scope_type=scope_type, scope_key=scope_key):
                    continue
                if action and not cls._action_matches(item, action):
                    # 与 authorize() 保持一致:cycle 级 broad 命令(scope_type 为
                    # factor_weight/parameter_template/context_policy/
                    # supervisor_template)授权给 specialist 控制面,specialist
                    # 内部再走更窄的 RiskPolicy/DecisionPolicy action 门。
                    # 若这里直接 continue,cycle 命令(如 factor_governance_cycle)
                    # 永远无法被 update_weight/promote_factor 消费 —— 命令创建、
                    # 授权全部通过,但 runtime_mutation claim 必败
                    # (aborted:v16_command_required,apply_count=0 死锁)。
                    command_scope = str(item.get("scope_type") or "")
                    if command_scope not in {
                        "factor_weight",
                        "parameter_template",
                        "context_policy",
                        "supervisor_template",
                    }:
                        continue
                if candidate_id and str(item.get("candidate_id") or "") != str(candidate_id):
                    continue
                if posterior_fingerprint and str(item.get("posterior_fingerprint") or "") != str(posterior_fingerprint):
                    continue
                if evidence_fingerprint and str(item.get("evidence_fingerprint") or "") != str(evidence_fingerprint):
                    continue
                apply_count = int(item.get("apply_count") or 0)
                max_apply_count = max(1, int(item.get("max_apply_count") or 1))
                token = f"v16claim_{uuid.uuid4().hex}"
                claimed_until = now + ttl
                claimed = execute(
                    conn,
                    """UPDATE v16_brain_command
                       SET claim_status='claimed', claim_token=?, claimed_at=?,
                           claim_expires_at=?, claim_attempts=claim_attempts+1,
                           updated_at=?
                       WHERE command_id=? AND decision='delegate'
                         AND claim_status='available' AND apply_count < max_apply_count
                         AND authority_issued_at>=?""",
                    (
                        token,
                        now,
                        claimed_until,
                        now,
                        str(item.get("command_id") or ""),
                        now - cls._max_age_seconds(),
                    ),
                )
                if int(getattr(claimed, "rowcount", 0) or 0) != 1:
                    continue
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
                    "authority_issued_at": cls._authority_issued_at(item),
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
    def ensure_finalize_schema(cls, db_path: str | Path = STATE_DB) -> None:
        """Verify production migration fields; add them only to SQLite fixtures."""
        from backend.services.v16_brain_orchestrator import ensure_v16_brain_command_table

        if not is_state_db_path(db_path):
            ensure_v16_brain_command_table(db_path)
        conn = connect(db_path)
        required = {
            "finalized_at": "REAL NOT NULL DEFAULT 0.0",
            "finalized_mutation_id": "TEXT NOT NULL DEFAULT ''",
            "finalized_config_hash": "TEXT NOT NULL DEFAULT ''",
            "finalized_domain_hash": "TEXT NOT NULL DEFAULT ''",
            "claim_attempts": "INTEGER NOT NULL DEFAULT 0",
            "failure_reason": "TEXT NOT NULL DEFAULT ''",
            "authority_issued_at": "REAL NOT NULL DEFAULT 0.0",
        }
        try:
            if not state_table_exists(conn, "v16_brain_command"):
                raise RuntimeError("v16_finalize_schema_missing:v16_brain_command")
            existing = state_table_columns(conn, "v16_brain_command")
            missing = sorted(set(required) - existing)
            if missing and is_state_db_path(db_path):
                raise RuntimeError(f"v16_finalize_schema_missing:{','.join(missing)}")
            for name in missing:
                execute(conn, f'ALTER TABLE v16_brain_command ADD COLUMN "{name}" {required[name]}')
            if not is_state_db_path(db_path):
                execute(
                    conn,
                    """UPDATE v16_brain_command
                       SET authority_issued_at=CASE
                           WHEN created_at>0.0 THEN created_at ELSE updated_at END
                       WHERE authority_issued_at<=0.0""",
                )
            if missing or not is_state_db_path(db_path):
                conn.commit()
        finally:
            conn.close()

    @classmethod
    def validate_claim_in_transaction(
        cls,
        conn: Any,
        *,
        command_id: str,
        claim_token: str,
        target_agent: str,
        scope_type: str,
        scope_key: str,
        action: str,
        candidate_id: str = "",
        posterior_fingerprint: str = "",
        evidence_fingerprint: str = "",
        mutation_id: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """Revalidate all delegation bindings inside the mutation transaction."""
        if not command_id or not claim_token:
            return cls._blocked("v16_command_claim_missing", command_id=command_id)
        row = execute(
            conn,
            """SELECT command_id, candidate_id, target_agent, scope_type, scope_key,
                      action, claim_status, claim_token, claim_expires_at,
                      posterior_fingerprint, evidence_fingerprint,
                      authority_issued_at, created_at
               FROM v16_brain_command WHERE command_id=?""",
            (str(command_id),),
        ).fetchone()
        if not row:
            return cls._blocked("v16_command_claim_not_found", command_id=command_id)
        item = {key: row[key] for key in row.keys()} if hasattr(row, "keys") else dict(row)
        current_time = float(now if now is not None else time.time())
        if not cls._candidate_binding_is_valid(
            conn,
            item,
            mutation_id=mutation_id,
        ):
            return cls._blocked("v16_command_candidate_binding_invalid", command_id=command_id)
        if str(item.get("claim_status") or "") != "claimed":
            return cls._blocked("v16_command_not_claimed", command_id=command_id)
        if str(item.get("claim_token") or "") != str(claim_token):
            return cls._blocked("v16_command_claim_token_mismatch", command_id=command_id)
        if safe_float(item.get("claim_expires_at")) <= current_time:
            return cls._blocked("v16_command_claim_expired", command_id=command_id)
        authority_issued_at = cls._authority_issued_at(item)
        if (
            authority_issued_at <= 0.0
            or current_time - authority_issued_at > cls._max_age_seconds()
        ):
            return cls._blocked("v16_command_authority_expired", command_id=command_id)
        if target_agent and str(item.get("target_agent") or "") != str(target_agent):
            return cls._blocked("v16_command_target_agent_mismatch", command_id=command_id)
        if not cls._scope_matches(item, scope_type=scope_type, scope_key=scope_key):
            return cls._blocked("v16_command_scope_mismatch", command_id=command_id)
        if action and not cls._action_matches(item, action):
            # 与 authorize()(:191-197)/claim()(:318-334) 保持一致:cycle 级 broad
            # 命令(scope_type 为 factor_weight/parameter_template/context_policy/
            # supervisor_template)授权给 specialist 控制面,事务内复验同样放行
            # 窄 action claim。否则 claim 成功但 validate_claim_in_transaction 硬
            # 失败 → GovernanceMutationError: v16_command_action_mismatch,
            # apply_count=0,命令已 claim 却永远无法消费(死锁)。
            command_scope = str(item.get("scope_type") or "")
            if command_scope not in {
                "factor_weight",
                "parameter_template",
                "context_policy",
                "supervisor_template",
            }:
                return cls._blocked("v16_command_action_mismatch", command_id=command_id)
        for field, expected in (
            ("candidate_id", candidate_id),
            ("posterior_fingerprint", posterior_fingerprint),
            ("evidence_fingerprint", evidence_fingerprint),
        ):
            if expected and str(item.get(field) or "") != str(expected):
                return cls._blocked(f"v16_command_{field}_mismatch", command_id=command_id)
        return {
            "ok": True,
            "allowed": True,
            "status": "v16_command_claim_binding_valid",
            "command_id": str(command_id),
            "authority_issued_at": authority_issued_at,
            "boundary": cls.boundary(),
        }

    @classmethod
    def finalize_in_transaction(
        cls,
        conn: Any,
        *,
        command_id: str,
        claim_token: str,
        mutation_id: str,
        config_hash: str,
        domain_hash: str,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Finalize exactly once using the caller's open database transaction."""
        if not command_id or not claim_token or not mutation_id:
            return cls._blocked("v16_command_claim_missing", command_id=command_id)
        finalized_at = float(now if now is not None else time.time())
        existing = execute(
            conn,
            """SELECT claim_status, apply_count, finalized_mutation_id,
                      finalized_config_hash, finalized_domain_hash,
                      authority_issued_at, created_at
               FROM v16_brain_command WHERE command_id=?""",
            (str(command_id),),
        ).fetchone()
        if existing:
            row = {key: existing[key] for key in existing.keys()} if hasattr(existing, "keys") else dict(existing)
            if str(row.get("claim_status") or "") == "finalized":
                same_binding = (
                    str(row.get("finalized_mutation_id") or "") == str(mutation_id)
                    and str(row.get("finalized_config_hash") or "") == str(config_hash or "")
                    and str(row.get("finalized_domain_hash") or "") == str(domain_hash or "")
                )
                if same_binding:
                    return {
                        "ok": True,
                        "allowed": True,
                        "status": "v16_command_already_finalized",
                        "command_id": str(command_id),
                        "mutation_id": str(mutation_id),
                        "apply_count": int(row.get("apply_count") or 0),
                        "boundary": cls.boundary(),
                    }
                return cls._blocked("v16_command_finalized_for_other_mutation", command_id=command_id)
            authority_issued_at = cls._authority_issued_at(row)
            if (
                authority_issued_at <= 0.0
                or finalized_at - authority_issued_at > cls._max_age_seconds()
            ):
                return cls._blocked(
                    "v16_command_authority_expired",
                    command_id=command_id,
                )
        result = execute(
            conn,
            """UPDATE v16_brain_command
               SET claim_status='finalized', apply_count=apply_count+1,
                   consumed_at=?, consumed_mutation_id=?, finalized_at=?,
                   finalized_mutation_id=?, finalized_config_hash=?,
                   finalized_domain_hash=?, claim_token='', claim_expires_at=0.0,
                   failure_reason='', updated_at=?
               WHERE command_id=? AND claim_status='claimed' AND claim_token=?
                 AND claim_expires_at>? AND apply_count < max_apply_count
                 AND authority_issued_at>=?""",
            (
                finalized_at,
                str(mutation_id),
                finalized_at,
                str(mutation_id),
                str(config_hash or ""),
                str(domain_hash or ""),
                finalized_at,
                str(command_id),
                str(claim_token),
                finalized_at,
                finalized_at - cls._max_age_seconds(),
            ),
        )
        if int(getattr(result, "rowcount", 0) or 0) != 1:
            return cls._blocked("v16_command_finalize_failed", command_id=command_id)
        return {
            "ok": True,
            "allowed": True,
            "status": "v16_command_finalized",
            "command_id": str(command_id),
            "mutation_id": str(mutation_id),
            "config_hash": str(config_hash or ""),
            "domain_hash": str(domain_hash or ""),
            "finalized_at": finalized_at,
            "boundary": cls.boundary(),
        }

    @classmethod
    def finalize(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        command_id: str,
        claim_token: str,
        mutation_id: str,
        config_hash: str,
        domain_hash: str,
    ) -> dict[str, Any]:
        try:
            cls.ensure_finalize_schema(db_path)
            conn = connect(db_path)
        except Exception as exc:
            return cls._blocked("v16_command_store_unavailable", error=f"{type(exc).__name__}: {exc}")
        try:
            if is_state_db_path(db_path):
                execute(conn, "SELECT pg_advisory_xact_lock(821640242)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            result = cls.finalize_in_transaction(
                conn,
                command_id=command_id,
                claim_token=claim_token,
                mutation_id=mutation_id,
                config_hash=config_hash,
                domain_hash=domain_hash,
            )
            if not result.get("allowed"):
                conn.rollback()
                return result
            conn.commit()
            return result
        except Exception as exc:
            conn.rollback()
            return cls._blocked(
                "v16_command_finalize_failed",
                command_id=command_id,
                error=f"{type(exc).__name__}: {exc}",
            )
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

    @classmethod
    def recover_expired_claims(
        cls,
        db_path: str | Path = STATE_DB,
        *,
        now: float | None = None,
    ) -> dict[str, Any]:
        """Release claims left by a dead process without renewing authority."""
        try:
            cls.ensure_finalize_schema(db_path)
            conn = connect(db_path)
        except Exception as exc:
            return {
                "ok": False,
                "status": "v16_claim_recovery_store_unavailable",
                "released_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
                "boundary": cls.boundary(),
            }
        recovered_at = float(now if now is not None else time.time())
        try:
            if is_state_db_path(db_path):
                execute(conn, "SELECT pg_advisory_xact_lock(821640242)")
            else:
                conn.execute("BEGIN IMMEDIATE")
            result = execute(
                conn,
                """UPDATE v16_brain_command
                   SET claim_status='available', claim_token='', claimed_at=0.0,
                       claim_expires_at=0.0,
                       last_release_reason='startup_claim_expired', updated_at=?
                   WHERE claim_status='claimed'
                     AND claim_expires_at>0.0 AND claim_expires_at<=?""",
                (recovered_at, recovered_at),
            )
            conn.commit()
            released = int(getattr(result, "rowcount", 0) or 0)
            return {
                "ok": True,
                "status": "v16_expired_claims_recovered",
                "released_count": released,
                "recovered_at": recovered_at,
                "authority_timestamp_unchanged": True,
                "boundary": cls.boundary(),
            }
        except Exception as exc:
            conn.rollback()
            return {
                "ok": False,
                "status": "v16_claim_recovery_failed",
                "released_count": 0,
                "error": f"{type(exc).__name__}: {exc}",
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

    @staticmethod
    def _authority_issued_at(row: dict[str, Any]) -> float:
        """Return V16-owned issuance time, never mutable claim timestamps."""
        return safe_float(row.get("authority_issued_at")) or safe_float(
            row.get("created_at")
        )

    @classmethod
    def _candidate_binding_is_valid(
        cls,
        conn: Any,
        row: dict[str, Any],
        *,
        mutation_id: str = "",
    ) -> bool:
        """Validate linked candidate lifecycle without constraining synthetic commands.

        Factor batch commands and other specialist commands may use a
        preflight identifier without a brain candidate row.  When a row does
        exist, terminal candidates or superseded bridges must never be
        claimable, even if command cleanup has not run yet.
        """
        candidate_id = str(row.get("candidate_id") or "")
        if not candidate_id or not state_table_exists(conn, "brain_governance_candidate"):
            return True
        candidate = execute(
            conn,
            """SELECT status, submitted_suggestion_id, updated_at
               FROM brain_governance_candidate
               WHERE candidate_id=?
               LIMIT 1""",
            (candidate_id,),
        ).fetchone()
        if not candidate:
            # Synthetic specialist command; it is still protected by its
            # command evidence and mutation transaction.
            return True
        status = str(candidate["status"] or "")
        suggestion_id = str(candidate["submitted_suggestion_id"] or "")
        if status in CANDIDATE_REVIEWABLE_STATUSES and not suggestion_id:
            return False
        if status not in CANDIDATE_EXECUTION_PENDING_STATUSES and status != "applied":
            return False
        if not state_table_exists(conn, "brain_governance_candidate_review"):
            return False
        review_columns = set(state_table_columns(conn, "brain_governance_candidate_review"))
        if not {"bridge_ready", "created_at", "evidence_fingerprint"}.issubset(review_columns):
            return False
        review = execute(
            conn,
            """SELECT bridge_ready, evidence_fingerprint, created_at
               FROM brain_governance_candidate_review
               WHERE candidate_id=?
               ORDER BY created_at DESC
               LIMIT 1""",
            (candidate_id,),
        ).fetchone()
        if not review or not bool(review["bridge_ready"]):
            return False
        if safe_float(review["created_at"]) < safe_float(candidate["updated_at"]):
            return False
        command_evidence = loads(row.get("evidence_json"), {})
        candidate_review_ref = dict(command_evidence.get("candidate_review") or {})
        if (
            not candidate_review_ref
            or str(candidate_review_ref.get("evidence_fingerprint") or "")
            != str(review["evidence_fingerprint"] or "")
        ):
            return False
        if not suggestion_id or not state_table_exists(conn, "policy_suggestion"):
            return False
        columns = set(state_table_columns(conn, "policy_suggestion"))
        required = {"status", "governance_eligible", "applied_mutation_id"}
        if not required.issubset(columns):
            return False
        suggestion = execute(
            conn,
            """SELECT status, governance_eligible, applied_mutation_id
               FROM policy_suggestion
               WHERE suggestion_id=?
               LIMIT 1""",
            (suggestion_id,),
        ).fetchone()
        suggestion_status = str(suggestion["status"] or "") if suggestion else ""
        suggestion_mutation_id = str(suggestion["applied_mutation_id"] or "") if suggestion else ""
        if suggestion_status == "applied":
            return bool(mutation_id and suggestion_mutation_id == str(mutation_id))
        return bool(
            suggestion
            and suggestion_status in {"proposed", "approved"}
            and int(suggestion["governance_eligible"] or 0) == 1
            and not suggestion_mutation_id
        )

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
            "supervisor_selection": {
                "supervisor_selection",
                "supervisor_template",
                "position_supervisor_template",
            },
        }
        if command_scope not in aliases.get(requested_scope, {requested_scope}):
            return False
        command_key = str(row.get("scope_key") or "")
        requested_key = str(scope_key or "")
        if not command_key or command_key in cls.BROAD_SCOPE_KEYS or not requested_key:
            return True
        scope_aliases = {
            "online_light:default": "online_light",
            "online_light": "online_light",
        }
        return scope_aliases.get(command_key, command_key) == scope_aliases.get(
            requested_key,
            requested_key,
        )

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
