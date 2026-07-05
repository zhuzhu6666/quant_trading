"""
Risk concentration monitoring — real-time factor exposure checks.

Provides FactorExposureMonitor for detecting concentration risk, consensus
risk, and single-factor overweight before opening a trade each bar.
"""

from dataclasses import dataclass, field
from typing import Any

from loguru import logger

from alpha.portfolio_compositor import resolve_factor_role


@dataclass
class ExposureReport:
    """Result of a concentration check, consumed by Summarizer / logging."""

    type_exposures: dict  # {type_name: total_weight}
    max_type_exposure: float
    violation_types: list[str]
    consensus_risk: bool
    consensus_all_long: bool
    consensus_all_short: bool
    total_factors: int
    healthy: bool


# ---------------------------------------------------------------------------
# Tag → canonical type mapping (first-match wins)
# ---------------------------------------------------------------------------
_TAG_TO_TYPE: dict[str, str] = {
    "量价": "量价",
    "动量": "动量",
    "均值回归": "均值回归",
    "波动率": "波动率",
    "趋势": "趋势",
    "宏观": "宏观",
    "利率": "宏观",
    "美元": "宏观",
    "金银比": "宏观",
    "持仓": "持仓",
    "黄金": "持仓",
    "白银": "持仓",
}


class FactorExposureMonitor:
    """
    Real-time monitoring of factor exposure concentration.

    Rules
    -----
    1. Single type total weight <= max_type_pct (default 40%)
    2. Single factor weight <= max_single_weight (default 3.0)
    3. If any type exposure > alert_type_pct (default 50%) -> ALERT
    4. Consensus risk: if >80% of factors point same direction -> ALERT

    Difference from AWE._enforce_diversity
    --------------------------------------
    AWE constrains weights during adaptation.
    This monitor checks BEFORE opening a trade each bar.
    """

    def __init__(
        self,
        max_type_pct: float = 0.40,
        alert_type_pct: float = 0.50,
        max_single_weight: float = 3.0,
    ):
        self.max_type_pct = max_type_pct
        self.alert_type_pct = alert_type_pct
        self.max_single_weight = max_single_weight

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check(
        self,
        factor_signals: dict[str, float],
        factor_tags: dict[str, list[str]],
        factor_weights: dict[str, float],
        factor_roles: dict[str, str] | None = None,
    ) -> ExposureReport:
        """
        Evaluate concentration risk for the current bar.

        Parameters
        ----------
        factor_signals : {name: signal_value}
            Normalised signal in [-1, 1].
        factor_tags : {name: [tag1, tag2, …]}
            Arbitrary-length list of type tags per factor.
        factor_weights : {name: weight}
            Current portfolio-compositor weights.

        Returns
        -------
        ExposureReport
        """
        # --- early exit for empty inputs ----------------------------------
        if not factor_signals or not factor_weights:
            logger.debug("No factors to check — returning healthy report")
            return ExposureReport(
                type_exposures={},
                max_type_exposure=0.0,
                violation_types=[],
                consensus_risk=False,
                consensus_all_long=False,
                consensus_all_short=False,
                total_factors=0,
                healthy=True,
            )

        roles = factor_roles or {}
        alpha_signals = {
            name: signal for name, signal in factor_signals.items()
            if resolve_factor_role(name, {"role": roles.get(name)}) == "alpha"
        }
        alpha_weights = {
            name: weight for name, weight in factor_weights.items()
            if resolve_factor_role(name, {"role": roles.get(name)}) == "alpha"
        }

        # --- 1. aggregate weights by type ---------------------------------
        type_exposures: dict[str, float] = {}
        for name, weight in alpha_weights.items():
            if weight == 0.0:
                continue  # exclude zero-weight factors

            tags = factor_tags.get(name, [])
            ftype = self._derive_type(tags)
            type_exposures[ftype] = type_exposures.get(ftype, 0.0) + weight

        max_type_exposure = max(type_exposures.values()) if type_exposures else 0.0

        # --- 2. check type-level limits -----------------------------------
        violation_types: list[str] = []
        for ftype, total in type_exposures.items():
            if total > self.max_type_pct:
                violation_types.append(ftype)
                logger.warning(
                    "Type exposure {!r} {:.2%} exceeds max {:.0%}",
                    ftype,
                    total,
                    self.max_type_pct,
                )
            if total > self.alert_type_pct:
                logger.warning(
                    "⚠  ALERT — type exposure {!r} {:.2%} exceeds alert threshold {:.0%}",
                    ftype,
                    total,
                    self.alert_type_pct,
                )

        # --- 3. check single-factor limits --------------------------------
        for name, weight in alpha_weights.items():
            if weight > self.max_single_weight:
                logger.warning(
                    "Single factor {!r} weight {:.2f} exceeds max {:.2f}",
                    name,
                    weight,
                    self.max_single_weight,
                )

        # --- 4. consensus risk --------------------------------------------
        positives = sum(
            1 for s in alpha_signals.values() if s > 0
        )
        negatives = sum(
            1 for s in alpha_signals.values() if s < 0
        )
        total_nonzero = positives + negatives

        consensus_risk: bool = False
        consensus_all_long: bool = False
        consensus_all_short: bool = False

        if total_nonzero > 0:
            ratio_pos = positives / total_nonzero
            ratio_neg = negatives / total_nonzero

            if ratio_pos > 0.80:
                consensus_risk = True
                consensus_all_long = True
                logger.warning(
                    "⚠  Consensus risk — {:.0%} of factors are long",
                    ratio_pos,
                )
            elif ratio_neg > 0.80:
                consensus_risk = True
                consensus_all_short = True
                logger.warning(
                    "⚠  Consensus risk — {:.0%} of factors are short",
                    ratio_neg,
                )

        # --- assemble report ----------------------------------------------
        healthy = (
            len(violation_types) == 0
            and max_type_exposure <= self.max_type_pct
            and not consensus_risk
            and all(
                w <= self.max_single_weight
                for w in alpha_weights.values()
            )
        )

        return ExposureReport(
            type_exposures=type_exposures,
            max_type_exposure=max_type_exposure,
            violation_types=violation_types,
            consensus_risk=consensus_risk,
            consensus_all_long=consensus_all_long,
            consensus_all_short=consensus_all_short,
            total_factors=len(alpha_signals),
            healthy=healthy,
        )

    # ------------------------------------------------------------------
    # Reporting helpers
    # ------------------------------------------------------------------

    def summarize(self, report: ExposureReport) -> str:
        """Return a human-readable summary for logging / dashboard display."""
        lines = ["╔══════════════════════════════════════╗"]
        lines.append("║  Factor Exposure Concentration     ║")
        lines.append("╚══════════════════════════════════════╝")
        lines.append(f"  Total factors            : {report.total_factors}")
        lines.append(f"  Healthy                  : {report.healthy}")
        lines.append("")

        if report.type_exposures:
            lines.append("  Type exposures:")
            for ftype, total in sorted(
                report.type_exposures.items(), key=lambda x: x[1], reverse=True
            ):
                marker = " ⚠" if total > self.max_type_pct else ""
                lines.append(f"    {ftype:>12s}  {total:>7.2%}{marker}")
        else:
            lines.append("  Type exposures: (none)")

        lines.append("")
        lines.append(f"  Max type exposure        : {report.max_type_exposure:.2%}")
        lines.append(f"  Violation types          : {report.violation_types or '(none)'}")
        lines.append(f"  Consensus risk           : {report.consensus_risk}")
        if report.consensus_risk:
            lines.append(
                f"    Direction              : {'ALL LONG' if report.consensus_all_long else 'ALL SHORT'}"
            )
        lines.append(
            f"  Healthy                  : {report.healthy}"
        )

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _derive_type(tags: list[str]) -> str:
        """
        Map a list of tags to a single canonical type.

        First-match wins: the mapping table is iterated in insertion order
        (Python 3.7+), so earlier entries take priority.

        Parameters
        ----------
        tags : list[str]
            Tags attached to a factor (e.g. from RuntimeConfig).

        Returns
        -------
        str
            Canonical type name, or ``'其他'`` if no tag matches.
        """
        for tag in tags:
            canonical = _TAG_TO_TYPE.get(tag)
            if canonical is not None:
                return canonical
        return "其他"
