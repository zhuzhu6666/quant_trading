import subprocess, json

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
if not token:
    print("LOGIN FAILED")
    exit(1)

auth = "Bearer " + token

def api(path):
    args = ["curl", "-s", BASE + path, "-H", "Authorization: " + auth]
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

d = api("/api/state")
print("source:", d.get("source"))
print("equity:", d.get("equity"))
print("balance:", d.get("balance"))
print("price:", d.get("current_price"))
print("pipeline_active:", d.get("closed_loop", {}).get("pipeline_active"))

eq = d.get("equity", 0) or 0
bal = d.get("balance", 0) or 0
pr = d.get("current_price", 0) or 0

print()
print("equity > 0:", eq > 0, " value:", eq)
print("balance > 0:", bal > 0, " value:", bal)
print("price > 0:", pr > 0, " value:", pr)

if eq > 0 and bal > 0:
    print("DATA OK - frontend shows numbers")
else:
    print("DATA EMPTY - frontend shows dashes")
