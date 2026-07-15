"""backend/runtime/evolution_orchestrator.py — 自进化编排器主循环 (v3: DB统一).

把 GP 搜索 → 注册 shadow → Canary 证据刷新 → 退役候选发现
→ 权重研究 → 串成端到端研究管线。生命周期执行统一交给
FactorGovernanceOrchestrator，避免两个调度器同时拥有晋升/退役权。

v3 修复 (audit 2026-06-22), PG 迁移更新 (2026-07-01):
  - 全部运行状态读写改用 PostgreSQL state store, 不再用 decision_log.db
  - canary_state / decision_log 都从 PostgreSQL state store 读写
  - 晋升后真正调用 adapter.promote() 更新因子 source
  - 退役检查后真正调用 adapter.retire() 移除因子
  - 权重更新推送 factor_portfolio_weights (AWE 读同一字段)
"""

from __future__ import annotations

import json as _json
import logging
import os
import time as _time
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np
import pandas as pd

from strategy import mab_router as _mab_router

logger = logging.getLogger(__name__)

from backend.core.db import connect_sqlite, get_state_pg_conn


_CANARY_DB = None


def _state_conn(*, read_only: bool = False):
    return get_state_pg_conn(read_only=read_only)


def _ensure_canary_db() -> None:
    """确保 PostgreSQL state store 中 canary_state 表存在."""
    try:
        conn = _state_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS canary_state (
                factor_name TEXT PRIMARY KEY,
                stage TEXT NOT NULL DEFAULT 'SHADOW',
                oos_bars INTEGER DEFAULT 0,
                cumulative_pnl REAL DEFAULT 0.0,
                promote_time REAL DEFAULT 0.0,
                rollback_count INTEGER DEFAULT 0,
                evidence_hash TEXT DEFAULT '',
                dataset_hash TEXT DEFAULT '',
                evidence_end_at TEXT DEFAULT '',
                stage_evidence_hash TEXT DEFAULT '',
                fresh_evidence_bars INTEGER DEFAULT 0,
                events_json TEXT DEFAULT '[]',
                updated_at REAL DEFAULT 0.0
            )
        """)
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS rollback_count INTEGER DEFAULT 0")
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS evidence_hash TEXT DEFAULT ''")
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS dataset_hash TEXT DEFAULT ''")
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS evidence_end_at TEXT DEFAULT ''")
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS stage_evidence_hash TEXT DEFAULT ''")
        conn.execute("ALTER TABLE canary_state ADD COLUMN IF NOT EXISTS fresh_evidence_bars INTEGER DEFAULT 0")
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("[Evolve] canary_state table init: %s", e)


def _load_canary_states() -> dict[str, dict]:
    """从 PostgreSQL state store 加载所有 canary 状态."""
    states: dict[str, dict] = {}
    try:
        _ensure_canary_db()
        conn = _state_conn(read_only=True)
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
                "cumulative_pnl": r["cumulative_pnl"],
                "promote_time": r["promote_time"],
                "rollback_count": r["rollback_count"],
                "evidence_hash": r["evidence_hash"] or "",
                "dataset_hash": r["dataset_hash"] or "",
                "evidence_end_at": r["evidence_end_at"] or "",
                "stage_evidence_hash": r["stage_evidence_hash"] or "",
                "fresh_evidence_bars": int(r["fresh_evidence_bars"] or 0),
                "events": events,
                "updated_at": r["updated_at"],
            }
        if states:
            return states
    except Exception as e:
        logger.debug("[Evolve] load canary from DB: %s", e)
    return {}


def _save_canary_states(states: dict[str, dict]) -> None:
    """持久化 canary 状态到 PostgreSQL state store."""
    try:
        _ensure_canary_db()
        conn = _state_conn()
        now = _time.time()
        for name, s in states.items():
            events_json = _json.dumps(s.get("events", []), ensure_ascii=False)
            conn.execute("""
                INSERT INTO canary_state
                (factor_name, stage, oos_bars, cumulative_pnl, promote_time, rollback_count,
                 evidence_hash, dataset_hash, evidence_end_at, stage_evidence_hash,
                 fresh_evidence_bars, events_json, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT(factor_name) DO UPDATE SET
                    stage=excluded.stage,
                    oos_bars=excluded.oos_bars,
                    cumulative_pnl=excluded.cumulative_pnl,
                    promote_time=excluded.promote_time,
                    rollback_count=excluded.rollback_count,
                    evidence_hash=excluded.evidence_hash,
                    dataset_hash=excluded.dataset_hash,
                    evidence_end_at=excluded.evidence_end_at,
                    stage_evidence_hash=excluded.stage_evidence_hash,
                    fresh_evidence_bars=excluded.fresh_evidence_bars,
                    events_json=excluded.events_json,
                    updated_at=excluded.updated_at
            """, (
                name,
                s.get("stage", "SHADOW"),
                s.get("oos_bars", 0),
                s.get("cumulative_pnl", 0.0),
                s.get("promote_time", 0.0),
                s.get("rollback_count", 0),
                s.get("evidence_hash", ""),
                s.get("dataset_hash", ""),
                s.get("evidence_end_at", ""),
                s.get("stage_evidence_hash", ""),
                s.get("fresh_evidence_bars", 0),
                events_json,
                now,
            ))
        conn.commit()
        conn.close()
    except Exception as e:
        logger.warning("[Evolve] save canary to PostgreSQL state store failed: %s", e)


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
        self.lifecycle_executor: str = "factor_governance_autonomous"
        self.lifecycle_actions_applied: bool = False
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
            "lifecycle_executor": self.lifecycle_executor,
            "lifecycle_actions_applied": self.lifecycle_actions_applied,
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
        split_at = max(500, int(len(df) * 0.80))
        split_at = min(split_at, len(df) - 100)
        research_df = df.iloc[:split_at].copy()
        shadow_oos_df = df.iloc[split_at:].copy()
        if len(shadow_oos_df) < 100:
            report.error = f"insufficient shadow OOS bars: {len(shadow_oos_df)}"
            report.duration_sec = _time.time() - t0
            return report

        # ── Step 2: GP 搜索 ──
        if gp_pop > 0 and gp_gen > 0:
            cb("gp_search", 15, f"GP search pop={gp_pop} gen={gp_gen}")
            expressions = _run_gp(research_df, pop=gp_pop, gen=gp_gen, top_k=gp_top_k)
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

        # ── Step 3: Shadow 绩效刷新 + Canary 候选评估 ──
        cb("shadow_perf", 52, "refreshing shadow factor performance")
        shadow_count = _update_shadow_performance(shadow_oos_df, symbol, timeframe)
        if shadow_count:
            logger.info("[Evolve] shadow perf refreshed: %d factors", shadow_count)

        # ── Step 3b: Canary 评估（只写证据/候选，不执行生命周期变更） ──
        cb("canary", 55, "running canary evaluation")
        promotions, rollbacks, stay = _run_canary_evaluation(
            symbol, timeframe, n_bars
        )
        report.canary_promotions = promotions
        report.canary_rollbacks = rollbacks
        report.canary_stay = stay

        if promotions:
            logger.info("[Evolve] canary promotion candidates: %s", promotions)
            _emit_evolution_story("canary_transition_candidates", {
                "promotion_candidates": promotions,
                "rollback_candidates": rollbacks,
                "executor": report.lifecycle_executor,
            })
        if rollbacks:
            logger.info("[Evolve] canary rollback candidates: %s", rollbacks)
        cb("canary_done", 70, f"promotion candidates {len(promotions)}, rollback candidates {len(rollbacks)}")

        # ── Step 4: 退役检查 ──
        cb("retirement", 75, "checking factor retirement")
        retire_info = _check_retirement()
        report.retire_candidates = retire_info["candidates"]
        report.retire_reason = retire_info["reason"]
        if retire_info["candidates"]:
            logger.info("[Evolve] retirement candidates: %s", retire_info["candidates"])
            _emit_evolution_story("factor_retirement_candidates", {
                "candidates": retire_info["candidates"],
                "reason": retire_info["reason"],
                "executor": report.lifecycle_executor,
            })
        cb("retirement_done", 85, f"retirement candidates {len(retire_info['candidates'])}")

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
        report.weights_updated = _update_weights(df=df, apply=False)
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
        from data.factor_frame import FactorFrameBuilder
        return FactorFrameBuilder().build(symbol=symbol, timeframe=timeframe, limit=n_bars)
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
        from risk.policy_service import RiskPolicyService
        verdict = RiskPolicyService.shared().evaluate(
            "register_factor",
            {
                "required_mode": "shadow",
                "candidate_count": len(expressions),
                "source": "evolution_orchestrator",
            },
        )
        if not verdict.allowed:
            logger.warning("[Evolve] shadow register blocked by risk policy: %s", verdict.reason)
            _emit_evolution_story("shadow_register_blocked", {
                "candidate_count": len(expressions),
                "risk_verdict": verdict.to_dict(),
            })
            return 0

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
                    from alpha.factor_dsl import evaluate_dsl, parse_dsl

                    parse_dsl(expression_str)
                    func = lambda df, _expr=expression_str: evaluate_dsl(_expr, df)
                except Exception as e:
                    logger.warning("[Evolve] skip invalid DSL expression for %s: %s", name, e)
                    _emit_evolution_story("shadow_register_invalid_dsl_skipped", {
                        "factor": name,
                        "reason": str(e),
                    })
                    continue
            try:
                ok = adapter.register_runtime(
                    name=name, func=func, source=SOURCE_SHADOW,
                    description=expression_str,
                )
                if ok:
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
        from config.runtime_config import shared as runtime_config
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
        expansion_frozen = bool(
            getattr(runtime_config(), "autonomy_expansion_frozen", True)
        )
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
                dir_state.rollback_count = int(state.get("rollback_count", 0) or 0)
                dir_state.evidence_hash = str(state.get("evidence_hash") or "")
                dir_state.dataset_hash = str(state.get("dataset_hash") or "")
                dir_state.evidence_end_at = str(state.get("evidence_end_at") or "")
                dir_state.stage_evidence_hash = str(state.get("stage_evidence_hash") or "")
                dir_state.fresh_evidence_bars = int(state.get("fresh_evidence_bars", 0) or 0)
                dir_state.history = [dict(event) for event in state.get("events", []) if isinstance(event, dict)]

        stage_priority = {
            "PROBATION": 0,
            "CANARY_50": 1,
            "CANARY_20": 2,
            "CANARY_5": 3,
            SHADOW: 4,
        }
        evaluation_limit = max(
            10,
            min(int(os.getenv("QUANT_CANARY_EVALUATION_LIMIT", "200") or 200), 1000),
        )
        evaluable_candidates = [
            item
            for item in candidates
            if str((saved_states.get(item[0]) or {}).get("stage") or SHADOW).upper()
            not in TERMINAL_STAGES | {ACTIVE}
        ]
        evaluable_candidates.sort(
            key=lambda item: (
                stage_priority.get(
                    str((saved_states.get(item[0]) or {}).get("stage") or SHADOW).upper(),
                    5,
                ),
                float((saved_states.get(item[0]) or {}).get("updated_at") or 0.0),
                -float(item[1]),
                item[0],
            )
        )
        selected_candidates = evaluable_candidates[:evaluation_limit]
        if len(evaluable_candidates) > len(selected_candidates):
            _emit_evolution_story("canary_evaluation_bounded", {
                "candidate_count": len(evaluable_candidates),
                "evaluation_limit": evaluation_limit,
                "deferred_count": len(evaluable_candidates) - len(selected_candidates),
                "selection_policy": "advanced_stage_then_oldest_evaluation_then_score",
            })

        for name, score, source in selected_candidates:
            # ★ P0.1: 不再 bypass canary validation. 所有因子 (无论 source
            # 是什么) 都从 canary_state 恢复 stage, 走标准 canary 管道。
            # 移除了 "discovered源且无saved_states→直接ACTIVE" 的捷径。
            ctx = _load_canary_ctx_from_log(name, score)
            try:
                result = director.check_promotion(name, ctx)
                if result == "promote":
                    if expansion_frozen:
                        # Evidence may continue to refresh while expansion is
                        # frozen, but no Canary stage may advance. Rollbacks
                        # remain available because they reduce risk.
                        logger.info(
                            "[Evolve] canary promotion blocked by autonomy expansion freeze: %s",
                            name,
                        )
                        stay.append(name)
                    else:
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
                if source == "discovered" and director.get_stage(name) != ACTIVE and name not in rollbacks:
                    rollbacks.append(name)
                    stay = [item for item in stay if item != name]
            except Exception as e:
                logger.debug("[Evolve] canary check %s failed: %s", name, e)
                stay.append(name)

        # ★ 持久化 director 状态到 DB
        new_states: dict[str, dict] = {}
        for name, _, _ in selected_candidates:
            s = director.get_state(name)
            new_states[name] = {
                "stage": s.stage,
                "oos_bars": s.oos_bars,
                "cumulative_pnl": s.cumulative_pnl,
                "promote_time": s.promote_time,
                "rollback_count": s.rollback_count,
                "evidence_hash": s.evidence_hash,
                "dataset_hash": s.dataset_hash,
                "evidence_end_at": s.evidence_end_at,
                "stage_evidence_hash": s.stage_evidence_hash,
                "fresh_evidence_bars": s.fresh_evidence_bars,
                "events": [dict(event) for event in s.history],
                "updated_at": _time.time(),
            }
        _save_canary_states(new_states)

    except Exception as e:
        logger.exception("[Evolve] canary eval failed: %s", e)

    return promotions, rollbacks, stay


def _execute_promotions(names: list[str]) -> None:
    """Deprecated compatibility hook; lifecycle writes belong to factor governance."""
    # Kept as an import-compatible evidence hook for older callers.  The
    # evolution worker may report canary candidates, but it must never write
    # RegistryAdapter lifecycle state; FactorGovernanceOrchestrator is the
    # sole lifecycle executor.
    _emit_evolution_story("canary_promotion_deferred_to_factor_governance", {
        "promotion_candidates": list(names),
        "execution_owner": "factor_governance",
        "applied": False,
    })


def _execute_rollbacks(names: list[str]) -> None:
    """Deprecated compatibility hook; lifecycle writes belong to factor governance."""
    _emit_evolution_story("canary_rollback_deferred_to_factor_governance", {
        "rollback_candidates": list(names),
        "execution_owner": "factor_governance",
        "applied": False,
    })


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
                    "n_active": perf.n_active,
                    "evidence_hash": perf.evidence_hash,
                    "dataset_hash": perf.dataset_hash,
                    "evidence_start_at": perf.evidence_start_at,
                    "evidence_end_at": perf.evidence_end_at,
                    "new_evidence_bars": perf.new_evidence_bars,
                },
            )
    except Exception as e:
        logger.debug("[Evolve] shadow canary_ctx(%s) skipped: %s", name, e)

    # ── 次选: decision_log close 记录 ──
    try:
        conn = _state_conn(read_only=True)
        rows = conn.execute(
            "SELECT meta FROM decision_log WHERE decision_type='close' AND strategy=%s",
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

    logger.warning("[Evolve] canary_ctx(%s): no real shadow perf data; staying in current stage", name)
    return CanaryEvalContext(
        oos_bars=0,
        oos_pnl=0.0,
        additional_metrics={"source": "missing_shadow_perf"},
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
    """Deprecated compatibility hook; scheduled evolution no longer calls it."""
    try:
        from alpha.registry_adapter import RegistryAdapter
        adapter = RegistryAdapter.shared()
        return adapter.retire(name, reason)
    except Exception as e:
        logger.debug("[Evolve] retire %s failed: %s", name, e)
        return False


def _collect_learning_suggestions(max_age_days: int = 30) -> tuple[dict[str, dict], dict[str, dict]]:
    """Collect rule-learning suggestions from PostgreSQL state store.

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
        if _CANARY_DB is not None:
            conn = connect_sqlite(_CANARY_DB, read_only=True)
            conn.row_factory = __import__("sqlite3").Row
            cols = {str(row[1]) for row in conn.execute("PRAGMA table_info(policy_suggestion)").fetchall()}
            reason_expr = "reason" if "reason" in cols else "'' AS reason"
            evidence_expr = "evidence_json" if "evidence_json" in cols else "'{}' AS evidence_json"
            review_expr = "review_note" if "review_note" in cols else "'' AS review_note"
            rows = conn.execute(
                f"""
                SELECT suggestion_id, scope_key, action, confidence, status,
                       {reason_expr}, {evidence_expr}, {review_expr}, created_at
                FROM policy_suggestion
                WHERE scope_type='factor' AND created_at>=?
                  AND status IN ('proposed', 'pending_review', 'approved', 'auto_approved', 'applied')
                ORDER BY created_at DESC
                """,
                (cutoff,),
            ).fetchall()
        else:
            conn = _state_conn(read_only=True)
            from backend.core.db import state_table_columns

            cols = set(state_table_columns(conn, "policy_suggestion"))
            reason_expr = "reason" if "reason" in cols else "'' AS reason"
            evidence_expr = "evidence_json" if "evidence_json" in cols else "'{}' AS evidence_json"
            review_expr = "review_note" if "review_note" in cols else "'' AS review_note"
            rows = conn.execute(
                f"""
                SELECT suggestion_id, scope_key, action, confidence, status,
                       {reason_expr}, {evidence_expr}, {review_expr}, created_at
                FROM policy_suggestion
                WHERE scope_type='factor' AND created_at>=%s
                  AND status IN ('proposed', 'pending_review', 'approved', 'auto_approved', 'applied')
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
            from backend.services.policy_suggestion_status import normalize_policy_suggestion_status

            status = normalize_policy_suggestion_status(dict(row))
            approved_like = status in {"auto_approved", "applied", "legacy_approved"}
            item = summary.setdefault(
                factor,
                {"proposed": 0, "approved": 0, "latest_action": action, "latest_confidence": confidence},
            )
            if approved_like:
                item["approved"] += 1
            else:
                item["proposed"] += 1
            # first row is latest because rows ordered desc
            if item["latest_action"] == action and item["latest_confidence"] == confidence:
                pass

            if not approved_like:
                continue
            if action not in {"downweight", "boost_small"}:
                continue

            source_weight = 0.0
            evidence: dict[str, Any] = {}
            try:
                raw_evidence = row["evidence_json"] or "{}"
                evidence = raw_evidence if isinstance(raw_evidence, dict) else _json.loads(raw_evidence)
                expected = evidence.get("expected_effect") or {}
                source_weight = float(expected.get("current_weight") or expected.get("suggested_target_weight") or 0.0)
                active_factor_context = evidence.get("active_factor_context") or {}
                source_weight = max(
                    source_weight,
                    float(active_factor_context.get("weight") or 0.0),
                )
            except Exception:
                source_weight = 0.0
            bridge = evidence.get("bridge") or {}
            is_demo_model_bridge = (
                str(evidence.get("source_agent") or "") == "lightgbm_shadow_models"
                and str(evidence.get("model_type") or "") == "factor_governance_lightgbm"
                and bridge.get("automatic_demo") is True
                and bridge.get("demo_nursery") is True
            )
            if is_demo_model_bridge:
                # A model suggestion is tied to the runtime factor snapshot
                # that produced it.  If pruning/quarantine disabled the
                # factor afterwards, do not keep feeding a stale suggestion
                # into the weight policy or report it as adopted.
                try:
                    from alpha.portfolio_compositor import resolve_factor_role
                    from config.runtime_config import shared as _runtime_config

                    runtime_cfg = _runtime_config()
                    factor_cfg = dict((runtime_cfg.factor_signal_config or {}).get(factor) or {})
                    current_runtime_weight = float((runtime_cfg.factor_portfolio_weights or {}).get(factor) or 0.0)
                    if (
                        factor_cfg.get("enabled") is False
                        or str(factor_cfg.get("lifecycle_status") or "").upper() in {"DEAD", "QUARANTINE"}
                        or resolve_factor_role(factor, factor_cfg) != "alpha"
                        or current_runtime_weight <= 0.0
                    ):
                        item["stale_runtime_target"] = True
                        item["stale_runtime_target_reason"] = "factor_not_active_in_runtime_score"
                        continue
                except Exception:
                    # If the runtime snapshot cannot be read, retain the
                    # fail-closed advisory behavior and do not apply it.
                    item["stale_runtime_target"] = True
                    item["stale_runtime_target_reason"] = "runtime_snapshot_unavailable"
                    continue
            bias_info = approved_biases.get(
                factor,
                {
                    "multiplier": 1.0,
                    "action": action,
                    "suggestion_ids": [],
                    "source_weight": source_weight,
                    "model_contributor": False,
                },
            )
            if source_weight > 0:
                bias_info["source_weight"] = max(float(bias_info.get("source_weight") or 0.0), source_weight)
            current = float(bias_info.get("multiplier", 1.0))
            if action == "downweight":
                current *= max(0.80, 1.0 - 0.20 * min(confidence, 1.0))
            elif action == "boost_small":
                current *= min(1.08, 1.0 + 0.08 * min(confidence, 1.0))
            bias_info["multiplier"] = min(1.10, max(0.70, current))
            bias_info["action"] = action
            bias_info["model_contributor"] = bool(
                bias_info.get("model_contributor")
                or (
                    str(evidence.get("source_agent") or "") == "lightgbm_shadow_models"
                    and bridge.get("automatic_demo") is True
                    and bridge.get("demo_nursery") is True
                )
            )
            bias_info["suggestion_ids"].append(str(row["suggestion_id"]))
            approved_biases[factor] = bias_info
    except Exception as e:
        logger.debug("[Evolve] collect learning suggestions: %s", e)
    return summary, approved_biases


def _apply_learning_biases(
    weights: dict[str, float],
    approved_biases: dict[str, dict],
    base_weights: dict[str, float] | None = None,
) -> tuple[dict[str, float], dict[str, dict]]:
    """Apply small approved learning biases on top of WeightPolicy output."""
    if not weights or not approved_biases:
        return dict(weights or {}), {}

    adjusted = dict(weights)
    base_weights = dict(base_weights or {})
    applied: dict[str, dict] = {}
    for factor, bias_info in approved_biases.items():
        if factor not in adjusted:
            if factor not in base_weights:
                source_weight = float((bias_info or {}).get("source_weight") or 0.0)
                if source_weight <= 0.0:
                    continue
                adjusted[factor] = source_weight
            else:
                adjusted[factor] = float(base_weights[factor] or 0.0)
        elif float(adjusted.get(factor) or 0.0) <= 0.0 and float((bias_info or {}).get("source_weight") or 0.0) > 0.0:
            # WeightPolicy may emit a zero for a factor that still has a
            # small live weight.  An explicitly approved model downweight is
            # a bounded relative change from that live weight; preserve that
            # baseline so DecisionPolicy and the weight service can evaluate
            # the change instead of silently dropping the model contribution.
            adjusted[factor] = float(
                base_weights.get(factor) or (bias_info or {}).get("source_weight") or 0.0
            )
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


def _apply_model_governed_downweights(
    *,
    approved_biases: dict[str, dict] | None = None,
) -> dict[str, Any]:
    """Apply approved model downweights before the broad health-score sync.

    Model governance must not depend on the much larger health-score/portfolio
    rebuild completing first.  That rebuild can be slow or empty during a
    worker restart, which used to make an approved model suggestion appear to
    be ignored.  This helper is intentionally limited to the demo bridge's
    risk-reducing factor downweight and still uses the normal mutation service.
    """
    try:
        if approved_biases is None:
            _, approved_biases = _collect_learning_suggestions()
        model_biases = {
            factor: info
            for factor, info in (approved_biases or {}).items()
            if bool((info or {}).get("model_contributor"))
            and str((info or {}).get("action") or "") == "downweight"
        }
        if not model_biases:
            return {"attempted": 0, "applied": False, "applications": {}}

        from alpha.decision_policy import DecisionPolicy
        from backend.services.factor_weight_change import FactorWeightChangeService
        from config.runtime_config import shared as _rc

        cfg = _rc()
        if str(getattr(cfg, "autonomy_mode", "") or "") != "demo_nursery":
            return {
                "attempted": 0,
                "applied": False,
                "applications": {},
                "reason": "model_bridge_demo_nursery_only",
            }
        current_weights = dict(cfg.factor_portfolio_weights)
        model_weight_service = FactorWeightChangeService()
        model_applications: dict[str, Any] = {}
        for factor, info in model_biases.items():
            old_weight = float(current_weights.get(factor) or 0.0)
            if old_weight <= 0.0:
                model_applications[factor] = {
                    "status": "skipped",
                    "reason": "current_weight_zero",
                    "suggestion_ids": list((info or {}).get("suggestion_ids") or []),
                }
                continue
            requested_multiplier = float((info or {}).get("multiplier") or 0.89)
            requested_target = old_weight * max(0.70, min(1.0, requested_multiplier))
            materiality_floor = max(0.002, old_weight * 0.05)
            target_weight = max(0.0, min(requested_target, old_weight - materiality_floor))
            result = model_weight_service.execute(
                source="demo_model_governance_downweight",
                producer="factor_governance",
                run_id=f"demo_model_weight_{int(_time.time())}",
                actor="system:evolution_orchestrator.demo_model_governance",
                reason="approved LightGBM model downweight through demo nursery",
                awe_patches={
                    factor: {
                        "weight": target_weight,
                        "reason": "approved_model_downweight",
                    }
                },
                weight_policy_weights=None,
                factor_configs=cfg.factor_signal_config,
                current_weights=current_weights,
                fast=True,
                bypass_for_risk_reduction=True,
                decision_policy=DecisionPolicy(min_weight=0.0),
                suggestion_ids_by_factor={factor: list((info or {}).get("suggestion_ids") or [])},
                evidence_by_factor={
                    factor: {
                        "model_governed_step": True,
                        "requested_multiplier": requested_multiplier,
                        "materiality_floor": materiality_floor,
                        "requested_target_weight": requested_target,
                        "target_weight": target_weight,
                        "model_bias": info,
                    }
                },
                source_agent="factor_governance",
            )
            model_applications[factor] = {
                "status": result.get("status"),
                "application_ids": result.get("applications") or {},
                "risk_verdict": result.get("risk_verdict") or {},
                "admissions": result.get("admissions") or {},
                "suggestion_ids": list((info or {}).get("suggestion_ids") or []),
            }
        applied = any(str(item.get("status") or "") == "applied" for item in model_applications.values())
        _emit_evolution_story("model_governed_applications", {
            "attempted": len(model_applications),
            "applied": applied,
            "items": model_applications,
        })
        if applied:
            # Ensure the broad sync, if it proceeds, starts from the actual
            # post-model runtime snapshot rather than the pre-application map.
            _rc()
        return {"attempted": len(model_applications), "applied": applied, "applications": model_applications}
    except Exception as exc:
        logger.warning("[Evolve] model governed downweight failed: %s", exc)
        _emit_evolution_story("model_governed_applications", {
            "attempted": 0,
            "applied": False,
            "error": f"{type(exc).__name__}: {exc}",
        })
        return {"attempted": 0, "applied": False, "applications": {}, "error": str(exc)}


def _update_weights(df: pd.DataFrame | None = None, *, apply: bool = True) -> bool:
    """计算动态权重并推入 factor_portfolio_weights (AWE 同一字段).

    从健康报告读取分数 → WeightPolicy + Shadow OOS → DecisionPolicy → RuntimeConfig.patch.
    DecisionPolicy 是唯一写路径, 解决 AWE 和 WeightPolicy 互相覆盖的问题.
    """
    try:
        # Execute the concrete model bridge independently of the broad health
        # score rebuild.  In demo mode this is the only model-originated
        # mutation and remains a risk-reducing, governed downweight.
        if apply:
            _, pre_model_biases = _collect_learning_suggestions()
            _apply_model_governed_downweights(approved_biases=pre_model_biases)
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
        current_weights = dict(cfg.factor_portfolio_weights)

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
            wp_weights, applied_biases = _apply_learning_biases(wp_weights, approved_biases, base_weights=current_weights)
            logger.info("[Evolve] applied learning biases to %d factors", len(applied_biases))
        else:
            applied_biases = {}

        # 来源 B: Shadow OOS 绩效 (从 PostgreSQL state store 读取)
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

        from backend.services.factor_weight_change import FactorWeightChangeService
        from alpha.decision_policy import DecisionPolicy

        weight_service = FactorWeightChangeService()
        model_decision_policy = (
            DecisionPolicy(min_weight=0.0)
            if any(bool((info or {}).get("model_contributor")) for info in applied_biases.values())
            else None
        )
        plan = weight_service.plan(
            awe_patches=None,
            weight_policy_weights=wp_weights,
            shadow_perfs=shadow_perfs,
            factor_configs=cfg.factor_signal_config,
            current_weights=current_weights,
            regime=regime,
            fast=False,
            decision_policy=model_decision_policy,
        )
        new_weights = dict(plan.get("proposed_weights") or {})
        if not new_weights:
            return False

        if not apply:
            _emit_evolution_story("weights_candidate", {
                "factors": len(new_weights),
                "factor_portfolio_weights": new_weights,
                "learning_biases": applied_biases,
                "applied": False,
                "reason": "scheduled_evolution_is_observation_only",
            })
            return False

        run_id = f"evolution_weight_{int(_time.time())}"
        weight_result = weight_service.execute(
            source="evolution_decision_policy_update_weight",
            producer="weight_policy_governance",
            run_id=run_id,
            actor="system:evolution_orchestrator",
            reason="explicit governed weight sync",
            awe_patches=None,
            weight_policy_weights=wp_weights,
            shadow_perfs=shadow_perfs,
            factor_configs=cfg.factor_signal_config,
            current_weights=current_weights,
            regime=regime,
            fast=False,
            decision_policy=model_decision_policy,
            suggestion_ids_by_factor={
                factor: list((info or {}).get("suggestion_ids") or [])
                for factor, info in applied_biases.items()
            },
            evidence_by_factor={
                factor: {
                    "governance": governance_result,
                    "learning_bias": info,
                }
                for factor, info in applied_biases.items()
            },
            source_agent="factor_governance",
        )
        if weight_result.get("status") != "applied":
            _emit_evolution_story("weights_blocked", {
                "status": weight_result.get("status"),
                "risk_verdict": weight_result.get("risk_verdict") or {},
                "admissions": weight_result.get("admissions") or {},
            })
            return False
        new_weights = dict(weight_result.get("proposed_weights") or {})
        _emit_evolution_story("weights_updated", {
            "factors": len(new_weights),
            "factor_portfolio_weights": new_weights,
            "learning_biases": applied_biases,
        })
        logger.info("[Evolve] weights: %d factors → factor_portfolio_weights (via governed service)",
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

