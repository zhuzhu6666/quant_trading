"""L2 factor discovery — CLI + importable service.

Two-mode form (per spec §1.3):
- `python scripts/discover_factors.py [args]`  → CLI behavior
- `from scripts.discover_factors import run_discovery`  → service call

The service function is the source of truth; the CLI is a thin wrapper.
"""
import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable, Optional

# Allow `from scripts.discover_factors import run_discovery` from project root
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.core.paths import CHARTS_DIR  # noqa: E402
from backend.jobs.progress import ProgressCB  # noqa: E402


def _candidate_expression(candidate: Any) -> str:
    return str(
        getattr(candidate, "expression", "")
        or getattr(candidate, "expr", "")
        or getattr(candidate, "dsl", "")
        or ""
    )


def _candidate_name(candidate: Any, index: int) -> str:
    explicit = str(getattr(candidate, "name", "") or "").strip()
    if explicit:
        return explicit
    expr = _candidate_expression(candidate)
    digest = hashlib.sha1(expr.encode("utf-8")).hexdigest()[:10] if expr else f"{index:04d}"
    return f"dsl_shadow_{digest}"


def _candidate_ic(candidate: Any) -> float:
    for attr in ("ic", "abs_ic_mean", "score"):
        value = getattr(candidate, attr, None)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _risk_verdict_dict(action: str, context: dict[str, Any]) -> dict[str, Any]:
    from risk.policy_service import RiskPolicyService

    return RiskPolicyService.shared().evaluate(action, context).to_dict()


def _register_top_factors(top: list[Any], *, engine: str, auto_register: bool) -> tuple[list[str], dict[str, Any]]:
    if not auto_register:
        return [], {
            "allowed": True,
            "reason": "auto_register_disabled",
            "required_mode": "shadow",
            "audit_payload": {
                "action": "register_factor",
                "source": "discover_factors",
                "candidate_count": len(top),
                "engine": engine,
            },
        }

    verdict = _risk_verdict_dict(
        "register_factor",
        {
            "required_mode": "shadow",
            "candidate_count": len(top),
            "engine": engine,
        },
    )
    if not verdict.get("allowed", False):
        return [], verdict

    from alpha.factor_dsl import evaluate_dsl
    from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW

    adapter = RegistryAdapter.shared()
    registered: list[str] = []
    for i, candidate in enumerate(top):
        expression = _candidate_expression(candidate)
        if not expression:
            continue
        name = _candidate_name(candidate, i)
        func = lambda df, _expr=expression: evaluate_dsl(_expr, df)
        ok = adapter.register_runtime(
            name=name,
            func=func,
            source=SOURCE_SHADOW,
            description=expression,
        )
        if ok:
            registered.append(name)
    return registered, verdict


def _filter_multi_forward(
    top: list[Any],
    df,
    forward_periods: list[int],
    *,
    min_score: float = 50.0,
) -> tuple[list[Any], dict[str, Any]]:
    """Re-score registration candidates across all forward periods."""
    if not top:
        return [], {"min_score": min_score, "items": []}

    from alpha.factor_score_evaluator import FactorScoreEvaluator

    evaluators = {
        int(fp): FactorScoreEvaluator(df, forward_period=int(fp))
        for fp in forward_periods
        if int(fp) > 0
    }
    kept: list[Any] = []
    items: list[dict[str, Any]] = []
    for i, candidate in enumerate(top):
        expression = _candidate_expression(candidate)
        name = _candidate_name(candidate, i)
        scores: list[dict[str, Any]] = []
        passed = bool(expression and evaluators)
        for fp, evaluator in evaluators.items():
            score = evaluator.score_expression(expression)
            ok = not score.error and score.score >= min_score
            passed = passed and ok
            scores.append(
                {
                    "forward_period": fp,
                    "score": round(float(score.score), 4),
                    "abs_ic_mean": round(float(score.abs_ic_mean), 6),
                    "n_obs": int(score.n_obs),
                    "status": score.status,
                    "error": score.error,
                    "passed": bool(ok),
                }
            )
        items.append({"name": name, "expr": expression, "passed": passed, "scores": scores})
        if passed:
            kept.append(candidate)
    return kept, {"min_score": min_score, "items": items}


def run_discovery(
    n_candidates: int = 1000,
    top_k: int = 50,
    forward_periods: list[int] | None = None,
    auto_register: bool = False,
    engine: str = "gp",
    gp_pop: int = 100,
    gp_gen: int = 20,
    n_bars: int = 50000,
    progress_cb: Optional[Callable[[str, float, str], None]] = None,
) -> dict:
    """Run L2 factor discovery. Service entry point. Progress via progress_cb.

    Returns dict: {engine, candidates_total, found, top_factors, report_path}
    """
    cb = progress_cb or (lambda *_: None)
    if forward_periods is None:
        forward_periods = [1, 5, 20]

    cb("loading", 5, f"engine={engine} n={n_candidates} top_k={top_k}")
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)
    report_path = CHARTS_DIR / "discover_report.json"

    cb("loading", 15, "loading bars from db")
    from data.duckdb_store import DuckDBDataStore as DataStore
    store = DataStore("data/ctrader_data.duckdb")
    df = store.load_bars("XAUUSD+", "M15")
    if len(df) > n_bars:
        df = df.tail(n_bars)
    cb("loaded", 25, f"loaded {len(df)} bars")

    if engine == "gp":
        cb("running", 30, f"GP search pop={gp_pop} gen={gp_gen}")
        from alpha.factor_search_gp import run_gp_search  # type: ignore
        candidates = run_gp_search(df, pop=gp_pop, gen=gp_gen, progress_cb=cb)
    else:
        cb("running", 30, f"random search n={n_candidates}")
        from alpha.factor_search import run_random_search  # type: ignore
        candidates = run_random_search(df, n=n_candidates, progress_cb=cb)

    cb("evaluating", 70, f"evaluating {len(candidates) if isinstance(candidates, list) else '?'} candidates")
    top = candidates[:top_k] if isinstance(candidates, list) else []
    governed_top, governance = _filter_multi_forward(top, df, forward_periods, min_score=50.0)
    registered_shadow, risk_verdict = _register_top_factors(governed_top, engine=engine, auto_register=auto_register)

    cb("writing", 95, f"writing report to {report_path}")
    governance_by_expr = {
        str(item.get("expr") or ""): item
        for item in governance.get("items", [])
        if isinstance(item, dict)
    }
    top_factors = [
        {
            "name": _candidate_name(c, i),
            "expr": _candidate_expression(c),
            "ic": _candidate_ic(c),
            "governance_passed": bool(governance_by_expr.get(_candidate_expression(c), {}).get("passed", False)),
            "multi_forward_scores": governance_by_expr.get(_candidate_expression(c), {}).get("scores", []),
        }
        for i, c in enumerate(top)
    ]
    report_path.write_text(
        json.dumps(
            {
                "engine": engine,
                "n_candidates": n_candidates,
                "top_k": top_k,
                "found": len(candidates) if isinstance(candidates, list) else 0,
                "top": top_factors,
                "auto_register": auto_register,
                "registered_shadow": registered_shadow,
                "risk_verdict": risk_verdict,
                "governance": governance,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    cb("done", 100, f"done. {len(top)} top factors written, {len(registered_shadow)} registered")

    return {
        "engine": engine,
        "candidates_total": n_candidates,
        "found": len(candidates) if isinstance(candidates, list) else 0,
        "top_factors": top_factors,
        "registered_shadow": registered_shadow,
        "risk_verdict": risk_verdict,
        "governance": governance,
        "report_path": str(report_path),
    }


def main() -> int:
    """CLI entry — preserve original argument names for backward compat."""
    parser = argparse.ArgumentParser(description="L2 factor discovery")
    parser.add_argument("--n-candidates", type=int, default=1000, help="Number of candidates to generate (random search)")
    parser.add_argument("--top-k", type=int, default=50, help="Number of top factors to keep")
    parser.add_argument("--forward-periods", type=str, default="1,5,20", help="CSV of forward periods")
    parser.add_argument("--auto-register", action="store_true", default=False, help="Auto-register top factors as shadow")
    parser.add_argument("--no-auto-register", dest="auto_register", action="store_false")
    parser.add_argument("--engine", type=str, default="gp", choices=["gp", "random"], help="Search engine")
    parser.add_argument("--gp-pop", type=int, default=100, help="GP population size")
    parser.add_argument("--gp-gen", type=int, default=20, help="GP generations")
    args = parser.parse_args()

    forward_periods = [int(x.strip()) for x in args.forward_periods.split(",") if x.strip().isdigit()]

    def _print_progress(step: str, pct: float, msg: str) -> None:
        print(f"[{pct:5.1f}%] {step}: {msg}", flush=True)

    result = run_discovery(
        n_candidates=args.n_candidates,
        top_k=args.top_k,
        forward_periods=forward_periods,
        auto_register=args.auto_register,
        engine=args.engine,
        gp_pop=args.gp_pop,
        gp_gen=args.gp_gen,
        progress_cb=_print_progress,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
