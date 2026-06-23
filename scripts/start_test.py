import subprocess, json, time

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
auth = "Bearer " + token

def api(path, method="GET", body=None):
    args = ["curl", "-s", BASE + path, "-H", "Authorization: " + auth]
    if method == "POST":
        args += ["-X", "POST", "-H", "Content-Type: application/json", "-d", body or "{}"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

# Start
print("START:", api("/api/live/start", "POST", "{}").get("ok"))

# Wait
for i in [3, 6, 10, 15, 20]:
    time.sleep(i if i < 10 else 5)
    d = api("/api/state")
    eq = d.get("equity", 0) or 0
    bal = d.get("balance", 0) or 0
    pr = d.get("current_price", 0) or 0
    print("t=%ds: eq=%s bal=%s price=%s src=%s pipe=%s" % (
        i + (0 if i < 10 else (i-10)*2), eq, bal, pr,
        d.get("source"), d.get("closed_loop", {}).get("pipeline_active")))
    if eq > 0:
        break

# Final
d = api("/api/state")
print("\nFINAL: eq=%s bal=%s price=%s" % (d.get("equity"), d.get("balance"), d.get("current_price")))
