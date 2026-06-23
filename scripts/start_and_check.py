import subprocess, json, time

BASE = "http://localhost:8000"

# Login
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
if not token:
    print("LOGIN FAILED")
    exit(1)

auth = "Bearer " + token

def api(path, method="GET", body=None):
    args = ["curl", "-s", BASE + path, "-H", "Authorization: " + auth]
    if method == "POST":
        args += ["-X", "POST"]
    if body:
        args += ["-H", "Content-Type: application/json", "-d", body]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)

# Start
print("Starting pipeline...")
d = api("/api/live/start", "POST", "{}")
print("start result:", json.dumps(d))

# Wait for cTrader
print("Waiting for cTrader connection + account fetch (up to 45s)...")
for i in range(1, 6):
    time.sleep(8)
    d = api("/api/state")
    eq = d.get("equity", 0) or 0
    bal = d.get("balance", 0) or 0
    pr = d.get("current_price", 0) or 0
    src = d.get("source", "?")
    print("  t+%ds: src=%s eq=%s bal=%s price=%s" % (i * 8, src, eq, bal, pr))
    if eq > 0 and bal > 0 and pr:
        print("  Got account data!")
        break

# Final check
d = api("/api/state")
print()
print("=== FINAL STATE ===")
print("source:", d.get("source"))
print("equity:", d.get("equity"))
print("balance:", d.get("balance"))
print("price:", d.get("current_price"))
print("pipeline_active:", d.get("closed_loop", {}).get("pipeline_active"))
print("position:", d.get("position"))

eq = d.get("equity", 0) or 0
bal = d.get("balance", 0) or 0
pr = d.get("current_price", 0) or 0
print()
if eq > 0 and bal > 0 and pr:
    print(">>> ALL DATA PRESENT - frontend OK")
else:
    print(">>> DATA MISSING:")
    if not eq: print("    equity = 0")
    if not bal: print("    balance = 0")
    if not pr: print("    price = None")
