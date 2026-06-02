"""
from MT5拉取历史K线数据并存储到本地SQLite数据库
Usage:
    python scripts/fetch_mt5_data.py              # 拉取所有周期
    python scripts/fetch_mt5_data.py --symbol XAUUSD+ --timeframe H1
    python scripts/fetch_mt5_data.py --bars 18000  # 指定根数
"""
import argparse
import sys
from pathlib import Path
from datetime import datetime, timedelta

import MetaTrader5 as mt5

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from modules.database import init_database, insert_candles, get_time_range, candle_count, table_summary
from config import MT5_LOGIN, MT5_PASSWORD, MT5_SERVER, SYMBOL

# MT5 symbol name
SYMBOL = "XAUUSD+"  # 你的经纪商需要带+号

# 目标时间周期 (必须用MT5枚举值，不能用分钟数)
# mt5.TIMEFRAME_M5=1, TIMEFRAME_M15=2, TIMEFRAME_M30=3, TIMEFRAME_H1=4, TIMEFRAME_H4=5, TIMEFRAME_D1=12
TIMEFRAMES = {
    "M5":  ("M5",  mt5.TIMEFRAME_M5),
    "M15": ("M15", mt5.TIMEFRAME_M15),
    "M30": ("M30", mt5.TIMEFRAME_M30),
    "H1":  ("H1",  mt5.TIMEFRAME_H1),
    "H4":  ("H4",  mt5.TIMEFRAME_H4),
    "D1":  ("D1",  mt5.TIMEFRAME_D1),
}

# 每个请求最多拉取的K线数(MT5限制)
MAX_BARS_PER_REQUEST = 50000


def connect_mt5():
    """连接MT5"""
    import MetaTrader5 as mt5
    if not mt5.initialize(login=MT5_LOGIN, password=MT5_PASSWORD, server=MT5_SERVER):
        logger.error(f"MT5连接失败: {mt5.last_error()}")
        return None
    account = mt5.account_info()
    logger.info(f"MT5已连接: 账户{MT5_LOGIN} 服务器{MT5_SERVER} 余额${account.balance}")
    return mt5


def fetch_timeframe(mt5, symbol: str, tf_key: str, bars: int = None) -> int:
    """拉取单个时间周期的数据

    优先用 copy_rates_from_pos（稳定），不够再分批
    """
    tf_label, tf_mt5 = TIMEFRAMES[tf_key]

    # 默认: 2年数据的估算
    # H1 ≈ 730*24 = 17520, M5 ≈ 730*24*12 = 210240
    default_bars = {
        "M5": 200000,
        "M15": 50000,
        "M30": 25000,
        "H1": 18000,
        "H4": 5000,
        "D1": 500,
    }
    if bars is None:
        bars = default_bars.get(tf_key, 50000)

    logger.info(f"拉取 {symbol} {tf_label} (最近{bars}根K线)")

    # 分批拉取，每批最多 MAX_BARS_PER_REQUEST
    all_rates = []
    fetched = 0
    offset = 0

    while fetched < bars:
        batch_size = min(MAX_BARS_PER_REQUEST, bars - fetched)
        rates = mt5.copy_rates_from_pos(symbol, tf_mt5, offset, batch_size)

        if rates is None:
            logger.warning(f"{tf_label}: offset={offset} 失败 {mt5.last_error()}")
            break

        if len(rates) == 0:
            break

        all_rates.extend(rates)
        fetched += len(rates)
        offset += len(rates)
        logger.info(f"  offset={offset}, 本批{len(rates)}条 (累计{len(all_rates)})")

        # MT5深层历史有限制，若返回数量少于请求数量，说明到底了
        if len(rates) < batch_size:
            break

    if not all_rates:
        logger.warning(f"{tf_label}: 无数据")
        return 0

    # 转换为DataFrame
    import pandas as pd
    import numpy as np

    # all_rates 是从 numpy array extend 来的列表，需要重新构造
    # 用 numpy 竖向堆叠比 pd.DataFrame(list) 更安全
    if not all_rates:
        return 0
    arr = np.array(all_rates, dtype=object)
    # 如果所有元素形状一致，转为结构化数组
    try:
        arr = np.array(all_rates)
    except Exception:
        pass
    df = pd.DataFrame(arr, columns=["time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume"])
    if "time" not in df.columns:
        logger.warning(f"{tf_label}: 数据列异常 {df.columns.tolist()}")
        return 0
    df["time"] = pd.to_datetime(df["time"].astype(np.int64), unit="s")
    df.sort_values("time", inplace=True)
    df.drop_duplicates("time", keep="last", inplace=True)

    # 写入数据库
    inserted = insert_candles(df, symbol, tf_key)
    return inserted


def fetch_all_timeframes(mt5, symbol: str, bars: int = None):
    """拉取所有时间周期"""
    total = 0
    for tf_key in TIMEFRAMES:
        count = fetch_timeframe(mt5, symbol, tf_key, bars)
        total += count
    return total


def main():
    parser = argparse.ArgumentParser(description="从MT5拉取历史K线数据")
    parser.add_argument("--symbol", default=SYMBOL, help="交易品种")
    parser.add_argument("--timeframe", default=None, choices=list(TIMEFRAMES.keys()), help="时间周期(默认全部)")
    parser.add_argument("--bars", type=int, default=None, help="拉取K线根数(默认自动估算)")
    args = parser.parse_args()

    # 初始化数据库
    init_database()

    # 连接MT5
    mt5 = connect_mt5()
    if mt5 is None:
        return

    try:
        if args.timeframe:
            # 单个时间周期
            inserted = fetch_timeframe(mt5, args.symbol, args.timeframe, args.bars)
        else:
            # 所有时间周期
            inserted = fetch_all_timeframes(mt5, args.symbol, args.bars)

        logger.info(f"完成! 共写入{inserted}条K线")

        # 打印数据概览
        print("\n=== 数据库概览 ===")
        summary = table_summary()
        if not summary.empty:
            print(summary.to_string(index=False))
        else:
            print("数据库为空")

    finally:
        mt5.shutdown()
        logger.info("MT5已断开")


if __name__ == "__main__":
    main()