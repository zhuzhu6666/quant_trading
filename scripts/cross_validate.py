"""Cross-validate: compare /api/state vs direct cTrader bridge vs DuckDB."""
import subprocess, json

BASE = "http://localhost:8000"

# Login
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
auth = "Bearer " + token

def api(path):
    r = subprocess.run(["curl", "-s", BASE + path, "-H", "Authorization: " + auth],
        capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

print("=" * 50)
print("1. /api/state  vs  /api/live/account")
print("=" * 50)
state = api("/api/state")
acct = api("/api/live/account")

print("state.equity:  ", state.get("equity"))
print("state.balance: ", state.get("balance"))
print("acct.equity:   ", acct.get("equity"))
print("acct.balance:  ", acct.get("balance"))
print("acct.currency: ", acct.get("currency"))

# Both should match when pipeline is running
match = (state.get("equity") == acct.get("equity") and 
         state.get("balance") == acct.get("balance"))
print("MATCH:", "OK" if match else "MISMATCH")

print()
print("=" * 50)
print("2. /api/v4/weights 数据合理性")
print("=" * 50)
weights = api("/api/v4/weights")
if weights:
    total = sum(w.get("new", 0) for w in weights)
    print("weights count:", len(weights))
    print("sum of all new:", round(total, 2))
    print("top 3:")
    for w in weights[:3]:
        print("  %s: %s" % (w["factor"], w["new"]))
    # Check: weights should be positive, no duplicates
    factors = [w["factor"] for w in weights]
    dups = len(factors) - len(set(factors))
    negs = sum(1 for w in weights if w.get("new", 0) < 0)
    print("duplicates:", dups)
    print("negatives:", negs)

print()
print("=" * 50)
print("3. closed_loop nodes 一致性")
print("=" * 50)
cl = state.get("closed_loop", {})
nodes = cl.get("nodes", {})
expected = ["data_sync","factor_engine","signal_normalizer","portfolio_compositor",
            "execution_gate","execution","attribution","adaptive_weight","risk"]
missing = [k for k in expected if k not in nodes]
extra = [k for k in nodes if k not in expected]
print("missing nodes:", missing or "none")
print("extra nodes:", extra or "none")
print("pipeline_active:", cl.get("pipeline_active"))

# Valid statuses
valid_statuses = {'running','ok','active','initialized','no_data','cold_start',
                  'stale','waiting','inactive','stale_critical','circuit_breaker','error'}
for k, v in sorted(nodes.items()):
    s = v.get("status", "?")
    ok = s in valid_statuses
    print("  %-22s %-15s [%s]" % (k, s, "OK" if ok else "INVALID"))

print()
print("=" * 50)
print("4. DuckDB: bars 新鲜度")
print("=" * 50)
try:
    import duckdb
    db = duckdb.connect("data/ctrader_data.duckdb")
    for tf in ["M5", "M15", "H1"]:
        row = db.execute(
            "SELECT MAX(time), COUNT(*) FROM bars WHERE symbol='XAUUSD+' AND timeframe=?",
            [tf]
        ).fetchone()
        if row:
            import time as _t
            latest = row[0] or 0
            count = row[1] or 0
            age_min = (_t.time() - latest) / 60 if latest else 999
            print("  %s: %d bars, latest=%s (%.0f min ago)" % (tf, count,
                _t.strftime('%H:%M', _t.localtime(latest)) if latest else 'never',
                age_min))
    db.close()
except Exception as e:
    print("  Error:", e)
