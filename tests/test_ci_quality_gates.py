from pathlib import Path


WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "quality-gates.yml"


def test_direct_main_push_runs_all_test_steps() -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert "push:\n    branches: [main]" in workflow
    assert "github.event_name != 'push'" not in workflow
    for step_name in (
        "Full pytest suite",
        "PostgreSQL integration",
        "Web tests",
    ):
        assert f"- name: {step_name}" in workflow
