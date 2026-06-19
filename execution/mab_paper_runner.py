"""execution/mab_paper_runner.py — MAB 多策略 paper runner (T1)

不破坏现有 PaperTrader 路径。新文件,主路径:
1. 接收多个 strategy 实例 + MABRouter + (可选) SelfLearningScheduler / WeightedScorer / Calibrator / Alerter
2. 主循环每根 bar:
   - 调 classify_regime 算 regime
   - router.select(regime) 选策略
   - 调该策略的 on_bar() 取 Signal
   - (可选) calibrator.calibrate(signal.confidence) 矫正
   - 把 Signal 喂给 PaperTrader-like 撮合
3. 交易 close 时回调:
   - router.update(strategy, regime, win)
   - scheduler.on_trade_close(...) [可选]
   - meta_monitor.on_observation(...) [可选]
   - alerter.send(INFO, ...) [可选]

这是把 P0/P1/P3/P8/P9 全部"实现齐"的组件接到 production 的粘合层。
"""
from __future__ import annotations

import logging
import math
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone, date
from typing import Optional

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)  # B1 fix: MABPaperRunner.__init__ line 193 用 logger.warning 之前没 import, rc=1 崩

from data.store import DataStore
from strategy.base import BaseStrategy, Signal
from strategy.mab_router import MABRouter, classify_regime, REGIMES
from execution.paper_trader import PaperTrader, PaperReport
from execution._sharpe import sharpe_ratio_log_nw  # OPT-2 (audit 2026-06-06)


# ── 报告 dataclass ─────────────────────────────────────
@dataclass
class MABPaperReport:
    """MAB 多策略 paper 报告 (扩展自 PaperReport + MAB 统计)"""
    symbol: str
    timeframe: str
    n_bars: int
    initial_balance: float
    final_balance: float
    net_pnl: float
    total_return_pct: float
    total_trades: int
    wins: int
    losses: int
    win_rate: float
    profit_factor: float
    max_drawdown_pct: float
    sharpe: float
    longest_win_streak: int
    longest_loss_streak: int
    final_position: str
    # MAB 特有
    strategy_picks: dict[str, int] = field(default_factory=dict)  # 策略名 → 选中次数
    strategy_pnl: dict[str, float] = field(default_factory=dict)   # 策略名 → 贡献 PnL
    regime_pnl: dict[str, float] = field(default_factory=dict)     # regime → 贡献 PnL
    daily_pnl: list[float] = field(default_factory=list)


class MABPaperRunner:
    """
    MAB 多策略 paper runner — production 路径

    与 PaperTrader 的区别:
    - PaperTrader 接受单一 strategy, 这里接受 strategy_dict + router
    - 这里集成了 P0/P1/P3/P8/P9 全部可选组件 (5 个 None-safe)
    - PaperReport → MABPaperReport (扩展 MAB 统计)

    ARCH-1 KNOWN ISSUE (audit 2026-06-06): 当前所有 4 个 strategy 共享 1 个 PaperEngine
    ─────────────────────────────────────────────────────────────────────────────────
    实现细节 (line 113-138, 336-341):
      - self.paper = PaperTrader(strategy=primary_strategy, ...) 包装 1 个 PaperEngine
      - 主循环每 bar router.select(chosen) 选策略, 然后:
          prev_strategy = self.paper.strategy
          self.paper.strategy = self.strategies[chosen]  # 临时切换 reference
          try:
              engine.on_bar(bar, signal)
          finally:
              self.paper.strategy = prev_strategy
      - 4 策略的 last_indicators / position / SL/TP / equity 全在 1 个 engine 上

    后果:
      1. 任何时候最多 1 个 active trade — 4 策略独立 alpha 退化为"选最优开仓"
      2. 共享 SL/TP 行为: strategy 切换不影响既有仓位的 SL/TP (用 primary 的 ATR 算)
      3. last_indicators 串味: multi_factor / trend / mean_reversion 维护的 indicator
         互相覆盖 (因为它们都读 self.paper.strategy.last_indicators)
      4. MAB router 学到"哪个 regime 用哪个策略",但实际仓位/风险都绑到 primary
      5. 拆解收益预期: 4 引擎后 PnL 不一定更好(独立 position 暴露更多 alpha),
         但能测出"4 策略真·alpha" vs "当前 primary + MAB 标签"的差

    拆解方案 (TODO, ~1-2 天工作量):
      - 4 个 strategy → 4 个 PaperEngine,每个 engine 独立 position/equity/last_indicators
      - 共享 state.daily 风控(全局熔断), 单笔风控各自
      - 主循环每 bar:
          * router.select 选 N 个候选(目前是 1 个,可以改成 2-3 个并行)
          * 对每个被选中的 strategy,调其 on_bar() 取 signal
          * 把 signal 送对应 engine.on_bar(bar, signal)
      - MAB stats / 元学习回调:从 4 个 engine 聚合 trades

    为什么这次只加护栏(不动主循环):
      - 重构期间任何 PnL 变化都难解释(MAB router 学习的是旧行为)
      - 现有 baseline (+407.51% / -9.54% 等) 是用共享 engine 测的, 不能让现有报告失效
      - 等 dedicated benchmark (verify-2 跑 fix-4 后的 PnL) 之后, 再安全拆解

    启动时打印一次性警告,提醒 caller 这架构问题:
    """

    def __init__(
        self,
        strategies: dict[str, BaseStrategy],
        router: MABRouter,
        initial_balance: float = 500.0,
        default_lots: float = 0.01,
        max_lots: float = 0.5,
        min_lots: float = 0.01,
        warmup_bars: int = 500,
        # 风控 (从 PaperTrader 同款参数)
        max_daily_loss_pct: float = 10.0,
        max_consecutive_loss: int = 5,
        max_trades_per_day: int = 20,
        single_risk_usd: float = 35.0,
        volatility_mult: float = 3.0,
        # FOOTGUN-2 fix (audit 2026-06-06): 改 None=禁用 vs 0.0=真 0% 风险
        risk_per_trade_pct: float | None = None,  # 0=固定 default_lots，>0 启用 Kelly 动态仓位
        enable_circuit: bool = True,
        scheduler=None,             # SelfLearningScheduler
        scorer=None,                # WeightedScorer
        calibrator=None,            # ProbabilityCalibrator
        meta_monitor=None,          # MetaLearnerMonitor
        factor_monitor=None,        # FactorMonitor
        alerter=None,               # monitor.alerter.Alerter
        retrain_scheduler=None,     # RetrainScheduler (T8)
        event_filter=None,          # SharedEventFilter (T13, 默认 None = 不过滤)
        event_sizing=None,          # EventSizing 事件感知仓位
    ):
        self.strategies = strategies
        self.router = router
        self.scheduler = scheduler
        self.scorer = scorer
        self.calibrator = calibrator
        self.meta_monitor = meta_monitor
        self.factor_monitor = factor_monitor
        self.alerter = alerter
        self.retrain_scheduler = retrain_scheduler
        self.event_filter = event_filter

        # 用一个"虚拟单策略"包装纸引擎: 选 multi_factor 跟其它策略共享风控参数
        # 实现上挑第一个 strategy, 让 PaperTrader 帮我们管 SL/TP/撮合/熔断
        primary_name = "multi_factor_m15" if "multi_factor_m15" in strategies else list(strategies.keys())[0]
        self.primary_name = primary_name
        primary_strategy = strategies[primary_name]

        # 复用 PaperTrader 的风控/撮合
        # enable_circuit=False → pre_trade + circuit_breaker 都关 (baseline 语义)
        # risk_per_trade_pct > 0 启用 Kelly-style 动态仓位 (按 equity × risk% / sl_distance 自动算手数)
        self.paper = PaperTrader(
            strategy=primary_strategy,
            initial_balance=initial_balance,
            default_lots=default_lots,
            max_lots=max_lots,
            warmup_bars=warmup_bars,
            max_daily_loss_pct=max_daily_loss_pct,
            max_consecutive_loss=max_consecutive_loss,
            max_trades_per_day=max_trades_per_day,
            single_risk_usd=single_risk_usd,
            volatility_mult=volatility_mult,
            risk_per_trade_pct=risk_per_trade_pct,
            enable_circuit=enable_circuit,
            event_sizing=event_sizing,
        )
        # 直接改 engine min_lots (PaperTrader 没暴露, 但 engine 有这个属性)
        self.paper.engine.min_lots = min_lots

        # 统计
        self._strategy_picks: dict[str, int] = {s: 0 for s in strategies}
        self._strategy_pnl: dict[str, float] = {s: 0.0 for s in strategies}
        self._regime_pnl: dict[str, float] = {r: 0.0 for r in REGIMES}
        self._trade_records: list[dict] = []  # 每笔 close 一条
        self._equity_curve: list[tuple[float, float]] = []
        self._bar_idx_counter: int = 0  # T8 retrain scheduler 用

        # ARCH-1 (audit 2026-06-06): 启动时一次性警告, 提醒 caller 4 策略共享 1 engine
        # 不影响功能, 但 caller 应该知道 PnL 数字已偏 MAB primary + 标签
        if len(strategies) > 1:
            logger.warning(
                f"[ARCH-1] MABRunner 共享 1 个 PaperEngine (包装 {primary_name}), "
                f"4 策略 {list(strategies.keys())} 共用 1 个 position + SL/TP + last_indicators. "
                f"当前 PnL 数字 (baseline +407.51% / -9.54%) 是 'MAB 选最优开仓' 模式, "
                f"不是 4 策略独立 alpha. 完整拆解方案见 docs/audits/refactor-1-mab-4-engines.md. "
                f"override ARCH-1_QUIET=True 可关闭本警告."
            )
        self._arch1_quiet = False  # caller 改 True 关警告

    def load_data(self, store: DataStore, symbol: str, timeframe: str,
                  start: str | None = None, end: str | None = None):
        """从 DataStore 加载 bar, 转发给 PaperTrader"""
        self.paper.load_data(store, symbol, timeframe, start=start, end=end)

    def _send_alert(self, level: str, title: str, msg: str, **state):
        if self.alerter is None:
            return
        # B12 fix: main.py 注入的 alerter 是 monitor.alerter.Alerter (字符串 API),
        # 之前 _send_alert 传 AlertLevel enum 导致 _should_send() ValueError
        # "unknown level: <AlertLevel.INFO: 'INFO'>" (enum 直接 str() 是 "AlertLevel.INFO",
        # 而 LEVEL_ORDER keys 是纯字符串 "INFO" 等). 修法: 跟 monitor.alerter 对齐传字符串.
        from monitor.alerter import DEBUG, INFO, WARNING, ERROR, CRITICAL  # noqa: F401
        # audit 2026-06-12: 同时处理 "WARN" 和 "WARNING"（alerter 内部用 "WARNING"，
        # 但 caller 可能传 "WARN"），避免 fallback 到 INFO
        level_map = {"INFO": INFO, "WARN": WARNING, "WARNING": WARNING,
                     "CRITICAL": CRITICAL, "ERROR": ERROR, "DEBUG": DEBUG}
        self.alerter.send(level_map.get(level, INFO), title, msg, **state)

    def _on_trade_close(self, trade) -> None:
        """每笔 trade close 的统一回调 — 把自学习层全接进来"""
        # trade 是 paper_engine.PaperTrade, 含 direction/pnl/strategy_hint
        # 注意: paper_engine 当前不记录 strategy 归属, 这里用 trade_records 推断
        # (见 run() 主循环里我们手动记录的 (idx, strategy_name) 配对)
        record = self._trade_records[-1] if self._trade_records else None
        if record is None:
            return
        strategy_name = record.get("chosen", "unknown")
        regime = record.get("regime", "RANGING")
        win = trade.pnl > 0

        # 1. router.update
        try:
            self.router.update(strategy_name, regime, win)
        except Exception as e:
            # 任何 self-learning 组件失败不能影响交易
            pass

        # 2. scheduler.on_trade_close
        if self.scheduler is not None:
            try:
                self.scheduler.on_trade_close(strategy_name, regime, win, trade.pnl)
            except Exception:
                pass

        # 3. meta_monitor.on_observation
        if self.meta_monitor is not None:
            try:
                # paper 路径里没概率输出, 用 win/loss 离散替代 (0/1)
                pred_prob = 1.0 if win else 0.0
                self.meta_monitor.on_observation(
                    model_name=strategy_name,
                    pred_prob=pred_prob,
                    y_true=1 if win else 0,
                    bar_ts=trade.time,
                )
            except Exception:
                pass

        # 3.5 T10: drift 检测 → 触发 retrain (severity-based)
        if self.meta_monitor is not None and self.retrain_scheduler is not None:
            try:
                statuses = self.meta_monitor.status()
                any_severe = any(s.drift_status == "SEVERE_DRIFT" for s in statuses)
                if any_severe:
                    # drift 触发, 强制走 retrain (override frequency)
                    self._last_drift_retrain_n = getattr(self, "_last_drift_retrain_n", -9999)
                    if len(self._trade_records) - self._last_drift_retrain_n >= 100:
                        self._last_drift_retrain_n = len(self._trade_records)
                        n_trades_total = sum(1 for t in self.paper.engine.trades if t.direction in (2, -2))
                        self._send_alert(
                            "CRITICAL",
                            "📊 Model drift detected, 强制 retrain",
                            f"n_models_drifted={sum(1 for s in statuses if s.drift_status=='SEVERE_DRIFT')}, "
                            f"n_trades={n_trades_total}",
                        )
                        # 强制触发, 不等 frequency
                        self.retrain_scheduler._last_retrain_n_trades = -9999
            except Exception:
                pass

        # 4. 统计
        self._strategy_pnl[strategy_name] = self._strategy_pnl.get(strategy_name, 0.0) + trade.pnl
        self._regime_pnl[regime] = self._regime_pnl.get(regime, 0.0) + trade.pnl

        # 5. retrain_scheduler 触发检查 (T8)
        if self.retrain_scheduler is not None:
            n_trades_total = sum(1 for t in self.paper.engine.trades if t.direction in (2, -2))
            event = self.retrain_scheduler.on_trade_close(
                bar_idx=self._bar_idx_counter,
                n_trades_so_far=n_trades_total,
            )
            if event is not None and event.success and self.calibrator is not None:
                # retrain 成功, 尝试从新 calibrator 加载
                try:
                    from alpha.probability_calibrator import ProbabilityCalibrator
                    new_cal = ProbabilityCalibrator.load(event.calibrator_path)
                    self.calibrator = new_cal
                    self._send_alert(
                        "INFO",
                        "🔄 Retrain 完成, calibrator 已更新",
                        f"#{len(self.retrain_scheduler.get_events())}, "
                        f"duration={event.duration_sec:.1f}s, path={event.calibrator_path}",
                    )
                except Exception as e:
                    pass  # 加载失败不影响 paper

        # 6. alerter (仅 WIN/LOSS 大额)
        if abs(trade.pnl) > 50.0:
            emoji = "🟢" if win else "🔴"
            self._send_alert(
                "INFO",
                f"{emoji} {strategy_name} 关闭 ({regime})",
                f"PnL ${trade.pnl:+.2f}, 累计策略 PnL ${self._strategy_pnl[strategy_name]:+.2f}",
            )

    def run(self) -> MABPaperReport:
        """主循环: 跟 PaperTrader 一样, 但每根 bar 调 router.select + 选 strategy 的 on_bar"""
        bars = self.paper._bars
        n = len(bars)
        if n == 0:
            raise ValueError("No bars loaded")

        warmup = self.paper.warmup_bars
        engine = self.paper.engine
        primary = self.paper.strategy

        # 初始化所有 strategy
        for s in self.strategies.values():
            s.on_init()

        # 批算 regime (per-bar classify_regime 太慢, 跟 mab_paper.py 同款 batch)
        closes_arr = np.array([b["close"] for b in bars], dtype=np.float64)
        batch_regimes: list[str | None] = [None] * n
        if n > 200:
            t0 = _time.time()
            for i in range(200, n):
                batch_regimes[i] = classify_regime(closes_arr[max(0, i - 200):i + 1])
            # print(f"[MAB] batch regime 算完 [{_time.time() - t0:.1f}s]")

        # 主循环
        last_reset = None
        from core.state import state, DailyStats  # 跟 PaperTrader 一致

        for i, bar in enumerate(bars):
            self._bar_idx_counter = i
            # 每日 reset
            bar_date = datetime.fromtimestamp(bar["time"], tz=timezone.utc).date()
            if last_reset is None:
                last_reset = bar_date
            elif bar_date != last_reset:
                peak = state.daily.peak_equity
                state.daily = DailyStats(date=bar_date, peak_equity=peak)
                if self.paper.circuit_breaker is not None:
                    self.paper.circuit_breaker.reset()
                last_reset = bar_date

            # warmup 期间
            if i < warmup or batch_regimes[i] is None:
                engine.on_bar(bar, None)
                self._equity_curve.append((bar["time"], engine.equity))
                continue

            regime = batch_regimes[i]

            # T13: 共享事件过滤器 — NFP/FOMC+CPI/GVZ 跳过 (避免 OOH 跳爆仓)
            skip, reason = (False, "")
            if self.event_filter is not None:
                skip, reason = self.event_filter.should_skip(bar["time"])

            # 1. router 选策略
            chosen = self.router.select(regime)
            if chosen is None or chosen not in self.strategies:
                engine.on_bar(bar, None)
                self._equity_curve.append((bar["time"], engine.equity))
                continue
            self._strategy_picks[chosen] = self._strategy_picks.get(chosen, 0) + 1

            # 2. 调该 strategy 取 signal (strategy 自带 skip 仍生效)
            signal = self.strategies[chosen].on_bar(bar)

            # 3. T13: 共享事件过滤 — 跳过该 bar 任何新信号
            if skip and signal is not None and signal.direction in (1, -1):
                signal = None  # event skip 强制覆盖

            # 3. calibrator 矫正 confidence + strength (T3 + REFACTOR-6)
            #
            # REFACTOR-6 (audit 2026-06-06): 让 ProbabilityCalibrator 真被消费
            # ─────────────────────────────────────────────────────────────
            # 旧实现: 校准只改 signal.confidence, 但 confidence 没进仓位决策
            #         (PaperEngine 算 lots 用 signal.strength, 跟 confidence 解耦)
            #         → 校准结果被算出来就被丢了
            # 新实现: 把校准因子 (cal/raw) 应用到 signal.strength, 让 PaperEngine
            #         按校准后 strength 算手数, 校准真影响仓位
            # 例: raw=0.7, cal=0.4 → strength *= 0.57 (历史经验: 0.7 实际只 0.4, 缩仓)
            #     raw=0.7, cal=0.85 → strength *= 1.21 (历史经验: 0.7 实际 0.85, 加仓)
            if signal is not None and self.calibrator is not None and signal.confidence is not None:
                try:
                    raw_conf = float(signal.confidence)
                    cal_conf = self.calibrator.calibrate(raw_conf)
                    signal.confidence = cal_conf
                    # 把校准因子转到 strength (PaperEngine 真消费)
                    if raw_conf > 1e-6 and cal_conf > 0:
                        cal_factor = cal_conf / raw_conf
                        cur_strength = float(getattr(signal, 'strength', 1.0) or 1.0)
                        signal.strength = cur_strength * cal_factor
                        # 记 meta 方便回溯
                        if not hasattr(signal, 'meta') or signal.meta is None:
                            signal.meta = {}
                        signal.meta['cal_factor'] = round(cal_factor, 4)
                        signal.meta['strength_raw'] = round(cur_strength, 4)
                except Exception:
                    pass

            # 4. 把 strategy 临时换到主策略上 (PaperEngine 内部用 self.paper.strategy 算 ATR 等)
            #    这是为了让 SL/TP 用被选中策略的 last_atr
            #
            # ARCH-1 KNOWN ISSUE (audit 2026-06-06): 临时切换 strategy reference 是 hack
            # ─────────────────────────────────────────────────────────────────────────
            # 真实问题: 4 策略共享 1 个 PaperEngine, 这里用 try/finally swap reference 让
            # engine 算 SL/TP 时用"当前策略的 last_atr". 副作用:
            #   1. 上一个 strategy 的 last_indicators 写到主 engine 后, 切换时丢失
            #   2. active position 持有中, strategy 切换不会调整 SL/TP (用 primary 的)
            #   3. 4 策略的 PnL 都被算在 primary 名下, MAB 统计是"按 chosen 标签分" 近似
            # 修复: 拆 4 个 PaperEngine (见 class docstring ARCH-1 段 + docs/audits/refactor-1-mab-4-engines.md)
            prev_strategy = self.paper.strategy
            self.paper.strategy = self.strategies[chosen]
            try:
                engine.on_bar(bar, signal)
            finally:
                self.paper.strategy = prev_strategy

            self._equity_curve.append((bar["time"], engine.equity))

            # 5. 检查 trade close
            #    engine.trades 每次 close 增长, 比较计数
            if len(engine.trades) > len(self._trade_records):
                # 有新 trade close, 调回调
                for j in range(len(self._trade_records), len(engine.trades)):
                    t = engine.trades[j]
                    # 只对 close 型 (direction in (2, -2)) 感兴趣
                    if t.direction in (2, -2):
                        self._trade_records.append({
                            "idx": i,
                            "chosen": chosen,
                            "regime": regime,
                        })
                        self._on_trade_close(t)

            # 6. factor_monitor.on_bar (T7, 接收当前所有 strategy 的因子值)
            if self.factor_monitor is not None:
                try:
                    factor_values = {}
                    for sname, s in self.strategies.items():
                        if s.last_indicators:
                            for k, v in s.last_indicators.items():
                                factor_values[f"{sname}.{k}"] = float(v) if v is not None else 0.0
                    # forward_return: 用未来 1 根 bar 的 close 变化
                    if i + 1 < n:
                        fwd_ret = (bars[i + 1]["close"] - bar["close"]) / bar["close"]
                        self.factor_monitor.on_bar(factor_values, fwd_ret, bar["time"])
                except Exception:
                    pass

        # 复用 PaperTrader._build_report 的统计, 但 strategy 名是 primary (报告风格)
        # 简化: 直接用自己累计的统计生成 MABPaperReport
        return self._build_report()

    def _build_report(self) -> MABPaperReport:
        eq = np.array([e for _, e in self._equity_curve])
        if len(eq) < 2:
            return MABPaperReport(
                symbol=self.paper.strategy.symbol,
                timeframe=self.paper.strategy.timeframe,
                n_bars=0,
                initial_balance=self.paper.engine.initial_balance,
                final_balance=self.paper.engine.balance,
                net_pnl=0.0, total_return_pct=0.0,
                total_trades=0, wins=0, losses=0, win_rate=0.0,
                profit_factor=0.0, max_drawdown_pct=0.0, sharpe=0.0,
                longest_win_streak=0, longest_loss_streak=0,
                final_position="flat",
            )

        net_pnl = self.paper.engine.balance - self.paper.engine.initial_balance
        total_return_pct = net_pnl / self.paper.engine.initial_balance * 100

        # Sharpe (OPT-2 audit 2026-06-06: log returns + Newey-West HAC)
        # ──────────────────────────────────────────────────
        # 跟 paper_trader._finalize 同一公式, 集中放在 execution/_sharpe.py
        # 旧: simple returns + iid 假设 → Sharpe 虚高 20-50%
        # 新: log returns + Newey-West HAC std → 真风险调整后收益
        tf = self.paper.strategy.timeframe
        sharpe = sharpe_ratio_log_nw(eq, tf)

        peak = np.maximum.accumulate(eq)
        dd = (peak - eq) / peak
        max_dd = float(dd.max() * 100) if len(dd) > 0 else 0.0

        closes = [t for t in self.paper.engine.trades if t.direction in (2, -2)]
        wins = [t for t in closes if t.pnl > 0]
        losses = [t for t in closes if t.pnl <= 0]
        total = len(closes)
        win_rate = (len(wins) / total * 100) if total > 0 else 0.0
        gross_profit = sum(t.pnl for t in wins)
        gross_loss = abs(sum(t.pnl for t in losses))
        pf = (gross_profit / gross_loss) if gross_loss > 1e-9 else float("inf")

        longest_win = longest_loss = cur_win = cur_loss = 0
        for t in closes:
            if t.pnl > 0:
                cur_win += 1
                cur_loss = 0
                longest_win = max(longest_win, cur_win)
            else:
                cur_loss += 1
                cur_win = 0
                longest_loss = max(longest_loss, cur_loss)

        pos = self.paper.engine.position
        if pos and pos.direction != 0:
            pos_str = (f"{'LONG' if pos.direction==1 else 'SHORT'} "
                       f"{pos.volume} @ {pos.entry_price:.2f} "
                       f"sl={pos.sl_price:.2f} tp={pos.tp_price:.2f}")
        else:
            pos_str = "flat"

        # 日 PnL
        by_day: dict[date, float] = {}
        for t in closes:
            d = datetime.fromtimestamp(t.time, tz=timezone.utc).date()
            by_day[d] = by_day.get(d, 0.0) + t.pnl
        daily_pnl = [v for _, v in sorted(by_day.items())]

        return MABPaperReport(
            symbol=self.paper.strategy.symbol,
            timeframe=tf,
            n_bars=len(self._equity_curve),
            initial_balance=self.paper.engine.initial_balance,
            final_balance=self.paper.engine.balance,
            net_pnl=net_pnl,
            total_return_pct=total_return_pct,
            total_trades=total,
            wins=len(wins),
            losses=len(losses),
            win_rate=win_rate,
            profit_factor=pf,
            max_drawdown_pct=max_dd,
            sharpe=sharpe,
            longest_win_streak=longest_win,
            longest_loss_streak=longest_loss,
            final_position=pos_str,
            strategy_picks=dict(self._strategy_picks),
            strategy_pnl=dict(self._strategy_pnl),
            regime_pnl=dict(self._regime_pnl),
            daily_pnl=daily_pnl,
        )

    def print_report(self, r: MABPaperReport):
        print()
        print("=" * 72)
        print(f"  MAB PAPER TRADING REPORT — {r.symbol} @ {r.timeframe}")
        print("=" * 72)
        print(f"  Period        : {r.n_bars} bars")
        print(f"  Initial       : ${r.initial_balance:.2f}")
        print(f"  Final         : ${r.final_balance:.2f}")
        print(f"  Net PnL       : ${r.net_pnl:+.2f}  ({r.total_return_pct:+.2f}%)")
        print("-" * 72)
        print(f"  Trades        : {r.total_trades}  "
              f"(W:{r.wins} / L:{r.losses}  WR={r.win_rate:.1f}%)")
        print(f"  Profit Factor : {r.profit_factor:.2f}")
        print(f"  Max Drawdown  : {r.max_drawdown_pct:.2f}%")
        print(f"  Sharpe (ann.) : {r.sharpe:.3f}")
        print(f"  Streaks       : {r.longest_win_streak}W / {r.longest_loss_streak}L")
        print(f"  Final Pos.    : {r.final_position}")
        print("-" * 72)
        print("  Strategy picks (MAB):")
        total_picks = sum(r.strategy_picks.values()) or 1
        for s, c in sorted(r.strategy_picks.items(), key=lambda x: -x[1]):
            pct = c / total_picks * 100
            pnl = r.strategy_pnl.get(s, 0.0)
            print(f"    {s:25s} {c:5d}  ({pct:5.1f}%)   PnL ${pnl:+.2f}")
        print("-" * 72)
        print("  PnL by regime:")
        for reg, pnl in sorted(r.regime_pnl.items(), key=lambda x: -abs(x[1])):
            if pnl != 0:
                print(f"    {reg:15s} ${pnl:+.2f}")
        # 自学习层状态
        if self.scheduler is not None:
            print("-" * 72)
            print("  Scheduler weights:")
            # weights() 是 method, 返 dict
            w_dict = self.scheduler.weights() if callable(self.scheduler.weights) else self.scheduler.weights
            for s, w in w_dict.items():
                print(f"    {s:25s} w={w:.2f}")
        if self.router is not None:
            print("-" * 72)
            print("  MAB router stats (alpha/beta per regime):")
            stats = self.router.stats()
            if not stats.empty:
                # 只打印前 8 行, 避免刷屏
                print(stats.head(8).to_string(index=False))
        if self.retrain_scheduler is not None:
            print("-" * 72)
            print("  RetrainScheduler events:")
            evs = self.retrain_scheduler.get_events()
            if evs:
                for e in evs:
                    print(f"    bar={e['bar_idx']:6d}  n_trades={e['n_trades_so_far']:4d}  "
                          f"duration={e['duration_sec']:5.1f}s  success={e['success']}")
            else:
                print("    (no events)")
            print(f"  Stats: {self.retrain_scheduler.stats()}")
        if self.event_filter is not None:
            print("-" * 72)
            print("  EventFilter (T13) stats:")
            st = self.event_filter.stats()
            for k, v in st.items():
                print(f"    {k}: {v}")
        print("=" * 72)
