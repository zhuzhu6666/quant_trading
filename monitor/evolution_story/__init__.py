"""monitor/evolution_story — package for evolution event management.

Re-exports the core EvolutionStory singleton from the sibling module
*monitor/evolution_story.py* (not the package itself), plus the
EvolutionReport generator for daily/weekly summaries.

NOTE: because the directory *monitor/evolution_story/* and the file
*monitor/evolution_story.py* share the same name, Python's import
system prefers the package.  We use ``importlib`` to load the
sibling module by its filesystem path.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import TYPE_CHECKING

# ── Load the sibling module monitor/evolution_story.py ──────────
# (the one with the EvolutionStory class, not this package)
_src = Path(__file__).resolve().parent.parent / "evolution_story.py"
_spec = importlib.util.spec_from_file_location(
    "monitor._evolution_story_module",
    str(_src),
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["monitor._evolution_story_module"] = _module
_spec.loader.exec_module(_module)

EvolutionStory = _module.EvolutionStory

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
