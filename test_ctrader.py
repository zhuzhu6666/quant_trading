
import sys, os, logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
sys.path.insert(0, ".")

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")

from execution.ctrader_bridge import CTraderBridge

bridge = CTraderBridge(
    client_id=env["CTRADER_CLIENT_ID"],
    client_secret=env["CTRADER_CLIENT_SECRET"],
    access_token=env["CTRADER_ACCESS_TOKEN"],
    account_id=int(env["CTRADER_ACCOUNT_ID"]),
    send_orders=False,
)

print("Connecting to cTrader...")
ok = bridge.connect()
print("Result:", ok)
if ok:
    info = bridge.account_info()
    print("Equity:", info.equity, "Balance:", info.balance)
bridge.disconnect()
