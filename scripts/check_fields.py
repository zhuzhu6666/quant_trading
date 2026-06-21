import subprocess, json

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
auth_header = "Authorization: Bearer *** + token
AUTH=*** auth_header]

def api(path, method="GET", body=None):
    args = ["curl", "-s", BASE + path] + AUTH
    if method == "POST":
        args.append("-X")
        args.append("POST")
    if body:
        args.extend(["-H", "Content-Type: application/json", "-d", body])
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    return json.loads(r.stdout)

# 1. /api/v4/stats
d = api("/api/v4/stats")
print("=== /api/v4/stats ===")
print("status: " + str(d.get("status")))
s = d.get("summary", {})
print("summary keys: " + str(sorted(s.keys())))
for k, v in sorted(s.items()):
    print("  %s: %s" % (k, v))
print()

# 2. weights
d = api("/api/v4/weights")
print("=== weights first 2 ===")
for w in d[:2]:
    print("  keys: " + str(sorted(w.keys())))
    print("  " + json.dumps(w, default=str))
print()

# 3. stop then start
d = api("/api/live/stop", "POST")
print("=== STOP: " + str(d.get("ok")) + " ===")

d = api("/api/live/start", "POST", "{}")
print("=== START: ok=" + str(d.get("ok")) + " ===")
print(json.dumps(d, indent=2, default=str))
