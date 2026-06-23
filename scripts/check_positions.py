import subprocess, json

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
auth = "Bearer " + token

r = subprocess.run(["curl", "-s", BASE + "/api/live/positions", "-H", "Authorization: " + auth],
    capture_output=True, text=True, timeout=10)
d = json.loads(r.stdout)
print(json.dumps(d, indent=2, default=str)[:2000])
