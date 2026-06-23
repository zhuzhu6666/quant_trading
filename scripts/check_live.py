import subprocess, json

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
h = "Authorization: Bearer *** + token
AUTH=*** 
def api(path):
    r = subprocess.run(["curl", "-s", BASE + path, "-H", h],
        capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

d = api("/api/state")
print("=== STATE ===")
print("source:", d.get("source"))
print("equity:", d.get("equity"))
print("balance:", d.get("balance"))
print("pnl_today:", d.get("pnl_today"))
print("current_price:", d.get("current_price"))
print("position:", d.get("position"))
print("n_positions:", d.get("n_positions"))
cl = d.get("closed_loop", {})
print("pipeline_active:", cl.get("pipeline_active"))
for k, v in sorted(cl.get("nodes", {}).items()):
    print("  %s: %s" % (k, v.get("status")))

d = api("/api/live/loop-status")
print("\n=== LOOP STATUS ===")
print(json.dumps(d, indent=2, default=str))

d = api("/api/live/account")
print("\n=== ACCOUNT ===")
print(json.dumps(d, indent=2, default=str))
