"""monitor/evolution_story — package for evolution event management.

Re-exports the core EvolutionStory singleton from sibling module
``core.py``, plus the EvolutionReport generator for daily/weekly summaries.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from monitor.evolution_story.core import EvolutionStory  # noqa: F401

# ── Lazy import of report to avoid circular dependency ─────────
if TYPE_CHECKING:
    from monitor.evolution_story.report import EvolutionReport  # noqa: F401


def __getattr__(name: str):
    """Lazy-access ``EvolutionReport`` to break any import ordering issues."""
    if name == "EvolutionReport":
        from monitor.evolution_story.report import EvolutionReport as _cls
        return _cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "EvolutionStory",
    "EvolutionReport",
]
