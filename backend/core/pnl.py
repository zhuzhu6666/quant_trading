"""Single authority for cTrader realized net PnL.

``close_commission`` already contains the full round-trip commission
(0.2 per 0.01 lot, both legs).  The per-leg ``commission`` column is display
detail only; adding it would double count the same money.
"""

from __future__ import annotations


def net_pnl(gross: float, swap: float, close_commission: float) -> float:
    """Round-trip net PnL used by every accounting consumer."""

    return (
        float(gross or 0.0)
        + float(swap or 0.0)
        + float(close_commission or 0.0)
    )


def net_pnl_sql(*, prefix: str = "") -> str:
    """SQL expression matching :func:`net_pnl` over ``ctrader_deals``.

    ``prefix`` is the optional table alias (for example ``"d."``).
    """

    return (
        f"COALESCE({prefix}gross_profit, 0.0)"
        f" + COALESCE({prefix}swap, 0.0)"
        f" + COALESCE({prefix}close_commission, 0.0)"
    )
