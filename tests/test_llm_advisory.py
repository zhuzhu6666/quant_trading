import json

from research.llm_advisory import LLMAdvisoryService


def test_llm_advisory_disabled_without_api_config(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_BASE_URL", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.delenv("LLM_MODEL", raising=False)

    service = LLMAdvisoryService(tmp_path / "state.db")
    result = service.run(
        task_type="trade_review",
        target_type="position",
        target_id="p1",
        context={"pnl": -1.2, "reason": "thesis_broken"},
    )

    assert result["ok"] is False
    assert result["status"] == "disabled"
    assert result["advisory_only"] is True
    assert result["permission"]["ok"] is True
    assert "missing_llm_api_config" in result["error"]

    audits = service.list_audits(target_type="position", target_id="p1")
    assert audits["count"] == 1
    assert audits["items"][0]["status"] == "disabled"
    assert audits["items"][0]["result"]["advisory_only"] is True


def test_llm_advisory_dry_run_builds_prompt_and_audit(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "review-model")

    service = LLMAdvisoryService(tmp_path / "state.db")
    result = service.run(
        task_type="meta_decision",
        context={"decision": {"posture": "contract", "risk_score": 0.8}},
        dry_run=True,
    )

    assert result["status"] == "dry_run"
    assert result["prompt"]["schema_version"] == "llm_prompt.v1"
    assert result["prompt"]["capabilities"]["can_place_orders"] is False
    assert result["permission"]["capabilities"]["live_trading"] is False


def test_llm_advisory_openai_compatible_response_is_audited(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "review-model")

    def fake_call(self, *, prompt, max_tokens, temperature):
        assert prompt["schema_version"] == "llm_prompt.v1"
        return {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "summary": "需要收缩观察",
                                "evidence": ["risk_score high"],
                                "risks": ["late session"],
                                "review_next_steps": ["governor review"],
                                "forbidden_actions_ack": True,
                            },
                            ensure_ascii=False,
                        )
                    }
                }
            ]
        }

    monkeypatch.setattr(LLMAdvisoryService, "_call_openai_compatible", fake_call)

    service = LLMAdvisoryService(tmp_path / "state.db")
    result = service.run(
        task_type="risk_ops_summary",
        target_type="day",
        target_id="2026-06-27",
        context={"risk": {"status": "degraded"}},
    )

    assert result["ok"] is True
    assert result["status"] == "ok"
    assert result["parsed"]["summary"] == "需要收缩观察"
    assert result["advisory_only"] is True

    audits = service.list_audits(status="ok")
    assert audits["count"] == 1
    assert audits["items"][0]["task_type"] == "risk_ops_summary"
    assert audits["items"][0]["result"]["parsed"]["forbidden_actions_ack"] is True


def test_llm_advisory_falls_back_to_reasoning_content(tmp_path, monkeypatch):
    monkeypatch.setenv("LLM_API_BASE_URL", "https://example.invalid/v1")
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("LLM_MODEL", "reasoning-model")

    def fake_call(self, *, prompt, max_tokens, temperature):
        return {
            "choices": [
                {
                    "message": {
                        "content": "",
                        "reasoning_content": "reasoning-only response",
                    }
                }
            ]
        }

    monkeypatch.setattr(LLMAdvisoryService, "_call_openai_compatible", fake_call)

    service = LLMAdvisoryService(tmp_path / "state.db")
    result = service.run(task_type="meta_decision", context={"risk_score": 0.4})

    assert result["ok"] is True
    assert result["content"] == "reasoning-only response"
    assert result["reasoning_content"] == "reasoning-only response"
