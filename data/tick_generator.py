"""data/tick_generator.py — Tick-level 历史数据生成器

用 OHLCV bar 通过 Brownian bridge 生成 tick-level CSV 文件 (time, price, volume).
输出存到 data/ticks/{symbol}_{timeframe}.csv

依赖: numpy + csv (标准库)
"""
from __future__ import annotations

import csv
import logging
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)


class TickGenerator:
    """OHLCV bar → tick 序列生成器

    Config:
        n_ticks_per_bar (int):      每个 bar 拆成多少个 tick (默认 100)
        seed (int):                 随机种子, 保证可复现 (默认 42)
        output_dir (str):           CSV 输出目录 (默认 data/ticks)
        symbol (str):               品种名, 用于文件名 (默认 XAUUSD+)
        bar_duration_seconds (int): 每根 bar 的时长秒数, 用于 tick 时间戳 (默认 900 = 15 min)
    """

    DEFAULT_CONFIG = {
        "n_ticks_per_bar": 100,
        "seed": 42,
        "output_dir": "data/ticks",
        "symbol": "XAUUSD+",
        "bar_duration_seconds": 900,  # 15 min = 900 s
    }

    def __init__(self, config: dict | None = None):
        self.config = {**self.DEFAULT_CONFIG, **(config or {})}
        self._rng = np.random.default_rng(self.config["seed"])

    # ------------------------------------------------------------------
    # core: bar → ticks
    # ------------------------------------------------------------------

    def bar_to_ticks(self, bar: dict) -> list[dict]:
        """给定单 bar (open/high/low/close/volume), 拆 N 个 tick.

        算法 (Brownian bridge):
            1. 线性插值 base = open → close
            2. 整体缩放到 [low, high] 区间
            3. 加高斯噪声 (σ = 0.01) + round(2)
            4. 均匀分配 volume

        返回 [{'time': float, 'price': float, 'volume': float}, ...]
        """
        n = self.config["n_ticks_per_bar"]
        bar_dur = float(self.config["bar_duration_seconds"])

        o = float(bar["open"])
        c = float(bar["close"])
        h = float(bar["high"])
        l = float(bar["low"])
        v = float(bar.get("volume", 0))
        bar_time = float(bar.get("time", 0))
        dt = bar_dur / n

        # 1) 基础线 open → close 线性插值
        base = np.linspace(o, c, n)

        # 2) 缩放到 [low, high] 区间 (Brownian bridge envelope)
        env_top = max(h, max(o, c))
        env_bot = min(l, min(o, c))
        ticks = base.copy()
        if ticks.max() - ticks.min() > 1e-12:
            ticks = env_bot + (ticks - ticks.min()) / (ticks.max() - ticks.min()) * (env_top - env_bot)

        # 3) 加高斯噪声 + 保留 2 位小数 + clip 在 [low, high]
        noise = self._rng.normal(0, 0.01, size=n)
        ticks = np.round(ticks + noise, 2)
        ticks = np.clip(ticks, env_bot, env_top)

        # 4) 体积均匀分配
        vol_per_tick = v / n if v > 0 else 0.0
        return [
            {"time": bar_time + i * dt, "price": float(t), "volume": vol_per_tick}
            for i, t in enumerate(ticks)
        ]

    # ------------------------------------------------------------------
    # batch: N 个 bar
    # ------------------------------------------------------------------

    def generate_ticks_for_bars(self, bars: list[dict]) -> list[dict]:
        """给定 N 个 bar, 顺序生成 N × n_ticks 个 tick.

        bars: [bar dict, ...], 每个 bar 含 time/open/high/low/close/volume
        返回: [tick dict, ...]
        """
        all_ticks: list[dict] = []
        for bar in bars:
            all_ticks.extend(self.bar_to_ticks(bar))
        return all_ticks

    # ------------------------------------------------------------------
    # CSV 输出
    # ------------------------------------------------------------------

    @staticmethod
    def save_ticks_to_csv(ticks: list[dict], filepath: str):
        """写 tick CSV (header: time,price,volume)."""
        Path(filepath).parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "price", "volume"])
            writer.writeheader()
            for tick in ticks:
                writer.writerow({
                    "time": round(float(tick["time"]), 3),
                    "price": round(float(tick["price"]), 2),
                    "volume": round(float(tick["volume"]), 2),
                })

    @staticmethod
    def _format_time(t: float) -> str:
        """epoch seconds → ISO 格式 (保留毫秒)."""
        import datetime as _dt
        us = int(round(t * 1_000_000))
        dt = _dt.datetime.utcfromtimestamp(us / 1_000_000)
        return dt.strftime("%Y-%m-%d %H:%M:%S.") + f"{dt.microsecond // 1000:03d}"

    # ------------------------------------------------------------------
    # 一体化
    # ------------------------------------------------------------------

    def generate_and_save(self, bars: list[dict], filename: str) -> str:
        """一体化: 生成 tick → 存 CSV.

        bars: [bar dict, ...]
        filename: 输出文件名 (如 'XAUUSD+_M15_ticks.csv')
        返回: 完整文件路径
        """
        ticks = self.generate_ticks_for_bars(bars)
        out_dir = Path(self.config["output_dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        filepath = str(out_dir / filename)
        self.save_ticks_to_csv(ticks, filepath)
        logger.info("Saved %d ticks to %s", len(ticks), filepath)
        return filepath
