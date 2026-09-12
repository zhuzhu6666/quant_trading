"""L0-0R/X1 contract: the single learning_eligible predicate fails closed.

Missing integrity fields are unknown, and unknown is refused instead of
being treated as clean; the close_reason blacklist is defence in depth.
"""

import pytest
from backend.core.contracts import CONTAMINATED_CLOSE_REASONS, learning_eligible

# Smoke: core fail-closed contracts, run on every change (pytest -m smoke).
pytestmark = pytest.mark.smoke


def test_learning_eligible_requires_explicit_full_integrity():
    assert learning_eligible(
        attribution_integrity="full",
        context_integrity="full",
        close_reason="broker_close",
    )

    for attribution in (None, "", "unknown", "missing", "recovered",
                        "restart_affected", "chain_broken"):
        assert not learning_eligible(
            attribution_integrity=attribution,
            context_integrity="full",
            close_reason="broker_close",
        ), attribution

    for context in (None, "", "unknown", "partial", "restart_affected"):
        assert not learning_eligible(
            attribution_integrity="full",
            context_integrity=context,
            close_reason="broker_close",
        ), context


def test_learning_eligible_refuses_contaminated_close_reasons():
    for reason in sorted(CONTAMINATED_CLOSE_REASONS):
        assert not learning_eligible(
            attribution_integrity="full",
            context_integrity="full",
            close_reason=reason,
        ), reason
