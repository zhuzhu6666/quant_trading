from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import httpx

from backend.core.db import STATE_DB, connect_sqlite, get_state_pg_conn, is_state_db_path
from backend.core.state_store import (
    is_state_schema_write_sql,
    validate_runtime_state_schema,
)
from backend.services.agent_authority_registry import AgentAuthorityRegistryService
from backend.services.model_permissions import validate_model_artifact


MODEL_TYPE = "llm_advisory_api"


def _json_dumps(value: Any) -> str:
    return json.dumps(value if value is not None else {}, ensure_ascii=False, sort_keys=True, default=str)


def _loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except Exception:
        return default


def _clip_text(value: Any, limit: int = 12000) -> str:
    text = str(value if value is not None else "")
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


class LLMAdvisoryService:
    """Advisory-only LLM API layer for explanations and review summaries."""

    def __init__(
        self,
        db_path: str | Path = STATE_DB,
        *,
        provider: str | None = None,
        model: str | None = None,
        base_url: str | None = None,
        api_key: str | None = None,
        timeout_sec: float | None = None,
    ):
        self.db_path = Path(db_path)
        self.provider = provider or os.getenv("LLM_PROVIDER", "openai_compatible")
        self.model = model or os.getenv("LLM_MODEL", "")
        self.base_url = (base_url or os.getenv("LLM_API_BASE_URL", "")).rstrip("/")
        self.api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.timeout_sec = float(timeout_sec or os.getenv("LLM_TIMEOUT_SEC", "30") or 30)
        self.max_prompt_chars = max(12000, _safe_int(os.getenv("LLM_MAX_PROMPT_CHARS"), 200000))
        self.default_max_tokens = max(1, _safe_int(os.getenv("LLM_DEFAULT_MAX_TOKENS"), 4096))
        self.max_output_tokens = max(self.default_max_tokens, _safe_int(os.getenv("LLM_MAX_OUTPUT_TOKENS"), 32768))
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        conn = get_state_pg_conn() if self._use_pg() else connect_sqlite(self.db_path)
        if not self._use_pg():
            conn.row_factory = sqlite3.Row
        return conn

    def _use_pg(self) -> bool:
        return is_state_db_path(self.db_path)

    def _sql(self, sql: str) -> str:
        return sql.replace("?", "%s") if self._use_pg() else sql

    def _execute(self, conn, sql: str, params: tuple | list | None = None):
        rendered = self._sql(sql)
        if self._use_pg() and is_state_schema_write_sql(rendered):
            return validate_runtime_state_schema(conn, rendered)
        if params is None:
            return conn.execute(rendered)
        return conn.execute(rendered, tuple(params))

    def _ensure_table(self) -> None:
        conn = self._conn()
        try:
            self._execute(conn,
                """
                CREATE TABLE IF NOT EXISTS llm_advisory_audit (
                    audit_id TEXT PRIMARY KEY,
                    provider TEXT DEFAULT '',
                    model TEXT DEFAULT '',
                    task_type TEXT DEFAULT '',
                    target_type TEXT DEFAULT '',
                    target_id TEXT DEFAULT '',
                    status TEXT DEFAULT '',
                    prompt_json TEXT DEFAULT '{}',
                    response_json TEXT DEFAULT '{}',
                    result_json TEXT DEFAULT '{}',
                    error TEXT DEFAULT '',
                    created_at REAL NOT NULL DEFAULT 0.0
                )
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_llm_advisory_audit_created
                ON llm_advisory_audit(created_at)
                """
            )
            self._execute(conn,
                """
                CREATE INDEX IF NOT EXISTS idx_llm_advisory_audit_target
                ON llm_advisory_audit(target_type, target_id, created_at)
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def artifact() -> dict[str, Any]:
        return {
            "schema_version": "llm_advisory_api_artifact.v1",
            "model_type": MODEL_TYPE,
            "capabilities": {
                "live_trading": False,
                "advisory_only": True,
                "shadow_only": True,
                "can_place_orders": False,
                "can_close_positions": False,
                "can_change_risk_limits": False,
                "can_increase_hard_risk_limits": False,
                "can_change_factor_weights": False,
                "can_bypass_risk_policy": False,
                "can_apply_policy_without_review": False,
                "can_release_market_connection": False,
            },
            "notes": [
                "LLM is explanation/review only.",
                "LLM output is not a trading signal and cannot mutate live policy.",
            ],
        }

    def run(
        self,
        *,
        task_type: str,
        context: dict[str, Any],
        target_type: str = "",
        target_id: str = "",
        dry_run: bool = False,
        max_tokens: int | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        permission = validate_model_artifact(
            self.artifact(),
            model_type=MODEL_TYPE,
            db_path=self.db_path,
            context={"operation": "llm_advisory_run", "task_type": task_type},
        )
        prompt = self.build_prompt(task_type=task_type, context=context)
        output_tokens = min(
            max(1, int(max_tokens if max_tokens is not None else self.default_max_tokens)),
            self.max_output_tokens,
        )
        status = "disabled"
        response: dict[str, Any] = {}
        result: dict[str, Any] = {}
        error = ""

        if not permission.get("ok"):
            status = "blocked"
            error = "model_permission_violation"
            result = {
                "ok": False,
                "error": error,
                "permission": permission,
                "advisory_only": True,
            }
        elif dry_run:
            status = "dry_run"
            result = self._disabled_result(
                reason="dry_run",
                prompt=prompt,
                permission=permission,
            )
        elif not self.base_url or not self.api_key or not self.model:
            missing = [
                name
                for name, value in {
                    "LLM_API_BASE_URL": self.base_url,
                    "LLM_API_KEY": self.api_key,
                    "LLM_MODEL": self.model,
                }.items()
                if not value
            ]
            error = "missing_llm_api_config:" + ",".join(missing)
            result = self._disabled_result(reason=error, prompt=prompt, permission=permission)
        else:
            try:
                response = self._call_openai_compatible(
                    prompt=prompt,
                    max_tokens=output_tokens,
                    temperature=temperature,
                )
                result = self._result_from_response(response=response, permission=permission)
                status = "ok"
            except Exception as exc:
                status = "error"
                error = str(exc)
                result = {
                    "ok": False,
                    "schema_version": "llm_advisory_result.v1",
                    "model_type": MODEL_TYPE,
                    "provider": self.provider,
                    "model": self.model,
                    "error": error,
                    "advisory_only": True,
                    "permission": permission,
                }

        audit = self._persist(
            provider=self.provider,
            model=self.model,
            task_type=task_type,
            target_type=target_type,
            target_id=target_id,
            status=status,
            prompt=prompt,
            response=response,
            result=result,
            error=error,
        )
        return {
            **result,
            "audit": audit,
            "status": status,
            "max_tokens": output_tokens,
        }

    def build_prompt(self, *, task_type: str, context: dict[str, Any]) -> dict[str, Any]:
        task = str(task_type or "review_summary")
        system = (
            "You are an advisory-only trading review assistant. "
            "Explain evidence from structured context. "
            "Do not recommend direct order placement, direct close actions, direct risk limit increases, "
            "factor-weight changes, or bypassing RiskPolicyService/Governor. "
            "Return concise JSON with summary, evidence, risks, and review_next_steps."
        )
        task_instructions = {
            "trade_review": "Explain whether the trade outcome was mainly entry, exit, timing, regime, parameter, or execution related.",
            "meta_decision": "Explain the meta-model posture and whether risk budget or trade frequency advice needs human/governor review.",
            "governance_review": "Summarize governance suggestions and identify what should be reviewed before any release.",
            "risk_ops_summary": "Summarize current risk and operations state for a human operator.",
            "factor_review": "Explain factor evidence and whether the factor should be observed, frozen, or reviewed.",
        }.get(task, "Summarize the context for human review.")
        user = {
            "task_type": task,
            "instruction": task_instructions,
            "output_contract": {
                "schema_version": "llm_advisory.v1",
                "advisory_only": True,
                "fields": ["summary", "evidence", "risks", "review_next_steps", "forbidden_actions_ack"],
            },
            "context": context,
        }
        return {
            "schema_version": "llm_prompt.v1",
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": _clip_text(_json_dumps(user), self.max_prompt_chars)},
            ],
            "task_type": task,
            "capabilities": self.artifact()["capabilities"],
            "limits": {
                "max_prompt_chars": self.max_prompt_chars,
                "default_max_tokens": self.default_max_tokens,
                "max_output_tokens": self.max_output_tokens,
            },
        }

    def list_audits(
        self,
        *,
        limit: int = 50,
        task_type: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        clauses = []
        params: list[Any] = []
        for col, value in (
            ("task_type", task_type),
            ("target_type", target_type),
            ("target_id", target_id),
            ("status", status),
        ):
            if value:
                clauses.append(f"{col}=?")
                params.append(str(value))
        where = "WHERE " + " AND ".join(clauses) if clauses else ""
        conn = self._conn()
        try:
            rows = self._execute(conn,
                f"""
                SELECT *
                FROM llm_advisory_audit
                {where}
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (*params, int(limit)),
            ).fetchall()
            items = []
            for row in rows:
                items.append(
                    {
                        "audit_id": str(row["audit_id"] or ""),
                        "provider": str(row["provider"] or ""),
                        "model": str(row["model"] or ""),
                        "task_type": str(row["task_type"] or ""),
                        "target_type": str(row["target_type"] or ""),
                        "target_id": str(row["target_id"] or ""),
                        "status": str(row["status"] or ""),
                        "prompt": _loads(row["prompt_json"], {}),
                        "response": _loads(row["response_json"], {}),
                        "result": _loads(row["result_json"], {}),
                        "error": str(row["error"] or ""),
                        "created_at": _safe_float(row["created_at"]),
                    }
                )
            return {"items": items, "count": len(items)}
        finally:
            conn.close()

    def _call_openai_compatible(
        self,
        *,
        prompt: dict[str, Any],
        max_tokens: int,
        temperature: float,
    ) -> dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": self.model,
            "messages": prompt["messages"],
            "temperature": float(temperature),
            "max_tokens": int(max_tokens),
        }
        with httpx.Client(timeout=self.timeout_sec) as client:
            resp = client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            return resp.json()

    def _result_from_response(self, *, response: dict[str, Any], permission: dict[str, Any]) -> dict[str, Any]:
        choices = list(response.get("choices") or [])
        content = ""
        reasoning_content = ""
        if choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "")
            reasoning_content = str(message.get("reasoning_content") or "")
            if not content and reasoning_content:
                content = reasoning_content
        parsed = _loads(content, {})
        return {
            "ok": True,
            "schema_version": "llm_advisory_result.v1",
            "model_type": MODEL_TYPE,
            "provider": self.provider,
            "model": self.model,
            "content": content,
            "reasoning_content": reasoning_content,
            "parsed": parsed if isinstance(parsed, dict) else {},
            "advisory_only": True,
            "permission": permission,
            "guardrails": self.artifact()["notes"],
        }

    def _disabled_result(
        self,
        *,
        reason: str,
        prompt: dict[str, Any],
        permission: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": "llm_advisory_result.v1",
            "model_type": MODEL_TYPE,
            "provider": self.provider,
            "model": self.model,
            "error": reason,
            "prompt": prompt,
            "advisory_only": True,
            "permission": permission,
        }

    def _persist(
        self,
        *,
        provider: str,
        model: str,
        task_type: str,
        target_type: str,
        target_id: str,
        status: str,
        prompt: dict[str, Any],
        response: dict[str, Any],
        result: dict[str, Any],
        error: str,
    ) -> dict[str, Any]:
        now = time.time()
        audit_id = f"llm:{task_type or 'task'}:{int(now * 1000)}"
        result_payload = dict(result or {})
        result_payload.setdefault(
            "authority_verdict",
            AgentAuthorityRegistryService().evaluate_scope_write(
                "llm_advisory",
                target_type or "llm_advisory",
                task_type or "advisory_review",
                requested_writes=["llm_advisory_audit"],
                status=status,
                impact_level="observe",
            ),
        )
        result_payload.setdefault("source_agent", "llm_advisory")
        conn = self._conn()
        try:
            self._execute(conn,
                """
                INSERT INTO llm_advisory_audit
                (audit_id, provider, model, task_type, target_type, target_id, status,
                 prompt_json, response_json, result_json, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    audit_id,
                    str(provider or ""),
                    str(model or ""),
                    str(task_type or ""),
                    str(target_type or ""),
                    str(target_id or ""),
                    str(status or ""),
                    _json_dumps(prompt),
                    _json_dumps(response),
                    _json_dumps(result_payload),
                    str(error or ""),
                    now,
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return {
            "audit_id": audit_id,
            "provider": str(provider or ""),
            "model": str(model or ""),
            "task_type": str(task_type or ""),
            "target_type": str(target_type or ""),
            "target_id": str(target_id or ""),
            "status": str(status or ""),
            "error": str(error or ""),
            "created_at": now,
        }
