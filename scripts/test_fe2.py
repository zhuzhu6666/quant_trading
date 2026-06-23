import subprocess, json, time, sys

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
if not token:
    print("LOGIN FAILED")
    sys.exit(1)
H = ["-H", "Authorization: Bearer *** + token]

def api(path):
    args = ["curl", "-s", BASE + path] + H
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

print("Waiting for cTrader...")
for i in range(1, 5):
    time.sleep(8)
    d = api("/api/state")
    eq = d.get("equity", 0) or 0
    bal = d.get("balance", 0) or 0
    pr = d.get("current_price", 0) or 0
    src = d.get("source", "?")
    pa = d.get("closed_loop", {}).get("pipeline_active")
    print("t+%ds: src=%s eq=%s bal=%s price=%s pipe=%s" % (i * 10, src, eq, bal, pr, pa))
    if eq > 0 and bal > 0:
        print("GOT DATA!")
        break

# Final check
d = api("/api/state")
print()
print("FINAL: source=%s equity=%s balance=%s price=%s" % (
    d.get("source"), d.get("equity"), d.get("balance"), d.get("current_price")))
print("pipeline_active=%s" % d.get("closed_loop", {}).get("pipeline_active"))
pos = d.get("position", {})
print("position: %s entry=%s unrealized=%s" % (pos.get("dir"), pos.get("entry"), pos.get("unrealized")))
