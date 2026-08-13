"""RuntimeConfig — 可热更新的运行时配置层。

设计目标:
- 把所有"运行中能改"的参数集中到一个 dataclass,避免散落在多个文件。
- 提供 subscribe/publish 机制,任何对配置的修改都广播给所有订阅者。
- 与 settings.yaml 解耦:RuntimeConfig 从 yaml 加载初值,但运行中修改不动 yaml。
- 任何代码都可以通过 RuntimeConfig.shared() 拿到单例。

注意:
- 本模块不强制所有配置都走这里;只把"运行中可能需要热更"的参数纳入。
- 老代码继续读 settings.yaml 完全不受影响,新旧并行。
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from backend.runtime.runtime_state import RuntimeState

logger = logging.getLogger(__name__)

DEMO_AUTONOMY_MODES = frozenset({"demo_autonomous", "demo_nursery"})
VALID_RUNTIME_INCIDENT_MODES = frozenset(
    {"normal", "shadow_only", "no_new_risk", "only_close", "frozen"}
)
OPERATOR_BOUNDED_DEMO_CONTROL_KEYS = frozenset(
    {
        "governance_expansion_paused",
        "risk_cvar_threshold_pct",
        "runtime_incident_mode",
    }
)

# These are the stable, code-owned directional primitives that may be seeded
# directly in the bounded Demo account.  They are deliberately explicit: a
# generic builtin or discovered factor must still use the normal lifecycle and
# V16 promotion path.
CLASSIC_DIRECTIONAL_FACTOR_IDS = (
    "di_spread",
    "ema_slope",
    "supertrend_str",
    "macd_hist",
    "stoch_k",
    "rsi_14",
)
CLASSIC_DIRECTIONAL_FACTOR_WEIGHTS = {
    "di_spread": 1.75,
    "ema_slope": 0.5,
    "supertrend_str": 0.8,
    "macd_hist": 0.5,
    "stoch_k": 1.0,
    "rsi_14": 1.0,
}


def resolve_bounded_demo_mode(cfg: Any, broker_cfg: Any) -> bool:
    """Purely resolve bounded Demo semantics from caller-owned snapshots."""

    mode = str(getattr(cfg, "autonomy_mode", "") or "manual").strip().lower()
    return mode in DEMO_AUTONOMY_MODES and bool(getattr(broker_cfg, "is_demo", False))


def bounded_demo_mode_active(cfg: Any | None = None) -> bool:
    """Return whether bounded Demo execution semantics are actually active.

    The autonomy-mode label alone never relaxes execution risk.  The broker
    connection must independently resolve to the cTrader Demo environment;
    missing or invalid broker configuration remains fail-closed.
    """

    # This helper is used by readiness and diagnostic paths.  Reading the
    # current in-process snapshot must not refresh the durable overlay or
    # activate a safety latch.  Authoritative refresh remains owned by the
    # backend RuntimeConfig reconciler.
    if cfg is None:
        holder = shared_holder()
        if holder.version() <= 0:
            return False
        current = holder.get()
    else:
        current = cfg
    try:
        from execution.broker_config import shared_broker_connection_config

        broker_cfg = shared_broker_connection_config()
    except Exception:
        return False
    return resolve_bounded_demo_mode(current, broker_cfg)


def effective_factor_governance_cron(cfg: Any | None = None) -> str:
    """Resolve the canonical heavy governance owner schedule.

    Health, V16 delegation and factor governance now run in one learning
    worker job.  The historical 15-minute factor-only defaults are retained
    as compatibility inputs but must resolve to the 30-minute combined owner
    cadence; otherwise the factor-only job can starve its own health/V16
    prerequisite behind the shared advisory lock.
    """

    current = cfg if cfg is not None else shared()
    configured = str(
        getattr(current, "factor_governance_cron", "*/15 * * * *")
        or "*/15 * * * *"
    ).strip()
    if configured in {
        "*/15 * * * *",
        "15,30,45 * * * *",
    }:
        return "23,53 * * * *"
    return configured


def operator_bounded_demo_control_exempt(
    *,
    actor: str,
    patch: Any,
    cfg: Any,
) -> bool:
    """Allow explicit operator control changes in the bounded Demo sandbox.

    This exemption is deliberately narrow: it does not apply to autonomous
    actors, live accounts, trading parameters other than the canonical CVaR
    admission limit, or static release flags.
    """

    payload = dict(patch or {})
    keys = set(payload)
    if not (
        str(actor or "").startswith("operator:")
        and keys
        and keys <= OPERATOR_BOUNDED_DEMO_CONTROL_KEYS
        and bounded_demo_mode_active(cfg)
    ):
        return False
    if keys == {"risk_cvar_threshold_pct"}:
        value = payload["risk_cvar_threshold_pct"]
        if isinstance(value, bool):
            return False
        try:
            cvar_limit = float(value)
            var_limit = float(getattr(cfg, "risk_var_threshold_pct", 0.0))
        except (TypeError, ValueError):
            return False
        return 0.0 < cvar_limit <= var_limit
    return "risk_cvar_threshold_pct" not in keys


def operator_classic_builtin_factor_activation_exempt(
    *,
    actor: str,
    patch: Any,
    cfg: Any,
) -> bool:
    """Allow the explicit classic-factor seed only inside bounded Demo.

    Lifecycle services still write the durable factor state and audit record.
    This exemption only prevents the generic V16 expansion claim from
    blocking a user-requested seed of the six code-owned classic direction
    factors in the Demo account.
    """

    if not (
        str(actor or "").startswith("operator:")
        and bounded_demo_mode_active(cfg)
    ):
        return False
    payload = dict(patch or {})
    allowed_keys = {"factor_signal_config", "factor_portfolio_weights"}
    if not payload or not set(payload) <= allowed_keys:
        return False
    signal_patch = payload.get("factor_signal_config")
    if not isinstance(signal_patch, dict) or not signal_patch:
        return False
    allowed_names = set(CLASSIC_DIRECTIONAL_FACTOR_IDS)
    names = set(signal_patch)
    if not names or not names <= allowed_names:
        return False
    for entry in signal_patch.values():
        if not isinstance(entry, dict):
            return False
        if (
            str(entry.get("source") or "").lower() != "builtin"
            or entry.get("direct_activation") is not True
        ):
            return False
    weight_patch = payload.get("factor_portfolio_weights")
    if weight_patch is not None:
        if not isinstance(weight_patch, dict) or set(weight_patch) != names:
            return False
        for value in weight_patch.values():
            try:
                if float(value) <= 0.0:
                    return False
            except (TypeError, ValueError):
                return False
    return True


def autonomy_expansion_freeze_applies(cfg: Any | None = None) -> bool:
    """Return whether the expansion freeze is effective for this runtime.

    Demo accounts are the bounded exploration environment: expansionary
    governance remains active there even when the global flag is kept set as a
    fail-closed default for non-demo/live authority surfaces.
    """
    current = cfg if cfg is not None else shared()
    if governance_expansion_is_paused(current):
        return True
    bounded_demo = bounded_demo_mode_active(current)
    return bool(getattr(current, "autonomy_expansion_frozen", True)) and not bounded_demo


def governance_expansion_is_paused(cfg: Any | None = None) -> bool:
    """Return the all-mode operator kill-switch state.

    Unlike ``autonomy_expansion_frozen``, this switch never grants a demo-mode
    exemption.  It blocks expansionary governance in every mode while leaving
    observation, research, rollback and risk-tightening paths available.
    """
    current = cfg if cfg is not None else shared()
    return bool(getattr(current, "governance_expansion_paused", False))


@dataclass
class RuntimeConfig:
    """运行时配置 dataclass。

    所有字段都有默认值,可以零参数构造。
    """

    # --- 影子因子 ---
    shadow_vote_weight: float = 0.15  # 多因子策略里影子因子投票权重(2026-06-03 校准中位)
    shadow_top_k: int = 3
    shadow_recompute_every: int = 8
    shadow_rank_window: int = 50
    shadow_min_samples: int = 30
    shadow_top_pct: float = 0.7
    shadow_bottom_pct: float = 0.3

    # --- 因子健康 ---
    factor_health_healthy_threshold: float = 70.0
    factor_health_watch_threshold: float = 40.0
    factor_health_min_n_obs: int = 100
    factor_health_ic_active_threshold: float = 0.02
    factor_health_decay_window_q: int = 4  # q1/q4 quartile 分割

    # --- 因子发现 / GP ---
    gp_pop_size: int = 50
    gp_n_generations: int = 20
    gp_elite_frac: float = 0.10
    gp_tournament_k: int = 3
    gp_mut_prob_subtree: float = 0.10
    gp_mut_prob_const: float = 0.05
    gp_max_runtime_sec: float = 600.0
    discover_n_bars: int = 8000
    discover_top_k: int = 10
    discover_score_threshold: float = 50.0
    discover_n_warmstart: int = 20  # 从 elite_archive 抽多少注入初始种群

    # --- 退役 ---
    retire_decaying_days: int = 7  # 连续 DECAYING N 天才进 grace
    retire_grace_hours_severe: int = 24  # 健康分 < 30
    retire_grace_hours_mild: int = 72  # 健康分 30-40
    retire_severe_threshold: float = 30.0

    # --- Canary ---
    canary_min_oos_bars: int = 80  # 每个 stage 最少 OOS bar
    canary_min_oos_pnl: float = 0.0
    canary_min_unreliable_count: int = 0

    # --- 漂移 ---
    drift_n_bars: int = 5000
    drift_pop: int = 50
    drift_gen: int = 30
    drift_research_cooldown_sec: int = 3600

    # --- 重训 ---
    retrain_every_n_trades: int = 200
    retrain_min_trades_before_first: int = 100
    retrain_timeout_sec: int = 300

    # --- Scheduler (deprecated compatibility fields; runtime schedulers own cadence) ---
    scheduler_auto_discover_cron: str = "0 1 * * *"
    scheduler_promote_cron: str = "30 1 * * *"
    scheduler_drift_research_interval_min: int = 15
    scheduler_dryrun_cron: str = "0 2 * * *"
    scheduler_timezone: str = "UTC"

    # --- 数据自给 ---
    sync_interval_sec: int = 300
    sync_recovery_max_attempts: int = 3

    # --- 策略控制 ---
    ctrader_send_orders: bool = False  # cTrader 是唯一执行通道, 默认保持 dry-run

    # --- 风控/执行参数 (原 strategy_* 前缀, 现被 Factor Takeover v4 管道使用) ---
    risk_sl_atr: float = 1.5
    risk_tp_atr: float = 2.5
    risk_max_drawdown_pct: float = 16.0
    risk_max_consecutive_losses: int = 8
    risk_max_daily_loss_pct: float = 10.0
    risk_max_daily_trades: int = 30
    risk_data_lag_max_seconds: float = 3600.0
    risk_circuit_breaker_bypass: bool = False
    risk_cooldown_bars: int = 3
    risk_loss_cooldown_after_losses: int = 2
    risk_loss_cooldown_bars: int = 3
    risk_supervisor_reentry_cooldown_bars: int = 3
    risk_supervisor_reentry_block_reduce: bool = True
    risk_max_holding_bars: int = 288
    risk_block_on_disk_critical: bool = True
    risk_enable_nfp_skip: bool = False
    risk_enable_gvz_gate: bool = False
    risk_gvz_drop_pct: float = -2.0
    position_supervisor_template_id: str = "position_supervisor:default.v1"
    autonomy_mode: str = "demo_autonomous"
    autonomy_demo_auto_apply: bool = True
    live_autonomy_unlocked: bool = False
    live_autonomy_unlock_id: str = ""
    demo_learning_max_daily_trades: int = 30
    # Effective only outside demo_nursery/demo_autonomous. Demo keeps governed
    # exploration active while RiskPolicy/V16/effect rollback remain mandatory.
    autonomy_expansion_frozen: bool = True
    # Operator-owned, all-mode expansion kill switch.  Autonomous services may
    # observe it but must never clear or rewrite it through their overlays.
    governance_expansion_paused: bool = False
    # Scoped demo-only envelope for bounded model decision influence.  This is
    # deliberately separate from ``autonomy_expansion_frozen`` so an operator
    # can canary one validated model without thawing unrelated governance.
    demo_model_influence_enabled: bool = False
    model_influence_config: Dict[str, Any] = field(default_factory=dict)
    supervisor_counterfactual_governance_horizon_minutes: int = 60
    supervisor_counterfactual_full_horizon_minutes: int = 120
    supervisor_canary_mature_trade_count: int = 50
    learning_effect_inconclusive_after_days: int = 7
    nursery_exploration_per_reason_daily_limit: int = 5
    nursery_exploration_global_daily_limit: int = 15
    nursery_exploration_setup_daily_limit: int = 1
    nursery_exploration_reservation_ttl_seconds: int = 300
    supervisor_min_stop_distance_points: float = 0.20
    supervisor_stop_safety_buffer_ratio: float = 0.00008
    supervisor_min_tighten_delta_points: float = 0.01
    supervisor_quote_max_age_seconds: float = 10.0
    entry_edge_uncertainty_atr_ratio: float = 0.10
    runtime_incident_mode: str = "normal"  # normal | shadow_only | no_new_risk | only_close | frozen
    # 别名兼容 (旧代码仍读 strategy_sl_atr 等, 这里存一份等同值供 RuntimeConfig.patch)
    strategy_sl_atr: float = 1.5
    strategy_tp_atr: float = 2.5
    strategy_cooldown_bars: int = 3

    # --- 评估 ---
    evaluation_embargo_bars: int = 288  # ~3 天 M15
    evaluation_purge_bars: int = 288
    evaluation_bootstrap_n: int = 1000
    evaluation_bootstrap_alpha: float = 0.05
    evaluation_causal_ortho_decay_threshold: float = 0.5

    # --- 可观测 ---
    observability_evolution_story_path: str = "data/charts/evolution_story.jsonl"
    observability_metrics_enabled: bool = True

    # ══════════════════════════════════════════════════════
    # FACTOR TAKEOVER V4 配置段
    # ══════════════════════════════════════════════════════

    factor_dry_run: bool = False             # True=只算信号不下单

    # --- 时间框架 ---
    timeframe: str = "M5"                    # 主交易周期

    # --- Signal Normalizer 配置 ---
    # 经典内置因子是 live alpha 的稳定基线：它们保留健康观测和治理审计，
    # 但不能因为尚未落一条 factor_health 行就从方向组合中全部消失。
    # ``health_gate_exempt`` 只豁免“缺健康证据”的 admission gate，不能
    # 绕过显式 disabled/terminal lifecycle，也不授予任何配置写权限。
    factor_signal_config: dict = field(default_factory=lambda: {
        # 模式 A: zscore_tanh（连续有界因子）
        "rsi_14":         {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "alpha", "direction": -1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "oscillator", "tags": ["技术", "均值回归"]},
        "di_spread":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "trend", "tags": ["技术", "趋势"]},
        "stoch_k":        {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "oscillator", "tags": ["技术", "动量"]},
        "adx":            {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["技术", "趋势强度"]},
        "atr_ratio":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["技术", "波动率"]},
        "ema_slope":       {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "trend", "tags": ["技术", "趋势"]},
        "supertrend_str":  {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "trend", "tags": ["技术", "趋势"]},
        "keltner_width":   {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["技术", "波动率"]},
        "obv_slope":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "source": "builtin", "redundancy_group": "volume_direction", "tags": ["量价"]},
        # Volume / volume-MA 只有强弱，没有天然多空方向；保留为 context，
        # 避免“放量”被组合器误读为看多、“缩量”误读为看空。
        "vol_ma_ratio":    {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["量价", "成交量强度"]},
        "macd_hist":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "direct_activation": True, "source": "builtin", "redundancy_group": "momentum", "tags": ["技术", "动量"]},

        # 结构/高周期候选：先 shadow 观察，健康后由治理循环自动启用。
        "htf_trend_alignment": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                 "enabled": True, "lifecycle_status": "SHADOW",
                                 "autonomous_activation": True, "role": "alpha", "source": "builtin",
                                 "tags": ["技术", "高周期", "趋势"]},
        "donchian_breakout_20": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                  "enabled": True, "lifecycle_status": "SHADOW",
                                  "autonomous_activation": True, "role": "alpha", "source": "builtin",
                                  "tags": ["技术", "突破", "结构"]},
        "range_expansion_20": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                "enabled": True, "lifecycle_status": "SHADOW",
                                "autonomous_activation": True, "role": "context", "source": "builtin",
                                "tags": ["技术", "波动率", "结构"]},
        "price_location_50": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                               "enabled": True, "lifecycle_status": "SHADOW",
                               "autonomous_activation": True, "role": "alpha", "source": "builtin",
                               "tags": ["技术", "价格位置", "结构"]},
        "candle_body_pressure": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                  "enabled": True, "lifecycle_status": "SHADOW",
                                  "autonomous_activation": True, "role": "alpha", "source": "builtin",
                                  "tags": ["K线", "实体", "方向"]},
        "wick_rejection": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                            "enabled": True, "lifecycle_status": "SHADOW",
                            "autonomous_activation": True, "role": "alpha", "source": "builtin",
                            "tags": ["K线", "影线", "反转"]},
        "morning_evening_star": {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},
                                  "enabled": True, "lifecycle_status": "SHADOW",
                                  "autonomous_activation": True, "role": "alpha", "source": "builtin",
                                  "tags": ["K线", "三K", "反转"]},
        "harami": {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},
                    "enabled": True, "lifecycle_status": "SHADOW",
                    "autonomous_activation": True, "role": "alpha", "source": "builtin",
                    "tags": ["K线", "两K", "反转"]},
        "fib_retracement_position": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                      "enabled": True, "lifecycle_status": "SHADOW",
                                      "autonomous_activation": True, "role": "context", "source": "builtin",
                                      "tags": ["Fibonacci", "波段", "价格位置"]},
        "fib_level_proximity": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                 "enabled": True, "lifecycle_status": "SHADOW",
                                 "autonomous_activation": True, "role": "context", "source": "builtin",
                                 "tags": ["Fibonacci", "支撑阻力", "价格位置"]},
        "fib_rejection_confirmation": {"mode": "zscore_tanh", "window": 100, "min_samples": 100,
                                        "enabled": True, "lifecycle_status": "SHADOW",
                                        "autonomous_activation": True, "role": "alpha", "source": "builtin",
                                        "tags": ["Fibonacci", "反弹确认", "反转"]},

        # 模式 B: rank_mapping（宏观/持仓/COT 因子）
        "dxy_corr_20":             {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1, "role": "context", "tags": ["宏观", "美元", "相关性"]},
        "slv_gld_ratio":           {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "金银比"]},
        "real_yield_chg":          {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "利率"]},
        "real_yield_pct_rank":     {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "利率"]},
        "gld_tonnes_chg_5d":       {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
        "gld_tonnes_chg_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
        "gld_tonnes_pct_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金"]},
        "gld_tonnes_zscore_60d":   {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "黄金", "极值"]},
        "slv_tonnes_chg_20d":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["持仓", "白银"]},
        "silver_gold_holdings_ratio": {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1, "tags": ["持仓", "金银比"]},
        "cb_total_chg_3m":         {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
        "cb_china_chg_3m":         {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
        "cb_russia_chg_3m":        {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金"]},
        "cb_china_3m_zscore":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["央行", "购金", "极值"]},
        "cot_mm_net":              {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
        "cot_mm_net_pct_oi":       {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
        "cot_mm_net_chg_4w":      {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机"]},
        "cot_mm_net_zscore_52w":   {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["COT", "投机", "极值"]},
        "cot_pm_net":              {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["COT", "商业"]},
        "cot_extreme_signal":      {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0}, "tags": ["COT", "反转", "综合"]},

        # 模式 C: discrete（形态/事件因子）
        "engulfing":               {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0}, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "source": "builtin", "redundancy_group": "price_action", "tags": ["形态", "反转"]},
        "pin_bar":                 {"mode": "discrete", "value_map": {"-1": -0.8, "0": 0.0, "1": 0.8}, "role": "alpha", "direction": 1, "enabled": True, "lifecycle_status": "ACTIVE", "health_gate_exempt": True, "source": "builtin", "redundancy_group": "price_action", "tags": ["形态", "反转"]},
        # 当前实现只表达“inside bar 出现”，并没有突破方向；不作为 alpha。
        "inside_bar":              {"mode": "discrete", "value_map": {"0": 0.0, "1": -0.3}, "role": "context", "tags": ["形态", "整理"]},
        "bb_width":                {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "role": "context", "tags": ["技术", "波动率", "布林带"]},
        "hour_utc":                {"mode": "discrete", "value_map": "hour_weights", "role": "context",   "tags": ["日历", "时段"]},
        "day_of_week":             {"mode": "discrete", "value_map": "day_weights", "role": "context",    "tags": ["日历", "周内"]},
        "hours_to_fomc":           {"mode": "discrete", "value_map": "fomc_weights", "role": "gate",      "tags": ["事件", "FOMC"]},
        "hours_to_nfp":            {"mode": "discrete", "value_map": "nfp_weights", "role": "gate",       "tags": ["事件", "NFP"]},
    })

    # --- Portfolio Compositor 权重配置 ---
    factor_portfolio_weights: dict = field(default_factory=lambda: {
        # 技术因子（Tactical Layer）
        "di_spread":      1.75,
        "rsi_14":         1.0,
        "stoch_k":        1.0,
        "adx":            0.5,
        "ema_slope":       0.5,
        "supertrend_str":  0.8,
        "atr_ratio":      0.5,
        "bb_width":       0.0,   # context only; not a BB gate and not directional alpha
        "macd_hist":      0.5,   # 普通因子, 正常参与组合
        "keltner_width":   0.3,
        "obv_slope":       0.5,
        "vol_ma_ratio":    0.3,
        "engulfing":       1.0,
        "pin_bar":         0.8,
        "inside_bar":      0.0,   # context-only: 当前值没有可靠的多空方向
        # 新增结构因子初始权重为 0；通过后验健康门槛后由治理服务设置小权重。
        "htf_trend_alignment": 0.0,
        "donchian_breakout_20": 0.0,
        "range_expansion_20": 0.0,
        "price_location_50": 0.0,
        "candle_body_pressure": 0.0,
        "wick_rejection": 0.0,
        "morning_evening_star": 0.0,
        "harami": 0.0,
        "fib_retracement_position": 0.0,
        "fib_level_proximity": 0.0,
        "fib_rejection_confirmation": 0.0,

        # 宏观因子（Macro Layer）
        "dxy_corr_20":             0.0,   # correlation regime only; not directional DXY alpha
        "slv_gld_ratio":            0.5,
        "real_yield_chg":           0.5,
        "real_yield_pct_rank":      0.5,
        "gld_tonnes_chg_5d":        0.7,
        "gld_tonnes_chg_20d":       0.7,
        "gld_tonnes_pct_20d":       0.5,
        "gld_tonnes_zscore_60d":    1.0,
        "slv_tonnes_chg_20d":       0.4,
        "silver_gold_holdings_ratio": 0.3,
        "cb_total_chg_3m":          0.8,
        "cb_china_chg_3m":          0.6,
        "cb_russia_chg_3m":         0.4,
        "cb_china_3m_zscore":       0.5,
        "cot_mm_net":               0.8,
        "cot_mm_net_pct_oi":        0.6,
        "cot_mm_net_chg_4w":        0.6,
        "cot_mm_net_zscore_52w":    1.2,
        "cot_pm_net":               0.4,
        "cot_extreme_signal":       1.5,

        # 事件/日历（context/gate, 保留权重键用于兼容旧 API/AWE 读表）
        "hours_to_fomc":  0.3,
        "hours_to_nfp":   0.3,
        "hour_utc":       0.1,
        "day_of_week":    0.1,
    })

    # --- 组合参数 ---
    factor_tactical_alpha: float = 0.7      # 战术层权重
    factor_signal_threshold: float = 0.3    # 开仓信号阈值
    live_factor_warmup_bars: int = 150      # live 启动时喂给因子/normalizer 的最近 K 线数
    # Retained only for persisted runtime-config hash compatibility.  The
    # live factor and gate paths must not read this deprecated field.
    filter_bb_enabled: bool = False

    # --- 金字塔/仓位控制 ---
    pyramid_enabled: bool = True             # 金字塔加仓规则: 新信号需强于已有持仓才加仓
    max_position_volume: float = 0.5           # 单品种最大持仓量
    max_position_api_volume: float = 1000.0    # 单品种最大持仓量(API volume, live 口径)
    max_position_count: int = 3              # 单品种最大同时持仓数

    # --- Adaptive Weight Engine (Phase 5 占位) ---
    awe_sensitivity: float = 0.5
    awe_anchor_pull: float = 0.15
    awe_max_single_change: float = 0.15
    awe_weight_min: float = 0.1
    awe_weight_max: float = 3.0
    awe_min_trades: int = 50                # 最少交易笔数才调权重
    awe_adapt_interval: int = 50
    awe_ic_floor: float = 0.02
    awe_health_floor: float = 40.0
    awe_disable_min_trades: int = 20
    awe_causal_threshold: float = -0.3
    awe_dsr_p_threshold: float = 0.95
    awe_resurrect_health_threshold: float = 60.0
    awe_resurrect_dsr_p: float = 0.05
    awe_resurrect_cooldown_days: int = 7
    awe_max_type_weight_pct: float = 0.40
    # Scheduled-open sessions can temporarily have no fresh quote during the
    # broker's maintenance break.  Health may tolerate that evidence-bound
    # state for this long, but must fail critical after the grace expires.
    market_open_pending_quote_grace_seconds: float = 4500.0

    # --- Factor Governance V3: 全自主自治 ---
    factor_governance_enabled: bool = True
    factor_governance_cron: str = "*/15 * * * *"
    factor_governance_shadow_min_oos_bars: int = 100
    factor_governance_shadow_min_valid: int = 80
    factor_governance_shadow_min_hit_rate: float = 0.50
    factor_governance_shadow_max_drawdown: float = 0.05
    # Zero in the code default is deliberate: autonomous activation requires
    # an explicit deployment value (settings.yaml currently supplies one).
    factor_governance_new_factor_weight: float = 0.0
    factor_governance_max_promotions_per_cycle: int = 1
    factor_governance_max_disables_per_cycle: int = 1
    factor_governance_max_retires_per_cycle: int = 1
    # 内置结构候选的 shadow -> live 自动启用边界。
    factor_governance_builtin_activation_enabled: bool = True
    factor_governance_builtin_activation_min_health_score: float = 70.0
    factor_governance_builtin_activation_min_n_obs: int = 500
    factor_governance_builtin_activation_max_weakness: float = 0.65
    factor_governance_builtin_activation_weight: float = 0.0
    factor_governance_max_builtin_activations_per_cycle: int = 1
    # 被自治治理隔离的因子自动恢复：只针对 QUARANTINE，不恢复 RETIRED/DEAD。
    factor_governance_auto_restore_enabled: bool = True
    factor_governance_restore_cooldown_days: int = 7
    factor_governance_restore_health_threshold: float = 60.0
    factor_governance_restore_max_weakness: float = 0.65
    factor_governance_max_restores_per_cycle: int = 1
    factor_governance_model_min_samples: int = 3
    # Model-derived factor mutation requires real per-factor coverage in
    # addition to the global artifact promotion gate. Sparse factors remain
    # shadow/advisory only.
    factor_governance_model_min_factor_samples: int = 20
    factor_governance_model_weakness_threshold: float = 0.65
    factor_governance_model_disable_threshold: float = 0.85
    factor_governance_rollback_min_trades: int = 3
    factor_governance_rollback_delta_threshold: float = -0.15
    factor_redundancy_min_samples: int = 200
    factor_redundancy_corr_threshold: float = 0.85
    factor_redundancy_max_group_weight: float = 0.35
    context_policy_enabled: bool = True

    # --- Phase 4: 执行算法配置 ---
    algo_enabled: bool = False               # 是否启用算法执行 (>0.05 API volume 自动拆单)
    algo_threshold_volume: float = 0.05        # 超过此 API volume 阈值启用 algo (0=永不用)
    algo_default: str = "TWAP"               # 默认算法: TWAP | VWAP | POV | IS
    algo_duration_minutes: int = 30          # 默认执行窗口 (分钟)
    algo_pov_participation: float = 0.10     # POV 算法的市场参与率
    algo_is_urgency: float = 0.5             # IS 算法默认 urgency

    # --- Phase 6: 多品种并行管道 ---
    enabled_symbols: list = field(default_factory=lambda: ["XAUUSD+"])

    multi_symbol_config: dict = field(default_factory=lambda: {
        "XAUUSD+": {
            "tactical_alpha": 0.7,
            "signal_threshold": 0.35,
            "max_position_volume": 0.5,
            "max_position_api_volume": 1000.0,
            "contract_size": 100,
        },
        "EURUSD": {
            "tactical_alpha": 0.6,
            "signal_threshold": 0.35,
            "max_position_volume": 0.5,
            "max_position_api_volume": 1000.0,
            "contract_size": 100000,
        },
    })

    # --- Phase 6: 跨品种协方差 — 每 N 根 bar 重算一次 ---
    cross_asset_covariance_enabled: bool = True
    cross_asset_covariance_window: int = 60      # 滚动窗口 (bar 数)
    cross_asset_update_interval: int = 60        # 更新间隔 (bar 数)

    # ══════════════════════════════════════════════════════
    # PHASE 5: 风控升级配置段
    # ══════════════════════════════════════════════════════

    # --- 5.1 VaR/CVaR 引擎 ---
    var_enabled: bool = True                     # 是否启用 VaR 检查
    var_window: int = 500                        # 滚动窗口
    var_alpha: float = 0.95                      # 置信水平
    var_method: str = "historical"               # parametric | historical | monte_carlo
    var_cvar_threshold: float = 0.02             # CVaR > 2% equity → 熔断
    risk_var_threshold_pct: float = 2.0          # VaR > 2% equity → 阻断新开仓
    risk_cvar_threshold_pct: float = 2.0         # CVaR > 2% equity → 阻断新开仓

    # --- 5.2 Kelly 动态仓位 ---
    kelly_enabled: bool = True                   # 是否启用 Kelly 仓位
    kelly_fraction: float = 0.5                  # 半凯利 = 0.5, 四分之一 = 0.25
    kelly_max_pct: float = 0.25                  # 最大资本占比上限
    kelly_risk_per_trade_pct: float = 0.05       # 动态 Kelly 单笔/探索实际止损风险上限 5.0%
    kelly_min_closed_trades: int = 20            # 样本不足时 demo 仅最小探索，非 demo 不放大
    kelly_canary_max_api_volume: float = 100.0   # Kelly 育苗期单笔 API volume 上限
    dynamic_sizing_enabled: bool = True          # 是否启用实盘阶梯式动态仓位
    dynamic_sizing_max_api_volume: float = 1000.0 # demo 动态仓位硬上限(API volume)，实际下单仍由 equity 风险预算细分
    dynamic_sizing_api_units_per_display_unit: float = 100.0  # XAUUSD: 100 API volume ~= 1 oz PnL

    # --- 5.3 压力测试 ---
    stress_test_enabled: bool = False            # 是否启用压力测试
    stress_test_max_survivable_loss_pct: float = 20.0  # 最大可承受亏损 %

    # --- 5.4 因子暴露集中度 ---
    concentration_enabled: bool = False          # 是否启用集中度监控
    concentration_max_type_pct: float = 0.40     # 单类型最大权重
    concentration_alert_type_pct: float = 0.50   # 告警阈值

    # --- 自由扩展字段(放最末,允许运行时塞入未知 key) ---
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "RuntimeConfig":
        if not isinstance(d, dict):
            return cls()
        known = {k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "extra"}
        extra = dict(d.get("extra", {})) if isinstance(d.get("extra"), dict) else {}
        # 任何未知 key 也丢进 extra
        for k, v in d.items():
            if k not in cls.__dataclass_fields__ and k != "extra":
                extra[k] = v
        # ``factor_governance_model_min_factor_samples`` was first persisted
        # as an extension key and later promoted to a typed field.  Rehydrate
        # the legacy location before constructing the dataclass so historical
        # snapshots keep their effective value instead of falling back to the
        # current default.  A disagreement is unsafe: the two representations
        # cannot be treated as the same runtime configuration.
        for key in RUNTIME_CONFIG_HASH_COMPAT_FIELDS:
            if key not in extra:
                continue
            legacy_value = extra.pop(key)
            if key in known and known[key] != legacy_value:
                raise ValueError(f"runtime_config_legacy_alias_conflict:{key}")
            known.setdefault(key, legacy_value)
        if "runtime_incident_mode" in known:
            incident_mode = str(known["runtime_incident_mode"] or "").strip().lower()
            if incident_mode not in VALID_RUNTIME_INCIDENT_MODES:
                raise ValueError(
                    "invalid_runtime_incident_mode: "
                    f"{incident_mode!r}; expected one of {sorted(VALID_RUNTIME_INCIDENT_MODES)}"
                )
            known["runtime_incident_mode"] = incident_mode
        known["extra"] = extra
        try:
            return cls(**known)
        except TypeError:
            logger.exception("RuntimeConfig.from_dict failed, falling back to defaults")
            return cls()

    @classmethod
    def from_yaml(cls, yaml_cfg: Dict[str, Any]) -> "RuntimeConfig":
        """从 settings.yaml 读取字段。

        优先读 `runtime` 段；像 `ctrader.send_orders` 这种历史安全闸，
        若 runtime 未显式覆盖，则沿用顶层配置避免默认值反转。
        """
        if not isinstance(yaml_cfg, dict):
            return cls()
        runtime_section = yaml_cfg.get("runtime", {})
        if not isinstance(runtime_section, dict):
            runtime_section = {}

        merged = dict(runtime_section)
        ctrader_section = yaml_cfg.get("ctrader", {})
        if "ctrader_send_orders" not in merged and isinstance(ctrader_section, dict):
            if "send_orders" in ctrader_section:
                merged["ctrader_send_orders"] = bool(ctrader_section["send_orders"])

        return cls.from_dict(merged)

    def to_yaml(self) -> Dict[str, Any]:
        """导出为可写回 settings.yaml 的 runtime 段。"""
        d = self.to_dict()
        return {"runtime": d}


# These fields were historically stored below ``extra``.  Keep one narrow,
# shared serialization rule for hashes and durable snapshots while allowing
# the typed RuntimeConfig surface to remain the current runtime API.
RUNTIME_CONFIG_HASH_COMPAT_FIELDS = frozenset(
    {"factor_governance_model_min_factor_samples"}
)


def canonical_runtime_config_payload(value: Any) -> Dict[str, Any]:
    """Return the stable config payload used by runtime-config hash bindings.

    The helper only canonicalizes the known promoted-field compatibility case;
    it does not change the live ``RuntimeConfig.to_dict()`` contract.  Callers
    that receive a conflicting top-level/legacy value fail closed instead of
    silently choosing one authority.
    """

    if isinstance(value, RuntimeConfig):
        payload = value.to_dict()
    elif isinstance(value, dict):
        payload = copy.deepcopy(value)
    elif is_dataclass(value):
        payload = asdict(value)
    elif hasattr(value, "to_dict"):
        payload = copy.deepcopy(dict(value.to_dict()))
    else:
        raise TypeError("runtime_config_payload_requires_mapping")

    extra_value = payload.get("extra")
    if extra_value is None:
        extra: Dict[str, Any] = {}
    elif isinstance(extra_value, dict):
        extra = copy.deepcopy(extra_value)
    else:
        raise ValueError("runtime_config_extra_must_be_mapping")

    for key in RUNTIME_CONFIG_HASH_COMPAT_FIELDS:
        if key not in payload:
            continue
        current_value = payload[key]
        if key in extra and extra[key] != current_value:
            raise ValueError(f"runtime_config_legacy_alias_conflict:{key}")
        extra[key] = copy.deepcopy(current_value)
        del payload[key]

    if "extra" in payload or extra:
        payload["extra"] = extra
    return payload


# ----- 单例管理 -----
class _RuntimeConfigHolder:
    """线程安全的 RuntimeConfig 单例 + 订阅者列表。"""

    def __init__(self) -> None:
        self._cfg: RuntimeConfig = RuntimeConfig()
        self._version: int = 0
        self._subscribers: List[Callable[[RuntimeConfig, int], None]] = []
        self._lock = threading.Lock()

    def get(self) -> RuntimeConfig:
        with self._lock:
            return copy.deepcopy(self._cfg)

    def version(self) -> int:
        with self._lock:
            return self._version

    def replace(self, new_cfg: RuntimeConfig) -> int:
        with self._lock:
            self._cfg = copy.deepcopy(new_cfg)
            self._version += 1
            v = self._version
            subs = list(self._subscribers)
            cfg_ref = self._cfg
        # 通知在锁外,避免回调里再次获取锁死锁
        for cb in subs:
            try:
                cb(cfg_ref, v)
            except Exception:  # noqa: BLE001
                logger.exception("RuntimeConfig subscriber raised: %r", cb)
        return v

    def patch(self, patch: Dict[str, Any]) -> int:
        """Atomically apply a typed patch to the current authoritative value.

        ``get()`` followed by ``replace()`` uses two separate critical
        sections and can therefore lose a concurrent overlay publication.  A
        narrow operator patch (for example an observability toggle) must be
        merged with the exact value held at publication time.
        """
        with self._lock:
            cur = self._cfg.to_dict()
            cur.update(copy.deepcopy(dict(patch or {})))
            self._cfg = RuntimeConfig.from_dict(cur)
            self._version += 1
            v = self._version
            subs = list(self._subscribers)
            cfg_ref = self._cfg
        for cb in subs:
            try:
                cb(cfg_ref, v)
            except Exception:  # noqa: BLE001
                logger.exception("RuntimeConfig subscriber raised: %r", cb)
        return v

    def subscribe(self, cb: Callable[[RuntimeConfig, int], None]) -> None:
        with self._lock:
            self._subscribers.append(cb)

    def unsubscribe(self, cb: Callable[[RuntimeConfig, int], None]) -> None:
        with self._lock:
            if cb in self._subscribers:
                self._subscribers.remove(cb)

    def reset(self) -> None:
        """仅供测试使用。"""
        with self._lock:
            self._cfg = RuntimeConfig()
            self._version = 0
            self._subscribers.clear()


_holder: _RuntimeConfigHolder = _RuntimeConfigHolder()
_holder_lock = threading.Lock()
_overlay_refresh_lock = threading.Lock()
_overlay_refreshing = False
_overlay_last_check_ts = 0.0
_overlay_last_hash_by_db: Dict[str, str] = {}
_overlay_base_config_by_db: Dict[str, Dict[str, Any]] = {}


def _truthy_env(name: str, default: str = "1") -> bool:
    value = str(os.getenv(name, default) or "").strip().lower()
    return value not in {"0", "false", "no", "off", "disabled"}


def _overlay_refresh_enabled() -> bool:
    if not _truthy_env("QUANT_RUNTIME_CONFIG_AUTO_OVERLAY_REFRESH", "1"):
        return False
    if os.getenv("PYTEST_CURRENT_TEST") or os.getenv("PYTEST_VERSION"):
        return str(os.getenv("QUANT_RUNTIME_CONFIG_AUTO_OVERLAY_REFRESH", "")).strip() == "1"
    return True


def _deep_merge_runtime_config(base: Dict[str, Any], overlay: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in dict(overlay or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge_runtime_config(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _overlay_db_key(db_path: str | Path | None = None) -> str:
    if db_path is None:
        try:
            from backend.core.db import STATE_DB

            db_path = STATE_DB
        except Exception:  # noqa: BLE001
            return "__default__"
    return str(Path(db_path).expanduser())


def register_overlay_base(
    config: RuntimeConfig | Dict[str, Any],
    db_path: str | Path | None = None,
    *,
    replace_existing: bool = True,
) -> None:
    """Register the immutable YAML/base layer used to rebuild an overlay.

    Persisted overlays are complete layers, not patches against whichever
    in-process value happened to be current.  Keeping the base separately is
    what makes key removal and an empty/cleared overlay propagate correctly.
    """

    payload = config.to_dict() if isinstance(config, RuntimeConfig) else dict(config or {})
    key = _overlay_db_key(db_path)
    with _overlay_refresh_lock:
        if replace_existing or key not in _overlay_base_config_by_db:
            _overlay_base_config_by_db[key] = copy.deepcopy(payload)


def overlay_base_config(db_path: str | Path | None = None) -> Dict[str, Any]:
    key = _overlay_db_key(db_path)
    with _overlay_refresh_lock:
        registered = _overlay_base_config_by_db.get(key)
        if registered is not None:
            return copy.deepcopy(registered)
    try:
        from backend.core.db import STATE_DB, is_state_db_path

        effective_db_path = db_path if db_path is not None else STATE_DB
        production_state = is_state_db_path(effective_db_path)
    except Exception:  # noqa: BLE001
        production_state = False
    if production_state:
        # A short-lived production caller may reach RuntimeConfig before the
        # backend/worker bootstrap has registered its immutable YAML layer.
        # Rebuilding a durable overlay on the holder's dataclass defaults can
        # invent a config-hash mismatch and persist a false no-new-risk latch.
        from backend.services.runtime_config_startup import load_yaml_runtime_config

        yaml_base, _yaml_payload = load_yaml_runtime_config()
        register_overlay_base(
            yaml_base,
            effective_db_path,
            replace_existing=False,
        )
        return yaml_base.to_dict()
    # Compatibility for callers that mutate an overlay before the explicit
    # startup restore path has run in isolated state (mostly tests).  Capture
    # once only; production state must always rebuild from its YAML authority.
    fallback = shared_holder().get().to_dict()
    register_overlay_base(fallback, db_path, replace_existing=False)
    return copy.deepcopy(fallback)


def config_from_overlay(
    overlay: Dict[str, Any],
    db_path: str | Path | None = None,
) -> RuntimeConfig:
    merged = _deep_merge_runtime_config(overlay_base_config(db_path), dict(overlay or {}))
    return RuntimeConfig.from_dict(merged)


def release_recovered_overlay_authority_latches(
    restored: Dict[str, Any],
) -> bool:
    """Release RuntimeConfig authority causes after a verified restore.

    Both causes are owned by the same validated overlay projection.  The
    refresh cause covers a transient poll failure, while the legacy-restore
    cause covers an earlier backend startup failure.  A later successful,
    committed/current restore is authoritative recovery evidence for both.
    """

    try:
        from backend.services.live_safety_state import (
            no_new_risk_latch_status,
            release_no_new_risk_latch_cause,
        )

        causes = (
            ("governance_authority", "runtime_config_overlay_refresh"),
            ("governance_authority", "legacy_restore:runtime_config_overlay"),
        )
        status = no_new_risk_latch_status(fail_closed=True)
        active = {
            (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
            for item in list(status.get("causes") or [])
            if isinstance(item, dict)
        }
        evidence = {
            "overlay_hash": str(restored.get("overlay_hash") or ""),
            "mutation_id": str(restored.get("mutation_id") or ""),
            "authority": dict(restored.get("authority") or {}),
        }
        remaining = set(active)
        for cause in causes:
            if cause not in remaining:
                continue
            released = release_no_new_risk_latch_cause(
                cause=cause[0],
                cause_id=cause[1],
                reason="runtime_overlay_authority_recovered",
                actor="system:runtime_config_restore",
                evidence=evidence,
            )
            remaining = {
                (str(item.get("cause") or ""), str(item.get("cause_id") or ""))
                for item in list(
                    released.get("remaining_causes")
                    or released.get("causes")
                    or []
                )
                if isinstance(item, dict)
            }
        return not any(cause in remaining for cause in causes)
    except Exception:
        logger.error(
            "RuntimeConfig overlay authority recovery latch release failed",
            exc_info=True,
        )
        return False


def refresh_from_overlay(db_path: str | Path | None = None, *, force: bool = False) -> bool:
    """Refresh the in-process RuntimeConfig from the persisted DB overlay.

    Runtime overlay writes are process-local at write time.  Other long-lived
    processes observe the same authority by polling the overlay at this shared
    config access point.  The refresh is intentionally throttled and disabled
    under pytest unless explicitly opted in.
    """

    global _overlay_refreshing, _overlay_last_check_ts
    if not force and not _overlay_refresh_enabled():
        return False
    now = time.time()
    interval = float(os.getenv("QUANT_RUNTIME_CONFIG_OVERLAY_REFRESH_INTERVAL_SEC", "5") or 5)
    if not force and now - _overlay_last_check_ts < max(0.5, interval):
        return False
    if _overlay_refreshing:
        return False
    with _overlay_refresh_lock:
        if _overlay_refreshing:
            return False
        if not force and now - _overlay_last_check_ts < max(0.5, interval):
            return False
        _overlay_refreshing = True
        _overlay_last_check_ts = now
    try:
        from backend.core.db import STATE_DB
        from backend.services.runtime_config_overlay import RuntimeConfigOverlayService

        effective_db_path = Path(db_path) if db_path is not None else Path(STATE_DB)
        service = RuntimeConfigOverlayService(effective_db_path)
        latest = service.latest()
        overlay_hash = str(latest.get("overlay_hash") or "")
        db_key = str(effective_db_path)
        if not latest.get("ok") or not overlay_hash:
            _overlay_last_hash_by_db[db_key] = overlay_hash
            return False
        if not force and _overlay_last_hash_by_db.get(db_key) == overlay_hash:
            return False
        base = RuntimeConfig.from_dict(overlay_base_config(effective_db_path))
        restored = service.restore_on_startup(base)
        shared_holder().replace(restored["config"])
        if release_recovered_overlay_authority_latches(restored):
            _overlay_last_hash_by_db[db_key] = overlay_hash
        return True
    except Exception as exc:  # noqa: BLE001
        try:
            from backend.services.runtime_config_overlay import (
                RuntimeConfigOverlayAuthorityError,
            )

            if isinstance(exc, RuntimeConfigOverlayAuthorityError):
                from backend.services.live_safety_state import (
                    activate_no_new_risk_latch,
                )

                try:
                    activate_no_new_risk_latch(
                        reason="runtime_overlay_refresh_authority_failed",
                        actor="system:runtime_config_refresh",
                        metadata={
                            "error": str(exc)[:500],
                            "authority": dict(getattr(exc, "report", {}) or {}),
                        },
                        cause="governance_authority",
                        cause_id="runtime_config_overlay_refresh",
                    )
                except Exception:
                    # The latch helper already fails closed in-process before
                    # surfacing persistence failure.
                    logger.error(
                        "RuntimeConfig overlay refresh latch persistence failed",
                        exc_info=True,
                    )
        except Exception:
            logger.error(
                "RuntimeConfig overlay authority failure handling failed",
                exc_info=True,
            )
        quarantined_config = getattr(exc, "quarantined_config", None)
        if isinstance(quarantined_config, RuntimeConfig):
            shared_holder().replace(quarantined_config)
            logger.warning(
                "RuntimeConfig overlay retained as read-only quarantine; "
                "new risk remains latched and authority will be retried"
            )
            return True
        logger.debug("RuntimeConfig overlay refresh skipped", exc_info=True)
        return False
    finally:
        with _overlay_refresh_lock:
            _overlay_refreshing = False


def shared_holder() -> _RuntimeConfigHolder:
    global _holder
    with _holder_lock:
        if _holder is None:
            _holder = _RuntimeConfigHolder()
    return _holder


def shared() -> RuntimeConfig:
    """对外 API:拿到当前 RuntimeConfig 快照。"""
    refresh_from_overlay()
    return shared_holder().get()


def replace(new_cfg: RuntimeConfig) -> int:
    """对外 API:原子替换 RuntimeConfig,广播给所有订阅者。"""
    holder = shared_holder()
    new_version = holder.replace(new_cfg)
    # 同步推到 RuntimeState(让 control API 能 version_bump)
    try:
        RuntimeState.shared().set_config(new_cfg.to_dict())
    except Exception:  # noqa: BLE001
        logger.debug("RuntimeState not initialized yet", exc_info=True)
    return new_version


def patch(patch_dict: Dict[str, Any]) -> int:
    """Atomically patch RuntimeConfig and keep RuntimeState in lockstep."""
    holder = shared_holder()
    new_version = holder.patch(patch_dict)
    try:
        RuntimeState.shared().set_config(holder.get().to_dict())
    except Exception:  # noqa: BLE001
        logger.debug("RuntimeState not initialized yet", exc_info=True)
    return new_version


def subscribe(cb: Callable[[RuntimeConfig, int], None]) -> None:
    shared_holder().subscribe(cb)


def version() -> int:
    return shared_holder().version()


def reset_for_tests() -> None:
    """仅供测试使用。"""
    global _holder, _overlay_last_check_ts
    with _holder_lock:
        _holder = _RuntimeConfigHolder()
    with _overlay_refresh_lock:
        _overlay_last_check_ts = 0.0
        _overlay_last_hash_by_db.clear()
        _overlay_base_config_by_db.clear()
