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
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.runtime.runtime_state import RuntimeState

logger = logging.getLogger(__name__)


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

    # --- Scheduler ---
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
    risk_cooldown_bars: int = 3
    risk_loss_cooldown_after_losses: int = 2
    risk_loss_cooldown_bars: int = 3
    risk_supervisor_reentry_cooldown_bars: int = 3
    risk_supervisor_reentry_block_reduce: bool = True
    risk_max_holding_bars: int = 288
    risk_block_on_disk_critical: bool = True
    risk_require_l2_depth: bool = False
    l2_collection_enabled: bool = True
    l2_snapshot_interval_sec: float = 5.0
    l2_write_batch_size: int = 1000
    l2_write_flush_interval_sec: float = 1.0
    risk_enable_nfp_skip: bool = False
    risk_enable_gvz_gate: bool = False
    risk_gvz_drop_pct: float = -2.0
    position_supervisor_template_id: str = "position_supervisor:default.v1"
    autonomy_mode: str = "demo_autonomous"
    autonomy_demo_auto_apply: bool = True
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
    factor_signal_config: dict = field(default_factory=lambda: {
        # 模式 A: zscore_tanh（连续有界因子）
        "rsi_14":         {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "均值回归"]},
        "di_spread":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "趋势"]},
        "stoch_k":        {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "动量"]},
        "adx":            {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "趋势"]},
        "atr_ratio":      {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "波动率"]},
        "ema_slope":       {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "tags": ["技术", "趋势"]},
        "supertrend_str":  {"mode": "zscore_tanh", "window": 50,  "min_samples": 30, "tags": ["技术", "趋势"]},
        "keltner_width":   {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "波动率"]},
        "obv_slope":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["量价"]},
        "vol_ma_ratio":    {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["量价"]},
        "macd_hist":       {"mode": "zscore_tanh", "window": 100, "min_samples": 50, "tags": ["技术", "动量"]},

        # 模式 B: rank_mapping（宏观/持仓/COT 因子）
        "dxy_corr_20":             {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": -1, "tags": ["宏观", "美元"]},
        "slv_gld_ratio":           {"mode": "rank_mapping", "window": 100, "min_samples": 30, "direction": 1,  "tags": ["宏观", "金银比"]},
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
        "engulfing":               {"mode": "discrete", "value_map": {"-1": -1.0, "0": 0.0, "1": 1.0},    "tags": ["形态", "反转"]},
        "pin_bar":                 {"mode": "discrete", "value_map": {"-1": -0.8, "0": 0.0, "1": 0.8},    "tags": ["形态", "反转"]},
        "inside_bar":              {"mode": "discrete", "value_map": {"0": 0.0, "1": -0.3},                "tags": ["形态", "整理"]},
        "hour_utc":                {"mode": "discrete", "value_map": "hour_weights",                       "tags": ["日历", "时段"]},
        "day_of_week":             {"mode": "discrete", "value_map": "day_weights",                       "tags": ["日历", "周内"]},
        "hours_to_fomc":           {"mode": "discrete", "value_map": "fomc_weights",                      "tags": ["事件", "FOMC"]},
        "hours_to_nfp":            {"mode": "discrete", "value_map": "nfp_weights",                       "tags": ["事件", "NFP"]},
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
        "bb_width":       0.0,   # 只做过滤器
        "macd_hist":      0.5,   # 普通因子, 正常参与组合
        "keltner_width":   0.3,
        "obv_slope":       0.5,
        "vol_ma_ratio":    0.3,
        "engulfing":       1.0,
        "pin_bar":         0.8,
        "inside_bar":      0.3,

        # 宏观因子（Macro Layer）
        "dxy_corr_20":             0.8,
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

        # 事件/日历（Macro Layer，低权重）
        "hours_to_fomc":  0.3,
        "hours_to_nfp":   0.3,
        "hour_utc":       0.1,
        "day_of_week":    0.1,
    })

    # --- 组合参数 ---
    factor_tactical_alpha: float = 0.7      # 战术层权重
    factor_signal_threshold: float = 0.3    # 开仓信号阈值
    filter_bb_enabled: bool = True

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

    # --- 5.2 Kelly 动态仓位 ---
    kelly_enabled: bool = True                   # 是否启用 Kelly 仓位
    kelly_fraction: float = 0.5                  # 半凯利 = 0.5, 四分之一 = 0.25
    kelly_max_pct: float = 0.25                  # 最大资本占比上限
    kelly_risk_per_trade_pct: float = 0.01       # 每笔风险 = equity × 1%

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
        cur = self.get().to_dict()
        cur.update(patch)
        return self.replace(RuntimeConfig.from_dict(cur))

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


def shared_holder() -> _RuntimeConfigHolder:
    global _holder
    with _holder_lock:
        if _holder is None:
            _holder = _RuntimeConfigHolder()
    return _holder


def shared() -> RuntimeConfig:
    """对外 API:拿到当前 RuntimeConfig 快照。"""
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
    return shared_holder().patch(patch_dict)


def subscribe(cb: Callable[[RuntimeConfig, int], None]) -> None:
    shared_holder().subscribe(cb)


def version() -> int:
    return shared_holder().version()


def reset_for_tests() -> None:
    """仅供测试使用。"""
    global _holder
    with _holder_lock:
        _holder = _RuntimeConfigHolder()
