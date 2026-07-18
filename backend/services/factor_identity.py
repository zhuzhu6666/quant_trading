"""Compatibility re-export for the alpha-owned stable factor identity API."""

from alpha.factor_identity import (
    FACTOR_IDENTITY_VERSION,
    canonical_factor_ast,
    canonical_factor_ast_json,
    canonical_factor_id,
    factor_definition_fingerprint,
)

__all__ = [
    "FACTOR_IDENTITY_VERSION",
    "canonical_factor_ast",
    "canonical_factor_ast_json",
    "canonical_factor_id",
    "factor_definition_fingerprint",
]
