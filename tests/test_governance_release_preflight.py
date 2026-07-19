from __future__ import annotations

from backend.services.governance_release_preflight import (
    collect_governance_release_preflight,
)


class _Cursor:
    description = None

    def __init__(self, rows):
        self.rows = list(rows)
        self.closed = False

    def execute(self, _sql):
        return None

    def fetchall(self):
        return list(self.rows)

    def close(self):
        self.closed = True


class _Conn:
    def __init__(self, rows):
        self.cursor_value = _Cursor(rows)
        self.closed = False

    def cursor(self):
        return self.cursor_value

    def close(self):
        self.closed = True


def _committed(mutation_id: str, *, risk_class: str = "risk_tightening") -> dict:
    row = {
        "mutation_id": mutation_id,
        "risk_class": risk_class,
        "status": "committed",
        "projection_status": "current",
        "v16_command_id": "",
        "committed_config_hash": f"config-{mutation_id}",
        "domain_hash": f"domain-{mutation_id}",
        "bound_v16_command_id": None,
        "v16_claim_status": None,
        "finalized_mutation_id": None,
        "finalized_config_hash": None,
        "finalized_domain_hash": None,
    }
    if risk_class == "risk_expanding":
        row.update(
            v16_command_id=f"v16-{mutation_id}",
            bound_v16_command_id=f"v16-{mutation_id}",
            v16_claim_status="finalized",
            finalized_mutation_id=mutation_id,
            finalized_config_hash=f"config-{mutation_id}",
            finalized_domain_hash=f"domain-{mutation_id}",
        )
    return row


def test_governance_release_preflight_accepts_tightening_without_v16_and_bound_expansion():
    conn = _Conn(
        [
            _committed("tightening"),
            _committed("expanding", risk_class="risk_expanding"),
        ]
    )

    result = collect_governance_release_preflight(conn_factory=lambda: conn)

    assert result["ok"] is True
    assert result["committed_count"] == 2
    assert result["expanding_count"] == 1
    assert conn.cursor_value.closed is True
    assert conn.closed is True


def test_governance_release_preflight_blocks_every_integrity_failure():
    prepared = {"mutation_id": "prepared", "status": "prepared"}
    degraded = _committed("degraded")
    degraded["projection_status"] = "degraded"
    missing_hash = _committed("missing-hash")
    missing_hash["domain_hash"] = ""
    invalid_v16 = _committed("bad-v16", risk_class="risk_expanding")
    invalid_v16["finalized_config_hash"] = "wrong"
    conn = _Conn([prepared, degraded, missing_hash, invalid_v16])

    result = collect_governance_release_preflight(conn_factory=lambda: conn)

    assert result["ok"] is False
    assert result["blockers"] == [
        "committed_governance_hash_binding_missing",
        "committed_governance_projection_not_current",
        "expanding_governance_v16_binding_invalid",
        "governance_mutation_in_flight",
    ]
    assert result["in_flight_mutation_ids"] == ["prepared"]
    assert result["degraded_projection_mutation_ids"] == ["degraded"]
    assert result["missing_hash_mutation_ids"] == ["missing-hash"]
    assert result["invalid_v16_binding_mutation_ids"] == ["bad-v16"]


def test_governance_release_preflight_fails_closed_when_postgres_is_unavailable():
    def fail():
        raise RuntimeError("postgres_unavailable")

    result = collect_governance_release_preflight(conn_factory=fail)

    assert result["ok"] is False
    assert result["status"] == "error"
    assert result["blockers"] == ["governance_release_preflight_error"]
