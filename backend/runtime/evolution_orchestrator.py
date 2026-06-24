"""backend/runtime/evolution_orchestrator.py — 自进化编排器主循环 (v3: DB统一).

把 GP 搜索 → 注册 shadow → Canary 晋升(持久化+执行) → 退役检查(执行)
→ 权重更新 → 串成端到端闭环管线。

v3 修复 (audit 2026-06-22):
  - 全部读写改用 STATE_DB (统一状态库), 不再用 decision_log.db
  - canary_state / decision_log 都从 state.db 读写
  - 晋升后真正调用 adapter.promote() 更新因子 source
  - 退役检查后真正调用 adapter.retire() 移除因子
  - 权重更新推送 factor_portfolio_weights (AWE 读同一字段)
"""

from __future__ import annotations

import json as _json
import logging
import sqlite3
import time as _time
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from strategy import mab_router as _mab_router

logger = logging.getLogger(__name__)

# audit v3: 统一使用 STATE_DB, 不再用 decision_log.db
from backend.core.db import STATE_DB as _CANARY_DB


def _ensure_canary_db() -> None:
    """确保 canary_state 表存在 (state.db, DDL已在backend/core/db.py定义)."""
    # state.db 的 DDL 已包含 canary_state 表, 此处幂等创建以防万一
    try:
        conn = sqlite3.connect(str(_CANARY_DB))
        conn.execute("""
            CREATE TABLE IF NOT EXISTS canary_state (
                factor_name TEXT PRIMARY KEY,
                stage TEXT NOT NULL DEFAULT 'SHADOW',
                oos_bars INTEGER DEFAULT 0,
                cumulative_pnl REAL DEFAULT 0.0,
                promote_time REAL DEFAULT 0.0,
                events_json TEXT DEFAULT '[]',
                updated_at REAL DEFAULT 0.0
            )
        """)
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("[Evolve] canary_state table init: %s", e)


def _load_canary_states() -> dict[str, dict]:
    """从 state.db 加载所有 canary 状态."""
    states: dict[str, dict] = {}
    try:
        _ensure_canary_db()
        conn = sqlite3.connect(str(_CANARY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM canary_state").fetchall()
        conn.close()
        for r in rows:
            name = r["factor_name"]
            try:
                events = _json.loads(r["events_json"]) if r["events_json"] else []
            except Exception:
                events = []
            states[name] = {
                "stage": r["stage"], "oos_bars": r["oos_bars"],
                "cumulative_pnl": r["cumulative_pnl"], "promote_time": r["promote_time"],
                "events": events, "updated_at": r["updated_at"],
            }
        if states:
            return states
    except Exception as e:
        logger.debug("[Evolve] load canary from DB: %s", e)
    return {}


def _save_canary_states(states: dict[str, dict]) -> None:
    """持久化 canary 状态到 state.db."""
    try:
        _ensure_canary_db()
        conn = sqlite3.connect(str(_CANARY_DB))
        now = _time.time()
        for name, s in states.items():
            events_json = _json.dumps(s.get("events", []), ensure_ascii=False)
            conn.execute("""
                INSERT OR REPLACE INTO canary_state
                (factor_name, stage, oos_bars, cumulative_pnl, promote_time, events_json, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                name,
                s.get("stage", "SHADOW"),
                s.get("oos_bars", 0),
                s.get("cumulative_pnl", 0.0),
                s.get("promote_time", 0.0),
                events_json,
                now,
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Evolve] save canary to state.db failed: %s", e)


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
    timeframe: str = "M5",
    n_bars: int = 8000,
    gp_pop: int = 50,
    gp_gen: int = 20,
    gp_top_k: int = 10,
    progress_cb: Callable[[str, float, str], None] | None = None,
) -> EvolutionReport:
    """执行一次完整的自进化循环 (闭环)."""
    report = EvolutionReport()
    cb = progress_cb or (lambda *_: None)
    t0 = _time.time()

    try:
        _emit_evolution_story("cycle_start", {})
        cb("start", 0, "evolution cycle starting")

        # ── Step 0: DataQualityGate (P3.3) ──
        try:
            from data.quality_gate import run_quality_gate, evolution_guard
            dq_report = run_quality_gate(
                symbol=symbol,
                max_lag_seconds=3600,
            )
            if not evolution_guard(dq_report):
                report.error = f"quality_gate: {dq_report.detail}"
                _emit_evolution_story("cycle_error", {"error": report.error})
                report.duration_sec = _time.time() - t0
                return report
        except Exception as dq_err:
            logger.debug("[Evolve] quality gate skipped: %s", dq_err)

        # ── Step 1: 加载数据 ──
        df = _load_bars(symbol, timeframe, n_bars)
        if df is None or len(df) < 500:
            report.error = f"insufficient bars: {len(df) if df is not None else 0}"
            logger.warning("[Evolve] %s", report.error)
            _emit_evolution_story("cycle_error", {"error": report.error})
            report.duration_sec = _time.time() - t0
            return report
        cb("data_loaded", 10, f"loaded {len(df)} bars")

        # ── Step 2: GP 搜索 ──
        if gp_pop > 0 and gp_gen > 0:
            cb("gp_search", 15, f"GP search pop={gp_pop} gen={gp_gen}")
            expressions = _run_gp(df, pop=gp_pop, gen=gp_gen, top_k=gp_top_k)
            report.gp_new_candidates = len(expressions)
            logger.info("[Evolve] GP found %d candidates", len(expressions))
            cb("gp_done", 40, f"GP found {len(expressions)} candidates")
        else:
            expressions = []
            cb("gp_skip", 40, "GP skipped")

        if expressions:
            cb("register", 42, f"registering {len(expressions)} shadow factors")
            registered = _register_shadow_factors(expressions)
            report.gp_registered_shadow = registered
            logger.info("[Evolve] registered %d shadow factors", registered)
            _emit_evolution_story("shadow_registered", {
                "count": registered, "source": "gp_search",
            })
            cb("register_done", 50, f"registered {registered} factors")
        else:
            logger.info("[Evolve] no new GP candidates")

        # ── Step 3: Shadow 绩效刷新 + Canary 评估 (持久化 + 真正晋升) ──
        cb("shadow_perf", 52, "refreshing shadow factor performance")
        shadow_count = _update_shadow_performance(df, symbol, timeframe)
        if shadow_count:
            logger.info("[Evolve] shadow perf refreshed: %d factors", shadow_count)

        # ── Step 3b: Canary 评估 (持久化 + 真正晋升) ──
        cb("canary", 55, "running canary evaluation")
        promotions, rollbacks, stay = _run_canary_evaluation(
            symbol, timeframe, n_bars
        )
        report.canary_promotions = promotions
        report.canary_rollbacks = rollbacks
        report.canary_stay = stay

        if promotions:
            logger.info("[Evolve] canary promoted: %s", promotions)
            # ★ 真正执行晋升: 更新 RegistryAdapter source
            _execute_promotions(promotions)
            _emit_evolution_story("canary_promotions", {
                "promoted": promotions, "rollbacked": rollbacks,
            })
        if rollbacks:
            logger.info("[Evolve] canary rolled back: %s", rollbacks)
            _execute_rollbacks(rollbacks)
        cb("canary_done", 70, f"promoted {len(promotions)}, rolled {len(rollbacks)}")

        # ── Step 4: 退役检查 ──
        cb("retirement", 75, "checking factor retirement")
        retire_info = _check_retirement()
        report.retire_candidates = retire_info["candidates"]
        report.retire_reason = retire_info["reason"]
        if retire_info["candidates"]:
            logger.info("[Evolve] retiring: %s", retire_info["candidates"])
            for name in retire_info["candidates"]:
                if _try_retire(name, retire_info["reason"]):
                    logger.info("[Evolve] retired: %s", name)
        cb("retirement_done", 85, f"retired {len(retire_info['candidates'])} factors")

        # ── Step 5: IC 刷新 + 因子健康报告 ──
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

        try:
            from alpha.factor_health import evaluate_factors, write_report
            from backend.core.paths import CHARTS_DIR
            report_result = evaluate_factors(df, threshold=0.04)
            out_txt = CHARTS_DIR / "factor_health_report.txt"
            out_json = CHARTS_DIR / "factor_health_report.json"
            write_report(report_result, out_txt, out_json)
            logger.info(
                "[Evolve] factor health report: healthy=%d watch=%d decaying=%d",
                report_result.get("healthy", 0),
                report_result.get("watch", 0),
                report_result.get("decaying", 0),
            )
        except Exception as e:
            logger.debug("[Evolve] factor health report skipped: %s", e)

        # ── Step 6: 权重更新 (推送到 AWE 消费同一字段) ──
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
    try:
        from data.store import DataStore
        ds = DataStore()
        return ds.load_bars(symbol, timeframe, limit=n_bars)
    except Exception as e:
        logger.warning("[Evolve] load_bars failed: %s", e)
        return None


def _run_gp(df: pd.DataFrame, pop: int = 50, gen: int = 20, top_k: int = 10) -> list[Any]:
    try:
        from alpha.factor_search_gp import run_gp_search
        t0 = _time.time()
        results = run_gp_search(df, pop=pop, gen=gen, top_k=top_k,
                                progress_cb=lambda step, pct, msg: None)
        elapsed = _time.time() - t0
        n = len(results) if results else 0
        if n > 0:
            top_score = getattr(results[0], "score", 0) if results else 0
            logger.info("[Evolve] GP done: %d candidates in %.1fs (top=%.3f)", n, elapsed, top_score)
        else:
            logger.info("[Evolve] GP done: 0 candidates in %.1fs", elapsed)
        return results if results else []
    except Exception as e:
        logger.exception("[Evolve] GP search failed: %s", e)
        return []


def _register_shadow_factors(expressions: list[Any]) -> int:
    """注册 GP 搜索结果为影子因子 (SOURCE_SHADOW).

    影子因子进入 factor_registry 但不参与投票——StreamingFactorEngine
    在每 tick 通过 adapter.get_meta() 检查 source, 跳过 shadow。
    只有通过 Canary 晋升为 SOURCE_DISCOVERED 后才参与交易。
    """
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW
        adapter = RegistryAdapter.shared()
        count = 0
        for expr_score in expressions:
            name = getattr(expr_score, "name", None) or \
                   f"dsl_auto_{hash(expr_score.expression or '') & 0xFFFFFFFF:08x}"
            expression_str = getattr(expr_score, "expression", "") or ""
            func = None
            if expression_str:
                try:
                    from alpha.factor_dsl import evaluate_dsl
                    func = lambda df, _expr=expression_str: evaluate_dsl(_expr, df)
                except Exception:
                    pass
            try:
                adapter.register_runtime(
                    name=name, func=func, source=SOURCE_SHADOW,
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
    """运行 CanaryDirector, 从持久化状态恢复, 评估后写回。

    Returns: (promotions, rollbacks, stay)
    """
    promotions: list[str] = []
    rollbacks: list[str] = []
    stay: list[str] = []
    saved_states: dict[str, dict] = {}

    try:
        from deployment.canary import ACTIVE, CANARY_STAGES, SHADOW, QUARANTINED, RETIRED, TERMINAL_STAGES, CanaryDirector, CanaryEvalContext
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter.shared()

        # 加载持久化状态
        saved_states = _load_canary_states()

        # 收集所有影子 + 已发现因子
        candidates: list[tuple[str, float, str]] = []
        for name in list(adapter._meta.keys()):
            meta = adapter.get_meta(name) or {}
            source = meta.get("source", "")
            score = float(meta.get("score", 0.0))
            if source in ("shadow", "discovered"):
                candidates.append((name, score, source))

        if not candidates:
            return promotions, rollbacks, stay

        director = CanaryDirector()
        candidate_names = {name for name, _, _ in candidates}

        # 恢复持久化状态到 director
        for name, state in saved_states.items():
            if name in candidate_names:
                dir_state = director.get_state(name)
                stage = str(state.get("stage", SHADOW)).upper()
                # P1.2: 保留 QUARANTINED / RETIRED 状态 (不重置到 SHADOW)
                valid_stages = set(CANARY_STAGES) | TERMINAL_STAGES
                dir_state.stage = stage if stage in valid_stages else SHADOW
                dir_state.oos_bars = state.get("oos_bars", 0)
                dir_state.cumulative_pnl = state.get("cumulative_pnl", 0.0)
                dir_state.promote_time = state.get("promote_time", 0.0)

        for name, score, source in candidates:
            # ★ P0.1: 不再 bypass canary validation. 所有因子 (无论 source
            # 是什么) 都从 canary_state 恢复 stage, 走标准 canary 管道。
            # 移除了 "discovered源且无saved_states→直接ACTIVE" 的捷径。
            ctx = _load_canary_ctx_from_log(name, score)
            try:
                result = director.check_promotion(name, ctx)
                if result == "promote":
                    director.promote(name)
                    if director.get_stage(name) == ACTIVE:
                        promotions.append(name)
                    else:
                        stay.append(name)
                elif result == "rollback":
                    director.rollback(name)
                    rollbacks.append(name)
                else:
                    stay.append(name)
            except Exception as e:
                logger.debug("[Evolve] canary check %s failed: %s", name, e)
                stay.append(name)

        # ★ 持久化 director 状态到 DB
        new_states: dict[str, dict] = {}
        for name, _, _ in candidates:
            s = director.get_state(name)
            new_states[name] = {
                "stage": s.stage,
                "oos_bars": s.oos_bars,
                "cumulative_pnl": s.cumulative_pnl,
                "promote_time": s.promote_time,
                "events": [dict(e) for e in getattr(s, "_events", [])],
                "updated_at": _time.time(),
            }
        _save_canary_states(new_states)

    except Exception as e:
        logger.exception("[Evolve] canary eval failed: %s", e)

    return promotions, rollbacks, stay


def _execute_promotions(names: list[str]) -> None:
    """真正执行晋升: adapter.promote(name, SOURCE_DISCOVERED)."""
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_DISCOVERED
        adapter = RegistryAdapter.shared()
        for name in names:
            try:
                ok = adapter.promote(name, new_source=SOURCE_DISCOVERED,
                                     reason="canary_promotion")
                if ok:
                    logger.info("[Evolve] ✓ promoted %s → DISCOVERED", name)
            except Exception as e:
                logger.debug("[Evolve] promote %s failed: %s", name, e)
    except Exception as e:
        logger.debug("[Evolve] execute_promotions failed: %s", e)


def _execute_rollbacks(names: list[str]) -> None:
    """回滚因子到 SOURCE_SHADOW."""
    try:
        from alpha.registry_adapter import RegistryAdapter, SOURCE_SHADOW
        adapter = RegistryAdapter.shared()
        for name in names:
            try:
                adapter.promote(name, new_source=SOURCE_SHADOW,
                                reason="canary_rollback")
                logger.info("[Evolve] ↺ rolled back %s → SHADOW", name)
            except Exception as e:
                logger.debug("[Evolve] rollback %s failed: %s", name, e)
    except Exception as e:
        logger.debug("[Evolve] execute_rollbacks failed: %s", e)


def _update_shadow_performance(df: pd.DataFrame, symbol: str, timeframe: str) -> int:
    """Refresh shadow/discovered virtual performance for Canary."""
    try:
        from alpha.shadow_trader import evaluate_shadow_factors
        results = evaluate_shadow_factors(
            df,
            symbol=symbol,
            timeframe=timeframe,
            sources=("shadow", "discovered"),
            persist=True,
        )
        if results:
            _emit_evolution_story("shadow_perf_updated", {
                "count": len(results),
                "factors": list(results.keys())[:20],
            })
        return len(results)
    except Exception as e:
        logger.debug("[Evolve] shadow perf refresh skipped: %s", e)
        return 0


def _load_canary_ctx_from_log(name: str, score: float) -> "CanaryEvalContext":
    """加载因子在 shadow 阶段的真实 OOS 绩效.

    P1.3: 优先使用 shadow_factor_perf 表 (影子虚拟交易真实 PnL),
          然后 decision_log 中的 close 记录,
          最后才回退到基于 score 的估算 (并打警告).
    """
    from deployment.canary import CanaryEvalContext

    # ── 首选: shadow_factor_perf 真实影子交易数据 ──
    try:
        from alpha.shadow_trader import load_shadow_perf
        perf = load_shadow_perf(name)
        if perf is not None and perf.oos_bars > 0:
            logger.info("[Evolve] canary_ctx(%s): shadow_factor_perf oos_bars=%d pnl=%.4f",
                        name, perf.oos_bars, perf.cumulative_pnl)
            return CanaryEvalContext(
                oos_bars=perf.oos_bars,
                oos_pnl=perf.cumulative_pnl,
                additional_metrics={
                    "source": "shadow_factor_perf",
                    "hit_rate": perf.hit_rate,
                    "max_drawdown": perf.max_drawdown,
                    "last_signal": perf.last_signal,
                },
            )
    except Exception as e:
        logger.debug("[Evolve] shadow canary_ctx(%s) skipped: %s", name, e)

    # ── 次选: decision_log close 记录 ──
    try:
        conn = sqlite3.connect(str(_CANARY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT meta FROM decision_log WHERE decision_type='close' AND strategy=?",
            (name,)
        ).fetchall()
        conn.close()
        if rows:
            total_pnl = 0.0
            for r in rows:
                meta = _json.loads(r["meta"]) if isinstance(r["meta"], str) else (r["meta"] or {})
                if isinstance(meta, dict):
                    total_pnl += float(meta.get("pnl", 0.0))
            logger.info("[Evolve] canary_ctx(%s): decision_log rows=%d pnl=%.4f",
                        name, len(rows), total_pnl)
            return CanaryEvalContext(oos_bars=len(rows), oos_pnl=total_pnl)
    except Exception as e:
        logger.debug("[Evolve] canary_ctx(%s) decision_log failed: %s", name, e)

    # ── 最后: 基于 score 的估算 (无真实数据) ──
    estimated_pnl = score * 0.05 if abs(score) > 0.01 else 0.0
    estimated_bars = min(int(abs(score) * 5000), 5000)
    logger.warning("[Evolve] canary_ctx(%s): no real perf data, estimated pnl=%.4f bars=%d",
                   name, estimated_pnl, estimated_bars)
    return CanaryEvalContext(
        oos_bars=estimated_bars,
        oos_pnl=estimated_pnl,
    )


def _check_retirement() -> dict[str, Any]:
    result: dict[str, Any] = {"candidates": [], "reason": ""}
    try:
        from alpha.factor_health import retirement_check
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter.shared()
        statuses = adapter.all_statuses()
        if statuses:
            rc = retirement_check(statuses)
            result["candidates"] = [c for c in rc.candidates]
            result["reason"] = rc.reason
    except Exception as e:
        logger.debug("[Evolve] retirement_check: %s", e)
    return result


def _try_retire(name: str, reason: str) -> bool:
    try:
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter.shared()
        return adapter.retire(name, reason)
    except Exception as e:
        logger.debug("[Evolve] retire %s failed: %s", name, e)
        return False


def _collect_learning_suggestions(max_age_days: int = 30) -> tuple[dict[str, dict], dict[str, dict]]:
    """Collect rule-learning suggestions from state.db.

    Returns:
        summary_by_factor:
            {
                factor: {
                    "proposed": int,
                    "approved": int,
                    "latest_action": str,
                    "latest_confidence": float,
                }
            }
        approved_biases:
            {
                factor: {
                    "multiplier": float,
                    "action": str,
                    "suggestion_ids": list[str],
                }
            }
    """
    summary: dict[str, dict] = {}
    approved_biases: dict[str, dict] = {}
    try:
        cutoff = _time.time() - max_age_days * 86400
        conn = sqlite3.connect(str(_CANARY_DB))
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT suggestion_id, scope_key, action, confidence, status, created_at
            FROM policy_suggestion
            WHERE scope_type='factor' AND created_at>=?
              AND status IN ('proposed', 'approved')
            ORDER BY created_at DESC
            """,
            (cutoff,),
        ).fetchall()
        conn.close()

        for row in rows:
            factor = str(row["scope_key"] or "")
            if not factor:
                continue
            action = str(row["action"] or "watch")
            confidence = float(row["confidence"] or 0.0)
            status = str(row["status"] or "proposed")
            item = summary.setdefault(
                factor,
                {"proposed": 0, "approved": 0, "latest_action": action, "latest_confidence": confidence},
            )
            if status == "approved":
                item["approved"] += 1
            else:
                item["proposed"] += 1
            # first row is latest because rows ordered desc
            if item["latest_action"] == action and item["latest_confidence"] == confidence:
                pass

            if status != "approved":
                continue
            if action not in {"downweight", "boost_small"}:
                continue

            bias_info = approved_biases.get(
                factor,
                {"multiplier": 1.0, "action": action, "suggestion_ids": []},
            )
            current = float(bias_info.get("multiplier", 1.0))
            if action == "downweight":
                current *= max(0.80, 1.0 - 0.20 * min(confidence, 1.0))
            elif action == "boost_small":
                current *= min(1.08, 1.0 + 0.08 * min(confidence, 1.0))
            bias_info["multiplier"] = min(1.10, max(0.70, current))
            bias_info["action"] = action
            bias_info["suggestion_ids"].append(str(row["suggestion_id"]))
            approved_biases[factor] = bias_info
    except Exception as e:
        logger.debug("[Evolve] collect learning suggestions: %s", e)
    return summary, approved_biases


def _apply_learning_biases(
    weights: dict[str, float],
    approved_biases: dict[str, dict],
) -> tuple[dict[str, float], dict[str, dict]]:
    """Apply small approved learning biases on top of WeightPolicy output."""
    if not weights or not approved_biases:
        return dict(weights or {}), {}

    adjusted = dict(weights)
    applied: dict[str, dict] = {}
    for factor, bias_info in approved_biases.items():
        if factor not in adjusted:
            continue
        mult = float((bias_info or {}).get("multiplier", 1.0))
        old_w = float(adjusted[factor] or 0.0)
        new_w = max(0.0, old_w * float(mult))
        adjusted[factor] = new_w
        applied[factor] = {
            "multiplier": round(float(mult), 6),
            "action": str((bias_info or {}).get("action", "watch")),
            "suggestion_ids": list((bias_info or {}).get("suggestion_ids", []) or []),
            "old_weight": round(old_w, 6),
            "biased_weight": round(new_w, 6),
        }

    total = sum(float(v) for v in adjusted.values())
    if total > 0:
        adjusted = {k: round(float(v) / total, 6) for k, v in adjusted.items()}
        for factor, info in applied.items():
            if factor in adjusted:
                info["new_weight"] = adjusted[factor]
    return adjusted, applied


def _update_weights(df: pd.DataFrame | None = None) -> bool:
    """计算动态权重并推入 factor_portfolio_weights (AWE 同一字段).

    从健康报告读取分数 → WeightPolicy + Shadow OOS → DecisionPolicy → RuntimeConfig.patch.
    DecisionPolicy 是唯一写路径, 解决 AWE 和 WeightPolicy 互相覆盖的问题.
    """
    try:
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter.shared()
        scores = adapter.all_health_scores()
        if not scores:
            return False

        # 规则学习治理: proposed -> approved/rejected, approved -> rolled_back
        governance_result = {}
        try:
            from research.learning.governor import RuleEvolutionGovernor
            _gov = RuleEvolutionGovernor()
            governance_result["review_pending"] = _gov.review_pending()
            governance_result["reconcile_active"] = _gov.reconcile_active()
            governance_result["reconcile_application_effects"] = _gov.reconcile_application_effects()
            _emit_evolution_story("learning_governance", governance_result)
        except Exception as e:
            logger.debug("[Evolve] learning governance: %s", e)
            _gov = None

        from config.runtime_config import shared as _rc
        cfg = _rc()

        # 来源 A: WeightPolicy 健康分权重
        from deployment.weight_policy import WeightPolicy
        wp = WeightPolicy()
        wp_weights = wp.compute_weights(scores)
        if not wp_weights:
            return False

        # 来源 A2: 规则学习建议 (proposed 只做观测, approved 才做小偏置)
        learning_summary, approved_biases = _collect_learning_suggestions()
        if learning_summary:
            _emit_evolution_story("learning_suggestions_seen", {
                "factors": len(learning_summary),
                "summary": learning_summary,
                "approved_biases": approved_biases,
            })
        if approved_biases:
            wp_weights, applied_biases = _apply_learning_biases(wp_weights, approved_biases)
            logger.info("[Evolve] applied learning biases to %d factors", len(applied_biases))
        else:
            applied_biases = {}

        # 来源 B: Shadow OOS 绩效 (从 state.db 读取)
        shadow_perfs = {}
        try:
            from alpha.shadow_trader import load_shadow_perf
            for name in wp_weights:
                perf = load_shadow_perf(name)
                if perf is not None:
                    shadow_perfs[name] = perf
        except Exception as e:
            logger.debug("[Evolve] load shadow perfs: %s", e)

        # 来源 C: 当前权重 (已有 AWE 调整过的)
        current_weights = dict(cfg.factor_portfolio_weights)

        # 来源 D: Regime — 沿用现有 MAB router 逻辑
        regime = None
        if df is not None:
            try:
                from strategy.mab_router import MABRouter
                _router = MABRouter(strategies=list(wp_weights.keys()))
                _result = _mab_router.auto_regime_boost(df, _router)
                if _result.get("boosted"):
                    regime = _result.get("current_regime")
                    logger.info("[Evolve] regime_boost: %s→%s",
                                _result.get("previous_regime"), regime)
            except Exception as e:
                logger.debug("[Evolve] regime_boost: %s", e)

        # ★ 统一决策入口: DecisionPolicy 融合所有来源
        from alpha.decision_policy import DecisionPolicy
        dp = DecisionPolicy()
        decisions = dp.decide(
            awe_patches=None,  # evolution 不运行 awe.adapt (有 awe_adapt job)
            weight_policy_weights=wp_weights,
            shadow_perfs=shadow_perfs,
            factor_configs=cfg.factor_signal_config,
            current_weights=current_weights,
            regime=regime,
        )
        new_weights = DecisionPolicy.to_weights(decisions)
        if not new_weights:
            return False

        from config.runtime_config import patch as rc_patch
        rc_patch({"factor_portfolio_weights": new_weights})
        _emit_evolution_story("weights_updated", {
            "factors": len(new_weights),
            "factor_portfolio_weights": new_weights,
            "learning_biases": applied_biases,
        })
        if applied_biases and _gov is not None:
            cycle_ts = _time.time()
            for factor, info in applied_biases.items():
                try:
                    _gov.log_application(
                        scope_type="factor",
                        scope_key=factor,
                        action=str(info.get("action", "watch")),
                        bias_multiplier=float(info.get("multiplier", 1.0)),
                        old_weight=float(info.get("old_weight", 0.0)),
                        new_weight=float(info.get("new_weight", info.get("biased_weight", 0.0))),
                        suggestion_ids=list(info.get("suggestion_ids", []) or []),
                        cycle_ts=cycle_ts,
                        details={
                            "governance": governance_result,
                            "biased_weight": info.get("biased_weight", 0.0),
                        },
                    )
                except Exception as e:
                    logger.debug("[Evolve] learning application log %s: %s", factor, e)
        logger.info("[Evolve] weights: %d factors → factor_portfolio_weights (via DecisionPolicy)",
                    len(new_weights))

        if df is not None:
            try:
                from core.state import state as _state
                equity = getattr(_state, "equity", None) or 1000.0
                from risk.circuit import auto_tune_risk
                risk_cfg = auto_tune_risk(df, equity)
                _emit_evolution_story("risk_tuned", risk_cfg)
            except Exception as e:
                logger.debug("[Evolve] auto_tune_risk: %s", e)

        return True
    except Exception as e:
        logger.debug("[Evolve] update_weights: %s", e)
        return False


def _emit_evolution_story(event_type: str, payload: dict) -> None:
    try:
        from monitor.evolution_story import EvolutionStory
        EvolutionStory.shared().append(event_type, payload)
    except Exception:
        pass


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    report = scheduled_evolution_cycle()
    print(_json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
