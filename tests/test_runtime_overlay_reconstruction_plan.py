from scripts.runtime_overlay_reconstruction_plan import build_reconstruction_plan


def _row(overlay):
    import json

    return {
        "overlay_id": "autonomous_factor_governance",
        "overlay_json": json.dumps(overlay),
        "overlay_hash": "hash",
        "source": "legacy",
        "mutation_id": "",
        "legacy_authority_json": "{}",
        "updated_at": 1.0,
    }


def test_plan_is_read_only_and_requires_typed_mutation_for_expansion():
    plan = build_reconstruction_plan(
        base_config={
            "autonomy_mode": "demo_autonomous",
            "factor_portfolio_weights": {"stable": 0.1},
        },
        overlay_row=_row(
            {
                "autonomy_mode": "demo_nursery",
                "factor_portfolio_weights": {"stable": 0.1, "new": 0.3},
            }
        ),
        target_mode="demo_nursery",
    )

    by_key = {item["key"]: item for item in plan["controls"]}
    assert plan["read_only"] is True
    assert plan["restart_authority_ready"] is False
    assert (
        by_key["autonomy_mode"]["recommended_action"]
        == "legacy_quarantine_or_typed_mutation"
    )
    assert by_key["factor_portfolio_weights"]["requires_committed_mutation"] is True


def test_autonomous_target_drops_legacy_nursery_override():
    plan = build_reconstruction_plan(
        base_config={"autonomy_mode": "demo_autonomous"},
        overlay_row=_row({"autonomy_mode": "demo_nursery"}),
        target_mode="demo_autonomous",
    )

    control = plan["controls"][0]
    assert control["key"] == "autonomy_mode"
    assert control["recommended_action"] == "drop_legacy_override"
    assert control["requires_committed_mutation"] is False
