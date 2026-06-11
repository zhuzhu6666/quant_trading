"""
data/live_sync/orchestrator.py — 同步编排器 (T16.4, 2026-06-02)

把 MT5Puller + BarFilter + DBInserter 串起来:
- first_run: 全量拉取 N 根 bar (initial backfill)
- incremental: 增量拉取 (只拉上次 sync 之后的新 bar)
- 多 timeframe: M5/M15/M30/H1/H4/D1
- 统计 + 日志
"""
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime

from data.live_sync.mt5_puller import MT5Puller, PullResult, TIMEFRAME_MAP
from data.live_sync.bar_filter import BarFilter, FilterResult
from data.live_sync.db_inserter import DBInserter, InsertResult, SyncStatus
from data.live_sync.quality_gate import DataQualityGate

import pandas as pd

logger = logging.getLogger(__name__)


# 默认周期
DEFAULT_TIMEFRAMES = ["M5", "M15", "M30", "H1", "H4", "D1"]


@dataclass
class SyncReport:
    """一次 sync 的完整报告"""
    run_type: str                     # "initial" | "incremental"
    symbol: str
    started_utc: str
    elapsed_sec: float = 0.0
    per_tf: list = field(default_factory=list)        # list of dict
    total_inserted: int = 0
    total_dups: int = 0
    total_incomplete: int = 0
    error: str = ""


class SyncOrchestrator:
    """
    同步编排器 — MT5 → DB.

    用法:
        orch = SyncOrchestrator()
        # 首次回填 (全量)
        report = orch.full_sync("XAUUSD+", timeframes=["M15", "H1", "D1"], n_bars=5000)
        # 增量更新
        report = orch.incremental_sync("XAUUSD+", timeframes=["M15", "H1", "D1"])
        orch.shutdown()
    """

    def __init__(self, db_path: str = "data/market_data.db"):
        self.puller = MT5Puller()
        # P3 (audit 2026-06-04 BUG-12): 注入 puller, BarFilter 用 server epoch
        self.filter = BarFilter(db_path=db_path, mt5_puller=self.puller)
        self.inserter = DBInserter(db_path=db_path)
        self.db_path = db_path
        self._quality_gate = DataQualityGate()

    def connect(self) -> bool:
        return self.puller.connect()

    def shutdown(self):
        self.puller.shutdown()

    def run_once(self, full: bool = False, n_bars: int = 5000,
                 symbol: str = "XAUUSD+",
                 timeframes: list[str] = None) -> SyncReport:
        """便捷入口: 一次 full or incremental sync"""
        if full:
            return self.full_sync(symbol, timeframes=timeframes or DEFAULT_TIMEFRAMES, n_bars=n_bars)
        else:
            return self.incremental_sync(symbol, timeframes=timeframes or DEFAULT_TIMEFRAMES)

    def full_sync(self, symbol: str = "XAUUSD+",
                  timeframes: list[str] = None,
                  n_bars: int = 5000) -> SyncReport:
        """
        首次全量回填: 每个 timeframe 拉 N 根, 过滤去重, 写入 db.

        Args:
            symbol: 品种名
            timeframes: 周期列表 (默认全部)
            n_bars: 每周期拉取数
        """
        if timeframes is None:
            timeframes = DEFAULT_TIMEFRAMES
        t0 = _time.time()
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        report = SyncReport(run_type="initial", symbol=symbol, started_utc=now_str)

        logger.info(f"[SyncOrch] full_sync: {symbol} x {timeframes}, n_bars={n_bars}")
        insert_results: list[InsertResult] = []   # 累积真实 InsertResult 传给 save_status (修 bug 2026-06-02)
        all_errors: list[str] = []                # v4-fix-1 (audit 2026-06-06): 跟 incremental_sync 一致, full_sync 也初始化, 避免 pull 失败时 UnboundLocalError
        for tf in timeframes:
            tf_info = TIMEFRAME_MAP.get(tf, (tf, 15))
            tf_minutes = tf_info[1]
            pull = self.puller.pull_history(symbol, tf, n=n_bars)
            if pull.error:
                logger.warning(f"[SyncOrch] {tf} 拉取失败: {pull.error}")
                all_errors.append(f"{tf}: {pull.error}")
                report.per_tf.append({"tf": tf, "error": pull.error})
                continue

            filt = self.filter.filter(pull.bars, symbol, tf, tf_minutes=tf_minutes)
            ins = self.inserter.insert_bars(filt.kept, symbol, tf)
            # DataQualityGate check
            try:
                _df = pd.DataFrame(filt.kept)
                if not _df.empty and "time" in _df.columns:
                    _df = _df.rename(columns={"time": "ts"})
                _qr = self._quality_gate.check(symbol, tf, _df)
                if not _qr.passed:
                    logger.warning(f"[SyncOrch] {tf} quality gate: bad_ratio={_qr.bad_ratio:.3f} gaps={_qr.n_gaps} dups={_qr.n_duplicates} outliers={_qr.n_outliers}")
            except Exception as _e:
                logger.warning(f"[SyncOrch] {tf} quality check failed: {_e}")
            report.per_tf.append({
                "tf": tf,
                "pulled": pull.n_bars,
                "dups": filt.dup_count,
                "incomplete": filt.incomplete_count,
                "inserted": ins.inserted,
                "total_db": ins.total_db_bars,
                "error": ins.error or "",
            })
            insert_results.append(ins)
            report.total_inserted += ins.inserted
            report.total_dups += filt.dup_count
            report.total_incomplete += filt.incomplete_count

        self.inserter.save_status(insert_results, symbol)
        report.elapsed_sec = _time.time() - t0
        logger.info(f"[SyncOrch] full_sync 完成: +{report.total_inserted} bars "
                    f"({report.elapsed_sec:.1f}s)")
        return report

    def incremental_sync(self, symbol: str = "XAUUSD+",
                         timeframes: list[str] = None) -> SyncReport:
        """
        增量更新: 拉上次 sync 之后的新 bar (最近 200 根), 过滤去重, 只入新 bar.

        Args:
            symbol: 品种名
            timeframes: 周期列表 (默认全部)
        """
        if timeframes is None:
            timeframes = DEFAULT_TIMEFRAMES
        t0 = _time.time()
        now_str = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        report = SyncReport(run_type="incremental", symbol=symbol, started_utc=now_str)

        # 取上次 sync 状态, 找到每个 tf 的 last_bar_time
        status = self.inserter.load_status()
        insert_results: list[InsertResult] = []   # 累积真实 InsertResult 传给 save_status (修 bug 2026-06-02)
        all_errors: list[str] = []                # 2026-06-03: collect per-tf pull errors so save_status marks the run as error
        for tf in timeframes:
            tf_info = TIMEFRAME_MAP.get(tf, (tf, 15))
            tf_minutes = tf_info[1]

            # 确定 since_time: 上次 sync 时 db 最新 time
            since_time = None
            if status and tf in status.per_tf:
                # 从 filter 拿真实 db max time
                since_time = self.filter._get_max_time(symbol, tf)
            if since_time is None:
                # 无历史状态 → 拉最近 200 根做增量
                since_time = 0

            # 拉增量 (最多 200 根 = 覆盖 2 天 M15)
            pull = self.puller.pull_incremental(symbol, tf, since_time=since_time or 0, max_bars=200)
            if pull.error:
                logger.warning(f"[SyncOrch] {tf} 增量拉取失败: {pull.error}")
                all_errors.append(f"{tf}: {pull.error}")
                report.per_tf.append({"tf": tf, "error": pull.error})
                continue

            filt = self.filter.filter(pull.bars, symbol, tf, tf_minutes=tf_minutes)
            ins = self.inserter.insert_bars(filt.kept, symbol, tf)
            # DataQualityGate check
            try:
                _df = pd.DataFrame(filt.kept)
                if not _df.empty and "time" in _df.columns:
                    _df = _df.rename(columns={"time": "ts"})
                _qr = self._quality_gate.check(symbol, tf, _df)
                if not _qr.passed:
                    logger.warning(f"[SyncOrch] {tf} quality gate: bad_ratio={_qr.bad_ratio:.3f} gaps={_qr.n_gaps} dups={_qr.n_duplicates} outliers={_qr.n_outliers}")
            except Exception as _e:
                logger.warning(f"[SyncOrch] {tf} quality check failed: {_e}")
            report.per_tf.append({
                "tf": tf,
                "pulled": pull.n_bars,
                "dups": filt.dup_count,
                "incomplete": filt.incomplete_count,
                "inserted": ins.inserted,
                "total_db": ins.total_db_bars,
                "error": ins.error or "",
            })
            insert_results.append(ins)
            report.total_inserted += ins.inserted
            report.total_dups += filt.dup_count
            report.total_incomplete += filt.incomplete_count

        self.inserter.save_status(insert_results, symbol, error="; ".join(all_errors))
        report.elapsed_sec = _time.time() - t0
        logger.info(f"[SyncOrch] incremental 完成: +{report.total_inserted} bars "
                    f"({report.elapsed_sec:.1f}s)")
        return report

    def print_report(self, report: SyncReport):
        """打印可读报告"""
        print()
        print("=" * 72)
        print(f"  LIVE SYNC REPORT — {report.run_type.upper()} ({report.symbol})")
        print("=" * 72)
        for r in report.per_tf:
            tf = r.get("tf", "?")
            if "error" in r and r["error"]:
                print(f"  {tf:6s}  ERROR: {r['error']}")
            else:
                print(f"  {tf:6s}  pull={r.get('pulled',0):5d}  dup={r.get('dups',0):4d}  "
                      f"incomplete={r.get('incomplete',0):2d}  inserted={r.get('inserted',0):5d}  "
                      f"db_total={r.get('total_db',0):6d}")
        print("-" * 72)
        print(f"  Total inserted: {report.total_inserted} bars")
        print(f"  Total dups:     {report.total_dups}")
        print(f"  Incomplete:     {report.total_incomplete}")
        print(f"  Elapsed:        {report.elapsed_sec:.1f}s")
        print("=" * 72)


# ── module-level entry (供 scripts/live_sync.py 直接调) ──────────
def run_once(
    timeframes: "list[str] | None" = None,
    sync_type: str = "incremental",
    symbol: str = "XAUUSD+",
    n_bars: int = 5000,
    db_path: str = "data/market_data.db",
) -> dict:
    """Module-level one-shot sync. 内部实例化 SyncOrchestrator.
    audit 2026-06-08: backend service 通过 scripts/live_sync 调此函数,
    之前缺 module-level 入口导致 AttributeError."""
    if timeframes is None:
        timeframes = ["M15", "H1", "D1"]
    orch = SyncOrchestrator(db_path=db_path)
    if not orch.connect():
        return {"error": "MT5 connect failed", "total_inserted": 0}
    try:
        if sync_type == "full":
            report = orch.full_sync(symbol=symbol, timeframes=timeframes, n_bars=n_bars)
        else:
            report = orch.incremental_sync(symbol=symbol, timeframes=timeframes)
        return {
            "total_inserted": report.total_inserted,
            "total_dups": report.total_dups,
            "per_tf": report.per_tf,
            "elapsed_sec": report.elapsed_sec,
            "run_type": report.run_type,
            "error": report.error or "",
        }
    finally:
        orch.shutdown()
