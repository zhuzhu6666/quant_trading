"""
scripts/ctrader_poc.py — cTrader Open API 最小 PoC

阶段: 第 1 阶段 (API 连通 + 下笔 market 单)
目标:
  1. App auth + Account auth
  2. 拉账户信息 + 当前持仓
  3. 拉 100 根 M15 bar
  4. (DRY-RUN 默认) market_buy 0.01 lot XAUUSD, 仅打印不真发

凭证: 全走环境变量 (避免 hardcode):
  CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET,
  CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID

用法:
  python scripts/ctrader_poc.py            # DRY-RUN 模式 (send_orders=False)
  python scripts/ctrader_poc.py --live     # 真下单 (谨慎, 默认禁)
  python scripts/ctrader_poc.py --symbol XAUUSD --volume 0.01
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from pathlib import Path

# 让脚本能从项目根 import
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env  # noqa: E402
load_env()  # 自动从 .env 读 CTRADER_*, 无需手动 export

from execution.ctrader_bridge import CTraderBridge, HAS_CTRADER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ctrader_poc")


def env_or_die(key: str) -> str:
    val = os.environ.get(key, "").strip()
    if not val:
        log.error(f"env {key} 未设")
        sys.exit(1)
    return val


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="demo.ctraderapi.com",
                   help="demo=模拟盘 host; live 实盘换 broker 给的 host")
    p.add_argument("--port", type=int, default=5035)
    p.add_argument("--symbol", default="XAUUSD",
                   help="Pepperstone 是 XAUUSD (无 +); ICMarkets 试 XAUUSD.a")
    p.add_argument("--volume", type=float, default=0.01,
                   help="手数; XAUUSD 0.01=1 oz, contract_size 100")
    p.add_argument("--live", action="store_true",
                   help="真下单 (默认 DRY-RUN, 改 send_orders=True)")
    p.add_argument("--skip-bars", action="store_true",
                   help="跳过 fetch_bars (节省 5-10s)")
    p.add_argument("--report", default="data/charts/ctrader_poc_report.txt",
                   help="落盘报告路径")
    args = p.parse_args()

    if not HAS_CTRADER:
        log.error("ctrader-open-api 未装; pip install ctrader-open-api")
        sys.exit(1)

    client_id = env_or_die("CTRADER_CLIENT_ID")
    client_secret = env_or_die("CTRADER_CLIENT_SECRET")
    access_token = env_or_die("CTRADER_ACCESS_TOKEN")
    account_id = int(env_or_die("CTRADER_ACCOUNT_ID"))

    report_lines = []
    def _r(s):
        log.info(s)
        report_lines.append(s)

    _r("=" * 70)
    _r(f"  cTrader Open API PoC — {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _r("=" * 70)
    _r(f"  host:port    = {args.host}:{args.port}")
    _r(f"  account_id   = {account_id}")
    _r(f"  client_id    = {client_id[:10]}...{client_id[-4:]} (masked)")
    _r(f"  symbol       = {args.symbol}")
    _r(f"  volume       = {args.volume} lot")
    _r(f"  send_orders  = {args.live}  (False=DRY-RUN)")
    _r("")

    bridge = CTraderBridge(
        client_id=client_id,
        client_secret=client_secret,
        access_token=access_token,
        account_id=account_id,
        host=args.host,
        port=args.port,
        symbol=args.symbol,
        send_orders=args.live,  # 安全闸: 默认 False
        request_timeout_sec=15.0,
    )

    _r(f"[1/5] connect() ...")
    t0 = time.time()
    if not bridge.connect():
        _r(f"  FAILED (耗时 {time.time()-t0:.1f}s); 看上面日志")
        _write_report(args.report, report_lines)
        sys.exit(1)
    _r(f"  OK (耗时 {time.time()-t0:.1f}s, is_connected={bridge.is_connected})")
    _r("")

    try:
        # 2. 账户信息
        _r("[2/5] account_info() ...")
        info = bridge.account_info()
        _r(f"  balance    = {info.get('balance', '?')} {info.get('currency', '?')}")
        _r(f"  equity     = {info.get('equity', '?')}")
        _r(f"  leverage   = {info.get('leverage', '?')}")
        _r("")

        # 3. 当前持仓
        _r("[3/5] get_positions() ...")
        positions = bridge.get_positions()
        _r(f"  open positions: {len(positions)}")
        for p in positions[:5]:
            _r(f"    pos_id={p['position_id']} {p['type']:4s} vol={p['volume']} "
               f"@ {p['price_open']} pnl={p['profit']}")
        _r("")

        # 4. 拉 100 根 M15 bar
        if not args.skip_bars:
            _r("[4/5] fetch_bars(M15, 100) ...")
            t0 = time.time()
            df = bridge.fetch_bars("M15", 100)
            if df is not None and not df.empty:
                _r(f"  OK ({len(df)} bars, 耗时 {time.time()-t0:.1f}s)")
                _r(f"  first  : {df.index[0]} close={df.iloc[0]['close']}")
                _r(f"  last   : {df.index[-1]} close={df.iloc[-1]['close']}")
            else:
                _r("  EMPTY (无数据或拉失败)")
            _r("")

        # 5. market_buy (DRY-RUN 默认)
        _r("[5/5] market_buy(...) ...")
        if not args.live:
            _r("  ⚠ DRY-RUN 模式, 不真发. 启用请加 --live")
        result = bridge.market_buy(volume=args.volume, comment="poc-2026-06-04")
        _r(f"  success   = {result.success}")
        _r(f"  order_id  = {result.order_id}")
        _r(f"  comment   = {result.comment}")
        if result.error_code:
            _r(f"  error     = {result.error_code}")
        _r("")

    finally:
        _r("[cleanup] disconnect() ...")
        bridge.disconnect()
        _r("  done.")
        _r("=" * 70)

    _write_report(args.report, report_lines)
    log.info(f"Report written: {args.report}")


def _write_report(path: str, lines: list[str]):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
