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
    include_shadow_factors: bool = False

    # --- 评估 ---
    evaluation_embargo_bars: int = 288  # ~3 天 M15
    evaluation_purge_bars: int = 288
    evaluation_bootstrap_n: int = 1000
    evaluation_bootstrap_alpha: float = 0.05
    evaluation_causal_ortho_decay_threshold: float = 0.5

    # --- 可观测 ---
    observability_evolution_story_path: str = "data/charts/evolution_story.jsonl"
    observability_metrics_enabled: bool = True

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
        """从 settings.yaml 的 'runtime' 段读取字段,缺失则用默认值。"""
        if not isinstance(yaml_cfg, dict):
            return cls()
        runtime_section = yaml_cfg.get("runtime", {})
        if not isinstance(runtime_section, dict):
            return cls()
        return cls.from_dict(runtime_section)

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
        # 通知在锁外,避免回调里再次获取锁死锁
        for cb in subs:
            try:
                cb(copy.deepcopy(self._cfg), v)
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
