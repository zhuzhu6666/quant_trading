#!/usr/bin/env python
"""验证 cTrader fetch_bars 修复 — K 线 + Tick 数据"""
import sys, os, time
sys.path.insert(0, r"C:\Users\zhu\quant_trading")
os.chdir(r"C:\Users\zhu\quant_trading")

from execution._env import load_env
load_env()

from execution.ctrader_bridge import CTraderBridge

bridge = CTraderBridge(
    client_id=os.getenv("CTRADER_CLIENT_ID", ""),
    client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
    access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
    account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
    symbol="XAUUSD",
    send_orders=False,
)

print("=" * 60)
print("验证: cTrader K线 + Tick 数据获取")
print("=" * 60)

ok = bridge.connect()
print(f"\n[1] 连接: {'✅' if ok else '❌'}")
if not ok:
    sys.exit(1)

# ── M5 K线 ──
print(f"\n[2] fetch_bars('M5', 100) — 最近 100 根 M5 K线...")
df = bridge.fetch_bars("M5", 100)
if df is not None:
    print(f"    ✅ 成功: {len(df)} bars")
    print(f"    时间范围: {df.index[0]} ~ {df.index[-1]}")
    print(f"    最新 5 根:")
    for i in range(max(0, len(df)-5), len(df)):
        bar = df.iloc[i]
        print(f"      {df.index[i]} O={bar.open:.2f} H={bar.high:.2f} L={bar.low:.2f} C={bar.close:.2f} V={bar.volume}")
else:
    print("    ❌ fetch_bars 返回 None")

# ── Tick 数据 ──
print(f"\n[3] ProtoOAGetTickDataReq — 最近 60 秒 Tick...")
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAGetTickDataReq
req = ProtoOAGetTickDataReq()
req.ctidTraderAccountId = bridge.account_id
req.symbolId = bridge._symbol_id
req.type = 1
now_ms = int(time.time() * 1000)
req.fromTimestamp = now_ms - 60_000
req.toTimestamp = now_ms
resp = bridge._send(req, timeout=30.0)
ticks = list(resp.tickData) if hasattr(resp, 'tickData') else []
print(f"    {'✅' if ticks else '❌'} {len(ticks)} 笔 Tick")
if ticks:
    total = len(ticks)
    for t in ticks[:3]:
        t_price = round(t.tick / 100000, 2)
        ts = time.strftime('%H:%M:%S', time.gmtime(t.timestamp/1000))
        print(f"      {ts} tick={t.tick} price={t_price}")
    if total > 3:
        print(f"      ...({total-6} more)...")
        for t in ticks[-3:]:
            t_price = round(t.tick / 100000, 2)
            ts = time.strftime('%H:%M:%S', time.gmtime(t.timestamp/1000))
            print(f"      {ts} tick={t.tick} price={t_price}")
    print(f"    hasMore={resp.hasMore}")

bridge.disconnect()
print(f"\n[4] 断开 ✅")
print("=" * 60)
