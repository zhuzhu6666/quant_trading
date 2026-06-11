"""
scripts/test_ctrader_full_flow.py — cTrader 开平仓 + SL/TP 全流测试

流程:
  1. connect()
  2. account_info() 看余额
  3. get_positions() 确认无持仓
  4. 查 symbol meta (min_volume / step_volume)
  5. market_buy(volume=min_lot) 真开多
  6. get_positions() 确认持仓已开
  7. amend_position_sltp(position_id, sl=低于市价1刀, tp=高于市价1刀)
  8. get_positions() 确认 SL/TP 已设
  9. close_position(position_id) 平仓
  10. get_positions() 确认已平
  11. disconnect()

用法:
  python scripts/test_ctrader_full_flow.py [--volume 0.1] [--dry-run]
"""
from __future__ import annotations

import logging
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env
load_env()

from execution.ctrader_bridge import CTraderBridge, HAS_CTRADER

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ctrader_full_test")

if not HAS_CTRADER:
    log.error("ctrader-open-api 未装; pip install ctrader-open-api")
    sys.exit(1)

# ── 读凭证 ──────────────────────────────────────────────
client_id = os.environ.get("CTRADER_CLIENT_ID", "").strip()
client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "").strip()
account_id = int(os.environ.get("CTRADER_ACCOUNT_ID", "0"))

if not all([client_id, client_secret, access_token, account_id]):
    log.error("CTRADER_CLIENT_ID / SECRET / ACCESS_TOKEN / ACCOUNT_ID 未设全")
    sys.exit(1)

# ── 参数 ────────────────────────────────────────────────
import argparse
p = argparse.ArgumentParser()
p.add_argument("--volume", type=float, default=0.01, help="手数 (默认 0.01)")
p.add_argument("--dry-run", action="store_true", help="DRY-RUN 不真发")
p.add_argument("--sl-offset", type=float, default=5.0, help="SL 偏移 (默认 5.0 USD)")
p.add_argument("--tp-offset", type=float, default=2.0, help="TP 偏移 (默认 2.0 USD)")
args = p.parse_args()

# ── 报告收集 ────────────────────────────────────────────
lines = []
def r(s):
    log.info(s)
    lines.append(s)

r("=" * 70)
r(f"  cTrader 全流测试 — {time.strftime('%Y-%m-%d %H:%M:%S')}")
r("=" * 70)
r(f"  account_id  = {account_id}")
r(f"  volume      = {args.volume} lot")
r(f"  send_orders = {not args.dry_run}")
r("")

# ── 建立 bridge ──────────────────────────────────────
bridge = CTraderBridge(
    client_id=client_id,
    client_secret=client_secret,
    access_token=access_token,
    account_id=account_id,
    send_orders=not args.dry_run,
    request_timeout_sec=20.0,
)

# ── 步骤 1: connect ─────────────────────────────────
r("[1/5] connect() ...")
t0 = time.time()
if not bridge.connect():
    r(f"  ❌ connect FAILED ({time.time()-t0:.1f}s)")
    sys.exit(1)
r(f"  ✅ OK ({time.time()-t0:.1f}s, is_connected={bridge.is_connected})")
r("")

# ── 步骤 2: account_info ────────────────────────────
r("[2/5] account_info() ...")
info = bridge.account_info()
bal = info.get("balance", "?")
cur = info.get("currency", "?")
eq = info.get("equity", "?")
lev = info.get("leverage", "?")
r(f"  余额={bal} {cur}  净值={eq}  杠杆={lev}")
r("")

# ── 步骤 3: get_positions (before) ──────────────────
r("[3/5] 开仓前 get_positions() ...")
before = bridge.get_positions()
r(f"  当前持仓: {len(before)}")
for p_ in before[:3]:
    r(f"    pos_id={p_['position_id']} {p_['type']} vol={p_['volume']} @ {p_['price_open']}")
r("")

# ── 步骤 4: 查 symbol meta ──────────────────────────
r("[4/5] 解析 symbol meta ...")
sid = bridge._symbol_id
meta = bridge._symbol_meta if hasattr(bridge, "_symbol_meta") else {}
r(f"  symbol_id = {sid}")
r(f"  meta: {meta}")

# 计算可用 volume
min_vol = meta.get("min_volume", 0.01)
step_vol = meta.get("step_volume", 0.01)
lot_size = meta.get("lot_size", 100)

# 如果用户指定的 volume 小于最小值，上取整到最小
use_volume = max(args.volume, min_vol)
# 按 step 取整
if step_vol > 0:
    use_volume = round(use_volume / step_vol) * step_vol

r(f"  使用 volume = {use_volume} lot (min={min_vol}, step={step_vol}, lot_size={lot_size})")
r("")

# ── 步骤 5: market_buy ──────────────────────────────
r("[5/9] market_buy(volume={}) ...".format(use_volume))
t0 = time.time()
buy_result = bridge.market_buy(volume=use_volume, comment="hermes-test-2026-06-11")
r(f"  耗时: {time.time()-t0:.1f}s")
r(f"  success   = {buy_result.success}")
r(f"  order_id  = {buy_result.order_id}")
r(f"  comment   = {buy_result.comment}")
if buy_result.error_code:
    r(f"  error     = {buy_result.error_code}")
r("")

if not buy_result.success:
    r("⚠ 开仓失败，后续步骤跳过")
    # 仍尝试平仓清理
else:
    # ── 步骤 6: get_positions (after open) ──────────
    r("[6/9] 开仓后 get_positions() ...")
    time.sleep(2)  # 等 broker processing
    after_open = bridge.get_positions()
    r(f"  当前持仓: {len(after_open)}")
    opened = [p_ for p_ in after_open if p_.get("position_id") > 0]
    for p_ in opened[:5]:
        r(f"    pos_id={p_['position_id']} {p_['type']} vol={p_['volume']} @ {p_['price_open']}  "
          f"sl={p_['sl']} tp={p_['tp']}")
    r("")

    if not opened:
        r("⚠ 没有找到已开仓位")
    else:
        first_pos = opened[0]
        pid = first_pos["position_id"]
        entry_price = first_pos["price_open"]
        sl_price = max(0.01, entry_price - args.sl_offset)
        tp_price = max(0.01, entry_price + args.tp_offset)
        r(f"  → 将修改 position_id={pid}, entry_price={entry_price}, sl={sl_price}, tp={tp_price}")

        # ── 步骤 7: amend_position_sltp ─────────────
        r("[7/9] amend_position_sltp(pos_id={}, sl={:.2f}, tp={:.2f}) ..."
          .format(pid, sl_price, tp_price))
        t0 = time.time()
        amend_result = bridge.amend_position_sltp(position_id=pid, sl=sl_price, tp=tp_price)
        r(f"  耗时: {time.time()-t0:.1f}s")
        r(f"  success   = {amend_result.success}")
        r(f"  comment   = {amend_result.comment}")
        if amend_result.error_code:
            r(f"  error     = {amend_result.error_code}")

        # ── 检查 SL/TP: get_positions again ────────
        r("  验证 SL/TP: get_positions() ...")
        time.sleep(1)
        after_amend = bridge.get_positions()
        amended = [p_ for p_ in after_amend if p_["position_id"] == pid]
        if amended:
            r(f"    sl={amended[0]['sl']}, tp={amended[0]['tp']}")
            if amended[0]["sl"] > 0 or amended[0]["tp"] > 0:
                r("  ✅ SL/TP 已设")
            else:
                r("  ⚠ SL/TP 仍为 0 (server 可能拒绝或异步)")
        else:
            r("  ⚠ 仓位已不存在")
        r("")

        # ── 步骤 9: close_position ─────────────────
        r("[9/9] close_position(pos_id={}) ...".format(pid))
        t0 = time.time()
        close_result = bridge.close_position(position_id=pid)
        r(f"  耗时: {time.time()-t0:.1f}s")
        r(f"  success   = {close_result.success}")
        r(f"  comment   = {close_result.comment}")
        if close_result.error_code:
            r(f"  error     = {close_result.error_code}")

        # ── 验证已平 ──────────────────────────────
        r("  验证: get_positions() ...")
        time.sleep(2)
        after_close = bridge.get_positions()
        still_open = [p_ for p_ in after_close if p_["position_id"] == pid]
        if not still_open:
            r("  ✅ 仓位已平")
        else:
            r(f"  ⚠ 仓位仍存在 {still_open}")
        r("")

# ── cleanup ──────────────────────────────────────────
r("[cleanup] disconnect() ...")
bridge.disconnect()
r("  done.")
r("=" * 70)

# ── 落报告 ──────────────────────────────────────────
report_path = Path("data/charts/ctrader_full_flow_report.txt")
report_path.parent.mkdir(parents=True, exist_ok=True)
report_path.write_text("\n".join(lines), encoding="utf-8")
log.info(f"Report: {report_path.resolve()}")
