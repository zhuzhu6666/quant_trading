"""A/B test of strategy paths — CLI + importable service.

Two-mode form (per spec §1.3):
- `python scripts/p1_e_ab_test.py [args]`  → CLI behavior
- `from scripts.p1_e_ab_test import run_ab`  → service call
"""
import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.core.paths import CHARTS_DIR  # noqa: E402


PATH_REGISTRY = {
    "baseline": "A: original factor weights (multi_factor_m15 default)",
    "reverse":  "B: negated factor weights (anti-signal)",
    "uniform":  "C: equal weights across all factors",
}


def _run_path(
    path: str,
    n_bars: int,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run a single path (baseline / reverse / uniform) and return its PnL summary."""
    cb = progress_cb or (lambda *_: None)
    cb("loading", 5, f"running path={path} n_bars={n_bars}")

    from data.store import DataStore
    from execution.paper_trader import PaperTrader

    store = DataStore("data/market_data.db")
    df = store.load_bars("XAUUSD+", "M15")
    if n_bars and len(df) > n_bars:
        df = df.tail(n_bars)

    # Map path → PaperTrader config
    weight_overrides: dict[str, float] = {}
    if path == "reverse":
        weight_overrides = {"__negate__": True}  # marker
    elif path == "uniform":
        weight_overrides = {"__uniform__": True}

    trader = PaperTrader(
        symbol="XAUUSD+",
        timeframe="M15",
        factor_overrides=weight_overrides,
    )
    cb("running", 30, f"path={path} started")
    stats = trader.run_bars(df, progress_cb=cb)
    return {
        "path": path,
        "pnl": stats.get("net_pnl", 0.0),
        "trades": stats.get("total_trades", 0),
        "sharpe": stats.get("sharpe", 0.0),
        "dd": stats.get("max_drawdown_pct", 0.0),
    }


def run_ab(
    path_a: str = "baseline",
    path_b: str = "reverse",
    n_bars: int = 5000,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run an A/B test between two paths. Service entry point.

    Returns dict: {result_a, result_b, delta_pnl, delta_sharpe, report_path}
    """
    cb = progress_cb or (lambda *_: None)
    cb("loading", 5, f"A/B test: {path_a} vs {path_b} ({n_bars} bars)")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CHARTS_DIR / "p1_e_ab_report.txt"

    cb("running", 20, f"running path A: {path_a}")
    result_a = _run_path(path_a, n_bars, progress_cb=cb)
    cb("running", 55, f"running path B: {path_b}")
    result_b = _run_path(path_b, n_bars, progress_cb=cb)

    delta_pnl = result_b["pnl"] - result_a["pnl"]
    delta_sharpe = result_b["sharpe"] - result_a["sharpe"]

    cb("writing", 92, f"writing report to {report_path}")
    lines = [
        "# A/B Test Report",
        f"# path_a: {path_a} ({PATH_REGISTRY.get(path_a, '?')})",
        f"# path_b: {path_b} ({PATH_REGISTRY.get(path_b, '?')})",
        f"# n_bars: {n_bars}",
        "",
        f"PATH A ({path_a}): pnl={result_a['pnl']} trades={result_a['trades']} sharpe={result_a['sharpe']} dd={result_a['dd']}",
        f"PATH B ({path_b}): pnl={result_b['pnl']} trades={result_b['trades']} sharpe={result_b['sharpe']} dd={result_b['dd']}",
        "",
        f"DELTA (B - A): pnl={delta_pnl} sharpe={delta_sharpe}",
    ]
    report_path.write_text("\n".join(lines), encoding="utf-8")

    cb("done", 100, f"report: {report_path}")
    return {
        "result_a": result_a,
        "result_b": result_b,
        "delta_pnl": delta_pnl,
        "delta_sharpe": delta_sharpe,
        "report_path": str(report_path),
    }


def main() -> int:
    """CLI entry — preserve original flags."""
    parser = argparse.ArgumentParser(description="A/B test of strategy paths")
    parser.add_argument("--path-a", type=str, default="baseline", choices=list(PATH_REGISTRY.keys()))
    parser.add_argument("--path-b", type=str, default="reverse", choices=list(PATH_REGISTRY.keys()))
    parser.add_argument("--n-bars", type=int, default=5000)
    args = parser.parse_args()

    def _print_progress(step: str, pct: float, msg: str) -> None:
        print(f"[{pct:5.1f}%] {step}: {msg}", flush=True)

    result = run_ab(
        path_a=args.path_a,
        path_b=args.path_b,
        n_bars=args.n_bars,
        progress_cb=_print_progress,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
