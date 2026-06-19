"""Risk parameter tuning — CLI + importable service.

Two-mode form (per spec §1.3):
- `python scripts/tune_risk_params.py [args]`  → CLI behavior
- `from scripts.tune_risk_params import run_tuning`  → service call
"""
import argparse
import json
import sys
import time
from itertools import product
from pathlib import Path
from typing import Any, Callable, Optional

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.core.paths import CHARTS_DIR  # noqa: E402


def _run_single_paper_pass(
    risk_pct: float,
    cb_pct: float,
    n_bars: int,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run one short paper pass with the given risk and CB params.

    Returns dict with at least: pnl, trades, sharpe, dd, risk_pct, cb_pct.
    """
    cb = progress_cb or (lambda *_: None)
    # Lazy import to keep module load cheap
    from execution.paper_engine import PaperEngine
    from data.store import DataStore

    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars("XAUUSD+", "M15")
    if n_bars and len(df) > n_bars:
        df = df.tail(n_bars)

    engine = PaperEngine(
        initial_balance=500.0,
        risk_per_trade_pct=risk_pct / 100.0,
        max_daily_loss_pct=cb_pct / 100.0,
    )
    stats = engine.run_bars(df, progress_cb=cb)
    return {
        "risk_pct": risk_pct,
        "cb_pct": cb_pct,
        "pnl": stats.get("net_pnl", 0.0),
        "trades": stats.get("total_trades", 0),
        "sharpe": stats.get("sharpe", 0.0),
        "dd": stats.get("max_drawdown_pct", 0.0),
    }


def run_tuning(
    risk_pct_grid: list[float] | None = None,
    cb_pct_grid: list[float] | None = None,
    n_bars: int = 5000,
    top_k: int = 5,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run a tuning sweep. Service entry point.

    Returns dict: {best: {risk_pct, cb_pct, pnl, ...}, all_results: [...], report_path}
    """
    if risk_pct_grid is None:
        risk_pct_grid = [0.5, 1.0, 1.5, 2.0]
    if cb_pct_grid is None:
        cb_pct_grid = [5, 10, 15, 20]
    cb = progress_cb or (lambda *_: None)

    n_combos = len(risk_pct_grid) * len(cb_pct_grid)
    cb("loading", 5, f"sweeping {n_combos} combos (risk={risk_pct_grid} × cb={cb_pct_grid})")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CHARTS_DIR / "tune_report.txt"

    results: list[dict] = []
    for i, (risk, cbp) in enumerate(product(risk_pct_grid, cb_pct_grid), start=1):
        cb("running", 5 + 80 * i / n_combos, f"combo {i}/{n_combos}: risk={risk} cb={cbp}")
        try:
            r = _run_single_paper_pass(risk, cbp, n_bars, progress_cb=cb)
            results.append(r)
        except Exception as e:
            cb("running", 5 + 80 * i / n_combos, f"combo {i}/{n_combos} failed: {e}")
            results.append({"risk_pct": risk, "cb_pct": cbp, "error": str(e)})

    # Rank by pnl (desc); tiebreak by sharpe
    valid = [r for r in results if "error" not in r]
    valid.sort(key=lambda r: (r.get("pnl", 0.0), r.get("sharpe", 0.0)), reverse=True)
    best = valid[0] if valid else None
    top = valid[:top_k]

    cb("writing", 92, f"writing report to {report_path}")
    lines = [
        "# Tune Report",
        f"# risk_pct_grid: {risk_pct_grid}",
        f"# cb_pct_grid: {cb_pct_grid}",
        f"# n_bars: {n_bars}",
        "",
        f"BEST: risk_pct={best['risk_pct']} cb_pct={best['cb_pct']} pnl={best['pnl']} sharpe={best['sharpe']} dd={best['dd']} trades={best['trades']}" if best else "NO VALID RESULTS",
        "",
        "TOP-{}:".format(top_k),
    ]
    for i, r in enumerate(top, 1):
        lines.append(f"  {i}. risk_pct={r['risk_pct']} cb_pct={r['cb_pct']} pnl={r['pnl']} sharpe={r['sharpe']} dd={r['dd']} trades={r['trades']}")
    report_path.write_text("\n".join(lines), encoding="utf-8")

    cb("done", 100, f"report: {report_path}")
    return {"best": best, "top": top, "all_results": results, "report_path": str(report_path)}


def main() -> int:
    """CLI entry — preserve original argument names."""
    parser = argparse.ArgumentParser(description="Risk parameter tuning")
    parser.add_argument("--risk-grid", type=str, default="0.5,1.0,1.5,2.0", help="CSV of risk_per_trade_pct values")
    parser.add_argument("--cb-grid", type=str, default="5,10,15,20", help="CSV of max_daily_loss_pct values")
    parser.add_argument("--n-bars", type=int, default=5000)
    parser.add_argument("--top-k", type=int, default=5)
    args = parser.parse_args()

    risk_grid = [float(x.strip()) for x in args.risk_grid.split(",") if x.strip()]
    cb_grid = [float(x.strip()) for x in args.cb_grid.split(",") if x.strip()]

    def _print_progress(step: str, pct: float, msg: str) -> None:
        print(f"[{pct:5.1f}%] {step}: {msg}", flush=True)

    result = run_tuning(
        risk_pct_grid=risk_grid,
        cb_pct_grid=cb_grid,
        n_bars=args.n_bars,
        top_k=args.top_k,
        progress_cb=_print_progress,
    )
    print(json.dumps({"best": result["best"], "top": result["top"]}, indent=2, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
