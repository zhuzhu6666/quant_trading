import subprocess, json

BASE = "http://localhost:8000"
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

d = api("/api/state")
print("source:", d.get("source"))
print("equity:", d.get("equity"))
print("balance:", d.get("balance"))
print("price:", d.get("current_price"))

cl = d.get("closed_loop", {})
print("pipeline_active:", cl.get("pipeline_active"))

# Also check account API directly
d2 = api("/api/live/account")
print("\naccount API:", json.dumps(d2, default=str)[:300])

# Check bridge state
d3 = api("/api/live/status")
print("\nstatus API:", json.dumps(d3, default=str)[:500])
