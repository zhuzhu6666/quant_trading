from __future__ import annotations

from scripts.canonical_v2_legacy_backfill import (
    _classify_sample_source,
    _classify_source_key,
)


def test_source_fact_requires_a_stable_legacy_key() -> None:
    assert _classify_source_key(table="trade_outcome_review", key="review-1") == (
        "source_event",
        "exact",
        "",
    )
    assert _classify_source_key(table="trade_outcome_review", key="") == (
        "quarantine",
        "unresolved",
        "empty_legacy_primary_key",
    )


def test_projection_mapping_requires_known_source_reference() -> None:
    assert _classify_sample_source(
        source_table="trade_outcome_review",
        source_id="review-1",
    ) == ("projection_reference", "strong", "")
    assert _classify_sample_source(source_table="", source_id="sample-1")[1:] == (
        "unresolved",
        "missing_projection_source_reference",
    )
    assert _classify_sample_source(
        source_table="unknown_table",
        source_id="row-1",
    )[1:] == ("unresolved", "unsupported_projection_source_table")
    assert _classify_sample_source(
        source_table="trade_outcome_review",
        source_id="missing-review",
        source_exists=False,
    )[1:] == ("unresolved", "missing_projection_source_row")
