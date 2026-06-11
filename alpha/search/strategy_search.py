"""alpha/search/strategy_search.py — GP domain for strategy template search (Task 2.1.4).

Genome:
    StrategyTemplate encodes how a set of factor signals are aggregated into trading
    decisions. GP evolves the aggregation method, threshold, sizing rule, and regime
    filter to maximize fitness (rank IC / Sharpe).
"""

from __future__ import annotations

import logging
import random
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import numpy as np

logger = logging.getLogger(__name__)


@dataclass
class StrategyTemplate:
    """A strategy template genome.

    Attributes:
        name: Unique name for this template.
        signal_aggregation: "vote", "weighted_vote", "max", "mean".
        threshold: Minimum signal threshold (0.0-1.0).
        sizing: "fixed", "kelly", "risk_pct".
        regime_filter: "all", "trending", "ranging", "volatile".
        score: Evaluation fitness (rank IC).
        generation: GP generation that produced it.
    """
    name: str
    signal_aggregation: str = "vote"
    threshold: float = 0.5
    sizing: str = "fixed"
    regime_filter: str = "all"
    score: float = 0.0
    generation: int = 0

    def clone(self) -> "StrategyTemplate":
        return StrategyTemplate(
            name=self.name, signal_aggregation=self.signal_aggregation,
            threshold=self.threshold, sizing=self.sizing,
            regime_filter=self.regime_filter, score=self.score,
            generation=self.generation,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name, "signal_aggregation": self.signal_aggregation,
            "threshold": self.threshold, "sizing": self.sizing,
            "regime_filter": self.regime_filter, "score": self.score,
            "generation": self.generation,
        }


# ── Constants ─────────────────────────────────────────────────────────

SIGNAL_AGGREGATIONS = ["vote", "weighted_vote", "max", "mean"]
SIZING_METHODS = ["fixed", "kelly", "risk_pct"]
REGIME_FILTERS = ["all", "trending", "ranging", "volatile"]

_ID_COUNTER: int = 0


def _next_id() -> str:
    global _ID_COUNTER
    _ID_COUNTER += 1
    return f"template_{_ID_COUNTER:06d}"


# ── Fitness evaluation ────────────────────────────────────────────────


def _rank_ic(signals: np.ndarray, forward_returns: np.ndarray) -> float:
    """Spearman rank IC between signal and forward returns."""
    mask = np.isfinite(signals) & np.isfinite(forward_returns)
    valid_n = mask.sum()
    if valid_n < 30:
        return 0.0
    s = signals[mask]
    fr = forward_returns[mask]
    s_rank = np.argsort(np.argsort(s)).astype(float)
    fr_rank = np.argsort(np.argsort(fr)).astype(float)
    s_rank -= s_rank.mean()
    fr_rank -= fr_rank.mean()
    denom = np.sqrt((s_rank**2).sum() * (fr_rank**2).sum())
    if denom < 1e-10:
        return 0.0
    return float((s_rank * fr_rank).sum() / denom)


def _aggregate_signals(
    factor_values: np.ndarray, method: str, regime_filter: str,
    regime_labels: np.ndarray | None = None,
) -> np.ndarray:
    """Aggregate (T, n_factors) into (T,) composite signal."""
    T, n = factor_values.shape
    if n == 0:
        return np.zeros(T)
    safe = np.nan_to_num(factor_values, nan=0.0)
    if method == "vote":
        composite = np.sign(safe).mean(axis=1)
    elif method == "weighted_vote":
        weights = np.abs(safe).mean(axis=0)
        w_sum = weights.sum()
        weights = np.ones(n) / n if w_sum < 1e-10 else weights / w_sum
        composite = (np.sign(safe) * weights[None, :]).sum(axis=1)
    elif method == "max":
        composite = safe.max(axis=1)
    else:  # "mean"
        composite = safe.mean(axis=1)

    if regime_filter != "all" and regime_labels is not None:
        rmap = {"trending": 0, "ranging": 1, "volatile": 2}
        allowed = rmap.get(regime_filter)
        if allowed is not None:
            composite = composite * (regime_labels == allowed).astype(float)
    return composite


def _apply_threshold(signals: np.ndarray, threshold: float) -> np.ndarray:
    out = signals.copy()
    out[np.abs(out) < threshold] = 0.0
    return out


def _evaluate_strategy_fitness(
    template: StrategyTemplate, factor_values: np.ndarray,
    forward_returns: np.ndarray, regime_labels: np.ndarray | None = None,
) -> float:
    """Compute rank IC fitness for one strategy template."""
    composite = _aggregate_signals(
        factor_values, template.signal_aggregation, template.regime_filter, regime_labels
    )
    thresholded = _apply_threshold(composite, template.threshold)
    return _rank_ic(thresholded, forward_returns)


# ── GP operators ──────────────────────────────────────────────────────


def _random_template() -> StrategyTemplate:
    return StrategyTemplate(
        name=_next_id(),
        signal_aggregation=random.choice(SIGNAL_AGGREGATIONS),
        threshold=round(random.uniform(0.0, 0.5), 4),
        sizing=random.choice(SIZING_METHODS),
        regime_filter=random.choice(REGIME_FILTERS),
    )


def _make_initial_population(pop_size: int) -> list[StrategyTemplate]:
    pop: list[StrategyTemplate] = []
    for agg in SIGNAL_AGGREGATIONS:
        for sz in SIZING_METHODS:
            for reg in REGIME_FILTERS:
                pop.append(StrategyTemplate(
                    name=_next_id(), signal_aggregation=agg,
                    threshold=round(random.uniform(0.0, 0.5), 4),
                    sizing=sz, regime_filter=reg,
                ))
    random.shuffle(pop)
    while len(pop) < pop_size:
        pop.append(_random_template())
    return pop[:pop_size]


def _tournament_select(population: list[StrategyTemplate], k: int = 3) -> StrategyTemplate:
    idxs = random.sample(range(len(population)), min(k, len(population)))
    return population[max(idxs, key=lambda i: population[i].score)].clone()


def _crossover(p1: StrategyTemplate, p2: StrategyTemplate) -> tuple[StrategyTemplate, StrategyTemplate]:
    fields1 = [p1.signal_aggregation, p1.threshold, p1.sizing, p1.regime_filter]
    fields2 = [p2.signal_aggregation, p2.threshold, p2.sizing, p2.regime_filter]
    split = random.randint(0, 3)
    for i in range(split):
        fields1[i], fields2[i] = fields2[i], fields1[i]
    gen = max(p1.generation, p2.generation) + 1
    return (
        StrategyTemplate(name=_next_id(), signal_aggregation=str(fields1[0]),
                         threshold=float(fields1[1]), sizing=str(fields1[2]),
                         regime_filter=str(fields1[3]), generation=gen),
        StrategyTemplate(name=_next_id(), signal_aggregation=str(fields2[0]),
                         threshold=float(fields2[1]), sizing=str(fields2[2]),
                         regime_filter=str(fields2[3]), generation=gen),
    )


def _mutate(t: StrategyTemplate, mut_prob: float = 0.15) -> StrategyTemplate:
    child = t.clone()
    child.name = _next_id()
    child.generation = t.generation + 1
    if random.random() < mut_prob:
        child.signal_aggregation = random.choice(
            [a for a in SIGNAL_AGGREGATIONS if a != child.signal_aggregation] or SIGNAL_AGGREGATIONS
        )
    if random.random() < mut_prob:
        child.threshold = round(max(0.0, min(1.0, child.threshold + random.gauss(0, 0.1))), 4)
    if random.random() < mut_prob:
        child.sizing = random.choice(
            [s for s in SIZING_METHODS if s != child.sizing] or SIZING_METHODS
        )
    if random.random() < mut_prob:
        child.regime_filter = random.choice(
            [r for r in REGIME_FILTERS if r != child.regime_filter] or REGIME_FILTERS
        )
    return child


def _step(population: list[StrategyTemplate], elite_frac: float = 0.10,
          tournament_k: int = 3, mut_prob: float = 0.15) -> list[StrategyTemplate]:
    n = len(population)
    n_elite = max(1, int(n * elite_frac))
    sorted_pop = sorted(population, key=lambda t: t.score, reverse=True)
    new_pop = [e.clone() for e in sorted_pop[:n_elite]]
    while len(new_pop) < n:
        p1 = _tournament_select(population, k=tournament_k)
        p2 = _tournament_select(population, k=tournament_k)
        c1, c2 = _crossover(p1, p2)
        new_pop.append(_mutate(c1, mut_prob))
        if len(new_pop) < n:
            new_pop.append(_mutate(c2, mut_prob))
    return new_pop[:n]


# ── Result ────────────────────────────────────────────────────────────


@dataclass
class StrategySearchResult:
    best: list[StrategyTemplate] = field(default_factory=list)
    all_top_per_gen: list[StrategyTemplate] = field(default_factory=list)
    n_generations: int = 0
    pop_size: int = 0
    total_evaluated: int = 0
    elapsed_sec: float = 0.0
    best_score_history: list[float] = field(default_factory=list)
    avg_score_history: list[float] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_generations": self.n_generations,
            "pop_size": self.pop_size,
            "total_evaluated": self.total_evaluated,
            "elapsed_sec": round(self.elapsed_sec, 2),
            "best_score_history": [round(x, 4) for x in self.best_score_history],
            "avg_score_history": [round(x, 4) for x in self.avg_score_history],
            "best": [t.to_dict() for t in self.best],
        }


# ── StrategySearch (main class) ───────────────────────────────────────


class StrategySearch:
    """GP domain for strategy template search."""

    def __init__(self):
        self._templates: dict[str, StrategyTemplate] = {}
        self._source_run_id: str | None = None

    def register(self, template: StrategyTemplate) -> None:
        self._templates[template.name] = template

    def get(self, name: str) -> Optional[StrategyTemplate]:
        return self._templates.get(name)

    def all(self) -> list[StrategyTemplate]:
        return list(self._templates.values())

    def best(self, k: int = 1) -> list[StrategyTemplate]:
        return sorted(self._templates.values(), key=lambda t: t.score, reverse=True)[:k]

    def run(
        self,
        factor_values: np.ndarray,
        forward_returns: np.ndarray,
        regime_labels: np.ndarray | None = None,
        pop_size: int = 50,
        n_generations: int = 20,
        elite_frac: float = 0.10,
        tournament_k: int = 3,
        mut_prob: float = 0.15,
        top_k: int = 10,
        max_runtime_sec: float = 600.0,
        seed: int = 42,
        verbose: bool = True,
        run_id: str | None = None,
        progress_cb: Callable[[str, float, str], None] | None = None,
    ) -> StrategySearchResult:
        """Run GP search for optimal strategy templates."""
        random.seed(seed)
        np.random.seed(seed)
        self._source_run_id = run_id
        cb = progress_cb or (lambda *_: None)
        t0 = _time.time()

        cb("init", 5, f"creating initial population of {pop_size}")
        population = _make_initial_population(pop_size)

        cb("evaluate", 10, f"evaluating {len(population)} individuals")
        for t in population:
            t.score = _evaluate_strategy_fitness(t, factor_values, forward_returns, regime_labels)

        best_history = [max(t.score for t in population)]
        valid_scores = [t.score for t in population if t.score > -1e-8]
        avg_history = [float(np.mean(valid_scores)) if valid_scores else 0.0]
        all_top = [sorted(population, key=lambda x: x.score, reverse=True)[0]]

        if verbose:
            logger.info("[StratGP] gen=0  best=%.4f  avg=%.4f  n_valid=%d/%d",
                        best_history[0], avg_history[0], len(valid_scores), len(population))

        gen = 0
        for gen in range(1, n_generations + 1):
            if _time.time() - t0 > max_runtime_sec:
                logger.warning("[StratGP] max_runtime_sec=%.1fs reached at gen %d", max_runtime_sec, gen)
                break
            population = _step(population, elite_frac, tournament_k, mut_prob)
            for t in population:
                t.score = _evaluate_strategy_fitness(t, factor_values, forward_returns, regime_labels)
            best_score = max(t.score for t in population)
            best_history.append(best_score)
            valid_scores = [t.score for t in population if t.score > -1e-8]
            avg = float(np.mean(valid_scores)) if valid_scores else 0.0
            avg_history.append(avg)
            all_top.append(sorted(population, key=lambda x: x.score, reverse=True)[0])
            cb("evolve", 10 + 80 * gen / n_generations, f"gen {gen}/{n_generations}")

        cb("finalise", 92, "collecting top templates")
        unique: dict[str, StrategyTemplate] = {}
        for t in population:
            key = f"{t.signal_aggregation}_{t.threshold:.2f}_{t.sizing}_{t.regime_filter}"
            if key not in unique or t.score > unique[key].score:
                unique[key] = t
        all_sorted = sorted(unique.values(), key=lambda x: x.score, reverse=True)
        result = StrategySearchResult(
            best=all_sorted[:top_k], all_top_per_gen=all_top, n_generations=gen,
            pop_size=pop_size, total_evaluated=gen * pop_size,
            elapsed_sec=_time.time() - t0,
            best_score_history=best_history, avg_score_history=avg_history,
        )
        for t in result.best:
            if t.name not in self._templates:
                self._templates[t.name] = t
        cb("done", 100, f"completed in {result.elapsed_sec:.1f}s")
        return result
