from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


@dataclass
class StrategyTemplate:
    """A strategy template produced by the strategy GP domain.

    Attributes:
        name: Unique name for this template
        signal_aggregation: How to combine signals ("vote", "weighted_vote", "max", "mean")
        threshold: Minimum signal threshold to trigger a trade (0.0-1.0)
        sizing: Position sizing method ("fixed", "kelly", "risk_pct")
        regime_filter: Optional regime requirement ("all", "trending", "ranging", "volatile")
        score: Evaluation score for this template
        generation: GP generation that produced it
    """

    name: str
    signal_aggregation: str = "vote"
    threshold: float = 0.5
    sizing: str = "fixed"
    regime_filter: str = "all"
    score: float = 0.0
    generation: int = 0


class StrategySearch:
    """GP domain for strategy template search.

    Full GP implementation is deferred. Currently provides the data model
    and a registry for discovered templates.
    """

    def __init__(self):
        self._templates: dict[str, StrategyTemplate] = {}

    def register(self, template: StrategyTemplate) -> None:
        self._templates[template.name] = template

    def get(self, name: str) -> Optional[StrategyTemplate]:
        return self._templates.get(name)

    def all(self) -> list[StrategyTemplate]:
        return list(self._templates.values())

    def best(self, k: int = 1) -> list[StrategyTemplate]:
        """Return top-k templates by score."""
        sorted_t = sorted(
            self._templates.values(), key=lambda t: t.score, reverse=True
        )
        return sorted_t[:k]
