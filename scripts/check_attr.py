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

d = api("/api/v4/stats")
print("status:", d.get("status"))
s = d.get("summary", {})
print("n_factors:", s.get("n_factors_attributed"))
print("total_trades:", s.get("total_trades"))
print("win_rate:", s.get("overall_win_rate"))
print("sharpe:", s.get("avg_sharpe_across_factors"))
print("per_factor count:", len(d.get("per_factor", {})))
