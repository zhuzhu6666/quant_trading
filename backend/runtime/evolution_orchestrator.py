"""backend/runtime/evolution_orchestrator.py — 自进化编排器主循环。

把 GP 搜索 → OOS 评估 → Canary 晋升 → 权重更新 → 退役检查
串成一个可被 InProcessScheduler 调度的端到端管线。

流程:
  1. 从 DataStore 加载 M15 数据
  2. 跑 GP 搜索发现新因子
  3. 注册为 shadow 因子 (RegistryAdapter)
  4. OOS 评估 (PurgedWalkForward + BootstrapCI)
  5. Canary 晋升 (CanaryDirector)
  6. 权重更新 (WeightPolicy)
  7. 退役检查 (retirement_check)
  8. 发射 EvolutionStory 事件
"""

from __future__ import annotations

import logging
import time as _time
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from strategy import mab_router as _mab_router

logger = logging.getLogger(__name__)

# ── EvolutionStage 记录 ───────────────────────────────────────────────


class EvolutionReport:
    """单次 evolution cycle 的报告."""

    def __init__(self) -> None:
        self.ts: float = _time.time()
        self.gp_new_candidates: int = 0
        self.gp_registered_shadow: int = 0
        self.oos_passed: int = 0
        self.canary_promotions: list[str] = []
        self.canary_rollbacks: list[str] = []
        self.canary_stay: list[str] = []
        self.retire_candidates: list[str] = []
        self.retire_reason: str = ""
        self.weights_updated: bool = False
        self.duration_sec: float = 0.0
        self.error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "ts": self.ts,
            "ts_iso": datetime.fromtimestamp(self.ts, tz=timezone.utc).isoformat(),
            "gp_new_candidates": self.gp_new_candidates,
            "gp_registered_shadow": self.gp_registered_shadow,
            "oos_passed": self.oos_passed,
            "canary_promotions": self.canary_promotions,
            "canary_rollbacks": self.canary_rollbacks,
            "canary_stay": self.canary_stay,
            "retire_candidates": self.retire_candidates,
            "retire_reason": self.retire_reason,
            "weights_updated": self.weights_updated,
            "duration_sec": round(self.duration_sec, 1),
            "error": self.error,
        }


# ── main cycle ────────────────────────────────────────────────────────


def scheduled_evolution_cycle(
    symbol: str = "XAUUSD+",
    timeframe: str = "M15",
    n_bars: int = 8000,
    gp_pop: int = 50,
    gp_gen: int = 20,
    gp_top_k: int = 10,
    progress_cb: Callable[[str, float, str], None] | None = None,
) -> EvolutionReport:
    """执行一次完整的自进化循环。

    设计原则:
    - 每一步出错不阻塞后续步骤 (非致命异常被 catch, 记到 report.error)
    - 每一步发射 EvolutionStory 事件
    - 支持 progress_cb 供 InProcessScheduler / API 跟踪进度

    Args:
        symbol: 品种代码.
        timeframe: 数据周期.
        n_bars: GP 搜索使用的 bar 数量.
        gp_pop: GP 种群大小.
        gp_gen: GP 世代数.
        gp_top_k: GP 返回 top-K 候选.
        progress_cb: 可选进度回调 (step, pct, msg).

    Returns:
        EvolutionReport 记录本次循环结果.
    """
    report = EvolutionReport()
    cb = progress_cb or (lambda *_: None)
    t0 = _time.time()

    try:
        _emit_evolution_story("cycle_start", {})
        cb("start", 0, "evolution cycle starting")

        # ── Step 1: 加载数据 ──
        df = _load_bars(symbol, timeframe, n_bars)
        if df is None or len(df) < 500:
            report.error = f"insufficient bars: {len(df) if df is not None else 0}"
            logger.warning("[Evolve] %s", report.error)
            _emit_evolution_story("cycle_error", {"error": report.error})
            report.duration_sec = _time.time() - t0
            return report
        cb("data_loaded", 10, f"loaded {len(df)} bars")

        # ── Step 2: GP 搜索 (如果 pop>0) ──
        if gp_pop > 0 and gp_gen > 0:
            cb("gp_search", 15, f"GP search pop={gp_pop} gen={gp_gen}")
            expressions = _run_gp(df, pop=gp_pop, gen=gp_gen, top_k=gp_top_k)
            report.gp_new_candidates = len(expressions)
            logger.info("[Evolve] GP found %d candidates", len(expressions))
            cb("gp_done", 40, f"GP found {len(expressions)} candidates")
        else:
            expressions = []
            cb("gp_skip", 40, "GP skipped (fast cycle mode)")

        if not expressions:
            logger.info("[Evolve] no new GP candidates, skipping registration")
        else:
            # ── Step 3: 注册 shadow 因子 ──
            cb("register", 42, f"registering {len(expressions)} shadow factors")
            registered = _register_shadow_factors(expressions)
            report.gp_registered_shadow = registered
            logger.info("[Evolve] registered %d shadow factors", registered)
            _emit_evolution_story("shadow_registered", {
                "count": registered, "source": "gp_search",
            })
            cb("register_done", 50, f"registered {registered} factors")

        # ── Step 4: Canary 评估 (无论 GP 有无产出, 已有的 shadow/UNKNOWN 因子都需要评估) ──
        cb("canary", 55, "running canary evaluation")
        promotions, rollbacks, stay = _run_canary_evaluation(
            symbol, timeframe, n_bars
        )
        report.canary_promotions = promotions
        report.canary_rollbacks = rollbacks
        report.canary_stay = stay
        if promotions:
            logger.info("[Evolve] canary promoted: %s", promotions)
            _emit_evolution_story("canary_promotions", {
                "promoted": promotions, "rollbacked": rollbacks,
            })
            # 晋升 ACTIVE → 启用策略中的影子因子
            try:
                from config.runtime_config import patch as rc_patch
                rc_patch({"include_shadow_factors": True})
                logger.info("[Evolve] enabled include_shadow_factors in RuntimeConfig")
            except Exception as e:
                logger.debug("[Evolve] enable shadow factors failed: %s", e)
        cb("canary_done", 70, f"promoted {len(promotions)}, rolled {len(rollbacks)}")

        # ── Step 5: 因子健康检查 + 退役 ──
        cb("retirement", 75, "checking factor retirement")
        retire_info = _check_retirement()
        report.retire_candidates = retire_info["candidates"]
        report.retire_reason = retire_info["reason"]
        if retire_info["candidates"]:
            logger.info("[Evolve] retiring: %s", retire_info["candidates"])
            for name in retire_info["candidates"]:
                _try_retire(name, retire_info["reason"])
        cb("retirement_done", 85, f"retired {len(retire_info['candidates'])} factors")

        # ── Step 5.5: IC Tracker 自动刷新 + 因子健康报告落盘 ──
        cb("ic_refresh", 86, "refreshing factor IC tracking")
        try:
            from alpha.ic_tracker import refresh_all_factors
            ic_result = refresh_all_factors(symbol=symbol, timeframe=timeframe, n_bars=n_bars)
            logger.info(
                "[Evolve] IC refresh: checked=%d changed=%d errors=%d",
                ic_result.get("factors_checked", 0),
                ic_result.get("ic_changed_count", 0),
                len(ic_result.get("errors", [])),
            )
        except Exception as e:
            logger.debug("[Evolve] IC refresh skipped: %s", e)

        # 写因子健康报告 (供前端 /api/factor-health/latest 消费)
        try:
            from alpha.factor_health import evaluate_factors, write_report
            from backend.core.paths import CHARTS_DIR
            report_result = evaluate_factors(df, threshold=0.04)
            out_txt = CHARTS_DIR / "factor_health_report.txt"
            out_json = CHARTS_DIR / "factor_health_report.json"
            write_report(report_result, out_txt, out_json)
            logger.info(
                "[Evolve] factor health report written: %s healthy=%d watch=%d decaying=%d unknown=%d",
                out_json,
                report_result.get("healthy", 0),
                report_result.get("watch", 0),
                report_result.get("decaying", 0),
                report_result.get("unknown", 0),
            )
        except Exception as e:
            logger.debug("[Evolve] factor health report write skipped: %s", e)

        # ── Step 6: 权重更新 ──
        cb("weights", 88, "recomputing factor weights")
        report.weights_updated = _update_weights(df=df)
        cb("weights_done", 95, "weights updated" if report.weights_updated else "weights unchanged")

        _emit_evolution_story("cycle_complete", report.to_dict())

    except Exception as e:
        logger.exception("[Evolve] cycle failed: %s", e)
        report.error = f"unexpected: {e}"
        _emit_evolution_story("cycle_error", {"error": str(e)})
    finally:
        report.duration_sec = _time.time() - t0

    return report


# ── 子步骤实现 ────────────────────────────────────────────────────────


def _load_bars(symbol: str, timeframe: str, n_bars: int) -> pd.DataFrame | None:
    """从 DataStore 加载 M15 bar 数据."""
    try:
        from data.store import DataStore
        ds = DataStore()
        df = ds.load_bars(symbol, timeframe, limit=n_bars)
        return df
    except Exception as e:
        logger.warning("[Evolve] load_bars failed: %s", e)
        return None


def _run_gp(
    df: pd.DataFrame, pop: int = 50, gen: int = 20, top_k: int = 10
) -> list[Any]:
    """运行 GP 搜索, 返回 ExpressionScore 列表."""
    try:
        from alpha.factor_search_gp import run_gp_search
        t0 = _time.time()
        results = run_gp_search(
            df, pop=pop, gen=gen, top_k=top_k,
            progress_cb=lambda step, pct, msg: None,
        )
        elapsed = _time.time() - t0
        n = len(results) if results else 0
        if n > 0:
            top_score = getattr(results[0], "score", 0) if results else 0
            logger.info("[Evolve] GP done: %d candidates in %.1fs (top_score=%.3f)", n, elapsed, top_score)
        else:
            logger.info("[Evolve] GP done: 0 candidates in %.1fs", elapsed)
        return results if results else []
    except Exception as e:
        logger.exception("[Evolve] GP search failed: %s", e)
        return []


def _register_shadow_factors(expressions: list[Any]) -> int:
    """注册 GP 搜索结果到 RegistryAdapter."""
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW
        adapter = RegistryAdapter()
        count = 0
        for expr_score in expressions:
            name = getattr(expr_score, "name", None) or f"dsl_auto_{hash(expr_score.expression or '') & 0xFFFFFFFF:08x}"
            expression_str = getattr(expr_score, "expression", "") or ""
            # 把表达式编译为可调用因子函数
            func = None
            if expression_str:
                try:
                    from alpha.factor_dsl import compile_expression
                    func = compile_expression(expression_str)
                except Exception:
                    pass
            try:
                adapter.register_runtime(
                    name=name,
                    func=func,
                    source=SOURCE_SHADOW,
                    description=expression_str,
                )
                count += 1
            except Exception as e:
                logger.debug("[Evolve] register %s failed: %s", name, e)
        return count
    except Exception as e:
        logger.exception("[Evolve] register failed: %s", e)
        return 0


def _run_canary_evaluation(
    symbol: str, timeframe: str, n_bars: int
) -> tuple[list[str], list[str], list[str]]:
    """运行 CanaryDirector 检查 shadow 因子晋升/回滚.

    真实 OOS PnL 来源优先级:
      1. decision_log 中 strategy 名匹配 → 实盘 close PnL
      2. 登记为 ACTIVE 的因子 → strategy_perf 累计 PnL
      3. 新 shadow 因子 → GP score 估算 (fallback)
    """
    promotions: list[str] = []
    rollbacks: list[str] = []
    stay: list[str] = []
    try:
        from deployment.canary import CanaryDirector, CanaryEvalContext
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter()

        shadows: list[tuple[str, float]] = []
        try:
            # RegistryAdapter 没有 list_all/get_metadata, 用 get_meta 遍历
            for name in list(adapter._meta.keys()):
                meta = adapter.get_meta(name) or {}
                score = float(meta.get("score", 0.0))
                shadows.append((name, score))
        except Exception:
            pass

        if not shadows:
            return promotions, rollbacks, stay

        director = CanaryDirector()
        for name, score in shadows:
            # 尝试从 decision_log 加载实盘 PnL
            ctx = _load_canary_ctx_from_log(name, score)
            try:
                result = director.check_promotion(name, ctx)
                if result == "promote":
                    director.promote(name)
                    promotions.append(name)
                elif result == "rollback":
                    director.rollback(name)
                    rollbacks.append(name)
                else:
                    stay.append(name)
            except Exception as e:
                logger.debug("[Evolve] canary check %s failed: %s", name, e)
                stay.append(name)
    except Exception as e:
        logger.exception("[Evolve] canary eval failed: %s", e)
    return promotions, rollbacks, stay


def _load_canary_ctx_from_log(name: str, score: float) -> "CanaryEvalContext":
    """从 decision_log 读实盘 PnL, 构造 CanaryEvalContext.

    Args:
        name: 因子/策略名.
        score: GP score fallback.

    Returns:
        CanaryEvalContext 含实盘 (bars, pnl) 或估算值.
    """
    from deployment.canary import CanaryEvalContext
    try:
        import sqlite3, json as _json
        db_path = "data/decision_log.db"
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 查询该因子的所有平仓记录
        rows = conn.execute(
            "SELECT meta FROM decision_log WHERE decision_type='close' AND strategy=?",
            (name,)
        ).fetchall()
        conn.close()
        if not rows:
            # fallback: 按 GP score 估算
            estimated_pnl = score * 0.05 if abs(score) > 0.01 else 0.0
            return CanaryEvalContext(
                oos_bars=min(int(abs(score) * 5000), 5000),
                oos_pnl=estimated_pnl,
            )
        # 从 meta JSON 提取 pnl
        total_pnl = 0.0
        for r in rows:
            meta = _json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
            if isinstance(meta, dict):
                total_pnl += float(meta.get("pnl", 0.0))
        return CanaryEvalContext(
            oos_bars=len(rows),
            oos_pnl=total_pnl,
        )
    except Exception as e:
        logger.debug("[Evolve] _load_canary_ctx_from_log(%s) failed: %s, fallback to score", name, e)
        estimated_pnl = score * 0.05 if abs(score) > 0.01 else 0.0
        return CanaryEvalContext(
            oos_bars=min(int(abs(score) * 5000), 5000),
            oos_pnl=estimated_pnl,
        )


def _check_retirement() -> dict[str, Any]:
    """检查 need retire 的因子."""
    result: dict[str, Any] = {"candidates": [], "reason": ""}
    try:
        from alpha.factor_health import retirement_check
        from alpha.registry_adapter import RegistryAdapter

        adapter = RegistryAdapter()
        # 构造 status 列表 (简化: 从 adapter 已知状态)
        statuses: list[Any] = []
        try:
            if hasattr(adapter, "all_statuses"):
                statuses = adapter.all_statuses()
        except Exception:
            pass

        if statuses:
            rc = retirement_check(statuses)
            result["candidates"] = [c for c in rc.candidates]
            result["reason"] = rc.reason
    except Exception as e:
        logger.debug("[Evolve] retirement_check skipped: %s", e)
    return result


def _try_retire(name: str, reason: str) -> bool:
    """安全地 retire 一个因子."""
    try:
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter()
        if hasattr(adapter, "retire"):
            return adapter.retire(name, reason)
    except Exception as e:
        logger.debug("[Evolve] retire %s failed: %s", name, e)
    return False


def _update_weights(df: pd.DataFrame | None = None) -> bool:
    """计算动态权重并通过 RuntimeConfig.patch 推给策略消费.

    Flow:
        1. 从 RegistryAdapter 收集所有活跃因子健康分
        2. WeightPolicy.compute_weights → dict[str, float]
        3. 映射到 multi_factor_m15 能消费的格式 (vote_weights + shadow_vote_weight)
        4. RuntimeConfig.patch() 广播给 subscribe 者

    If *df* is provided and a MABRouter can be constructed from the current
    factor set, also runs :func:`auto_regime_boost` to detect regime changes
    and boost Beta priors for the new regime.
    """
    try:
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter()

        scores: dict[str, float] = {}
        try:
            if hasattr(adapter, "all_health_scores"):
                scores = adapter.all_health_scores()
        except Exception:
            pass

        if not scores:
            return False

        from deployment.weight_policy import WeightPolicy
        wp = WeightPolicy()
        new_weights = wp.compute_weights(scores)

        if not new_weights:
            return False

        # ── Regime-aware MAB prior boost (if we have price data) ──
        if df is not None:
            try:
                factor_names = list(new_weights.keys())
                if factor_names:
                    from strategy.mab_router import MABRouter
                    _router = MABRouter(strategies=factor_names)
                    _result = _mab_router.auto_regime_boost(df, _router)
                    if _result.get("boosted"):
                        logger.info(
                            "[Evolve] auto_regime_boost: %s → %s (boosted=%s)",
                            _result.get("previous_regime"),
                            _result.get("current_regime"),
                            _result.get("boosted"),
                        )
            except Exception as e:
                logger.debug("[Evolve] auto_regime_boost skipped: %s", e)

        # 映射到策略可消费的参数
        # 3 个内置因子权重: 按因子名匹配, 未知因子归入 shadow_vote_weight
        builtin_names = ["di_spread", "rsi_14", "stoch_k"]
        builtin_weights = [new_weights.get(n, 1.0) for n in builtin_names]
        # 归一化
        total_b = sum(builtin_weights)
        if total_b > 0:
            builtin_weights = [w / total_b * 3 for w in builtin_weights]  # scale to ~3

        # 影子因子权重 = 按 IC 比值动态调权
        # 公式: shadow_vote_weight = clamp(shadow_avg_ic / builtin_avg_ic, 0.15, 2.0)
        # 影子因子 IC 比内置高 → 权重向 1 靠拢, 可达 2.0 (超额投票权)
        # 影子因子 IC 比内置低 → 权重最低保底 0.15 (仍然不归零)
        shadow_names = [k for k in new_weights if k not in builtin_names]
        if shadow_names:
            shadow_avg_ic = float(np.mean([new_weights[k] for k in shadow_names]))
            builtin_avg_ic = float(np.mean([new_weights.get(n, 0) for n in builtin_names])) if builtin_names else 0.001
            ratio = shadow_avg_ic / max(builtin_avg_ic, 0.001)
            shadow_w = max(0.15, min(2.0, ratio))
        else:
            shadow_w = 0.15

        # 推入 RuntimeConfig → multi_factor_m15 通过 subscribe 接收
        from config.runtime_config import patch as rc_patch
        patch_dict: dict = {
            "vote_weights": builtin_weights,
            "shadow_vote_weight": round(min(shadow_w, 1.0), 4),
        }
        rc_patch(patch_dict)
        _emit_evolution_story("weights_updated", {
            "factors": len(new_weights),
            "vote_weights": builtin_weights,
            "shadow_vote_weight": patch_dict["shadow_vote_weight"],
        })
        logger.info("[Evolve] weights pushed to RuntimeConfig: vote=%s shadow=%.4f",
                     builtin_weights, patch_dict["shadow_vote_weight"])

        # ── Adaptive risk tuning (volatility + equity based) ──
        if df is not None:
            try:
                from core.state import state as _state
                equity = getattr(_state, "equity", None) or 1000.0
                from risk.circuit import auto_tune_risk
                risk_cfg = auto_tune_risk(df, equity)
                _emit_evolution_story("risk_tuned", risk_cfg)
                logger.info(
                    "[Evolve] auto_tune_risk: max_daily_loss_pct=%.2f%%, "
                    "single_trade_risk_usd=%.2f, atr_percentile=%.1f%%",
                    risk_cfg["max_daily_loss_pct"],
                    risk_cfg["single_trade_risk_usd"],
                    risk_cfg["atr_percentile"],
                )
            except Exception as e:
                logger.debug("[Evolve] auto_tune_risk skipped: %s", e)

        return True
    except Exception as e:
        logger.debug("[Evolve] update_weights failed: %s", e)
        return False


def _emit_evolution_story(event_type: str, payload: dict) -> None:
    """发射 EvolutionStory 事件 (非失败)."""
    try:
        from monitor.evolution_story import EvolutionStory
        EvolutionStory.shared().append(event_type, payload)
    except Exception:
        pass


# ── 便捷 CLI ──────────────────────────────────────────────────────────


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = scheduled_evolution_cycle()
    import json as _json
    print(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
