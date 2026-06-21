import subprocess, json, os

env_path = "/home/ubuntu/quant_trading/.env"
jwt_secret = ""
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            if line.startswith("QUANT_JWT_SECRET"):
                jwt_secret = line.split("=", 1)[1].strip().strip("\"'")
                break

BASE = "http://localhost:8000"

# Login
r = subprocess.run(["curl", "-s", f"{BASE}/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
login = json.loads(r.stdout)
token = login.get("token") or login.get("access_token", "")
if not token:
    print("LOGIN FAILED:", r.stdout[:200])
    exit(1)
print("TOKEN:", token[:30] + "...")
AUTH = ["-H", "Authorization: Bearer " + token]

def api(path, desc):
    print("\n" + "=" * 60)
    print("=== %s (%s) ===" % (desc, path))
    r = subprocess.run(["curl", "-s", BASE + path] + AUTH,
        capture_output=True, text=True, timeout=10)
    try:
        return json.loads(r.stdout)
    except:
        print("PARSE ERROR:", r.stdout[:300])
        return None

# 1. /api/state
d = api("/api/state", "state")
if d:
    cl = d.get("closed_loop", {})
    print("pipeline_active:", cl.get("pipeline_active"))
    nodes = cl.get("nodes", {})
    print("nodes (%d):" % len(nodes))
    for k, v in sorted(nodes.items()):
        print("  %-22s status=%-15s" % (k, v.get("status", "?")))
    print("equity=%s, balance=%s, pnl_today=%s" % (d.get("equity"), d.get("balance"), d.get("pnl_today")))
    pos = d.get("position", {})
    print("position: dir=%s, entry=%s, size=%s, unrealized=%s" % (
        pos.get("dir"), pos.get("entry"), pos.get("size"), pos.get("unrealized")))
    daily = d.get("daily", {})
    print("daily: trades=%s, win=%s, loss=%s, pnl=%s, drawdown_pct=%s" % (
        daily.get("trades"), daily.get("win"), daily.get("loss"),
        daily.get("pnl"), daily.get("drawdown_pct")))
    risk = d.get("risk", {})
    print("risk: circuit_breaker=%s, consecutive_loss=%s" % (
        risk.get("circuit_breaker"), risk.get("consecutive_loss")))

# 2. /api/v4/stats
d = api("/api/v4/stats", "v4/stats")
if d and "summary" in d:
    s = d["summary"]
    print("status=%s" % d.get("status"))
    print("n_factors_attributed=%s, total_trades=%s" % (s.get("n_factors_attributed"), s.get("total_trades")))
    print("overall_win_rate=%s, avg_sharpe=%s" % (s.get("overall_win_rate"), s.get("avg_sharpe_across_factors")))
    pf = d.get("per_factor", {})
    print("per_factor count: %d" % len(pf))
    if pf:
        sorted_items = sorted(pf.items(), key=lambda x: abs(x[1].get("avg_mc", 0)), reverse=True)[:5]
        for name, st in sorted_items:
            print("  %-30s avg_mc=%+.4f  wr=%.1f%%  n=%d" % (
                name, st.get("avg_mc", 0), st.get("win_rate", 0) * 100, st.get("n_trades", 0)))
else:
    print("MISSING or error")

# 3. /api/v4/weights
d = api("/api/v4/weights", "v4/weights")
if isinstance(d, list):
    print("weights count: %d" % len(d))
    for w in d[:5]:
        print("  %-30s old=%.4f new=%.4f" % (w.get("factor", "?"), w.get("old", 0), w.get("new", 0)))
elif d:
    print(str(type(d)), json.dumps(d, indent=2, default=str)[:500])

# 4. /api/system/db-health (no auth)
r = subprocess.run(["curl", "-s", BASE + "/api/system/db-health"],
    capture_output=True, text=True, timeout=10)
d = json.loads(r.stdout)
print("\n" + "=" * 60)
print("=== db-health ===")
print("overall=%s, summary=%s" % (d.get("overall"), d.get("summary")))
for db in d.get("databases", []):
    print("  %-20s %-8s %12s rows  %s" % (db["name"], db["freshness"],
        "{:,}".format(db["total_rows"]) if db["total_rows"] else "0",
        db.get("size", "?")))

# 5. /api/control/evolution/latest
d = api("/api/control/evolution/latest", "evolution/latest")
if d:
    print("ts=%s" % d.get("ts"))
    print("gp_new=%s, shadow=%s, oos=%s" % (d.get("gp_new_candidates"), d.get("gp_registered_shadow"), d.get("oos_passed")))
    print("promotions=%s, rollbacks=%s" % (d.get("canary_promotions"), d.get("canary_rollbacks")))
    print("retire=%s, weights_updated=%s, duration=%ss" % (d.get("retire_candidates"), d.get("weights_updated"), d.get("duration_sec")))
else:
    print("MISSING or error")
