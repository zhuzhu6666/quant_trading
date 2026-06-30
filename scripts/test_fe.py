import subprocess, json

BASE = "http://localhost:8000"
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
t = json.loads(r.stdout).get("token", "")
if not t:
    print("LOGIN FAILED")
    exit(1)
h = "Authorization: Bearer " + t

def api(path, need_auth=True):
    args = ["curl", "-s", BASE + path]
    if need_auth:
        args += ["-H", h]
    r = subprocess.run(args, capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

print("=" * 50)
print("1. /api/state")
print("=" * 50)
d = api("/api/state")
print("source:", d.get("source"))
print("equity:", d.get("equity"))
print("balance:", d.get("balance"))
print("pnl_today:", d.get("pnl_today"))
print("price:", d.get("current_price"))
pos = d.get("position", {})
print("position:", pos.get("dir"), "entry:", pos.get("entry"))
daily = d.get("daily", {})
print("daily:", daily.get("trades"), "trades, win:", daily.get("win"))
risk = d.get("risk", {})
print("risk cb:", risk.get("circuit_breaker"))
cl = d.get("closed_loop", {})
print("pipeline_active:", cl.get("pipeline_active"))
nodes = cl.get("nodes", {})
green = ['running','ok','active','initialized']
for k, v in sorted(nodes.items()):
    s = v.get("status", "?")
    icon = "GREEN" if s in green else "ORANGE" if s in ['no_data','cold_start','stale','waiting','inactive'] else "RED"
    print("  %-22s %-12s [%s]" % (k, s, icon))

print()
print("=" * 50)
print("2. 前端字段检查")
print("=" * 50)
results = [
    ("pipeline_active存在", "pipeline_active" in cl),
    ("nodes >= 9", len(nodes) >= 9),
    ("source字段 str", isinstance(d.get("source"), str)),
    ("equity 数字", isinstance(d.get("equity"), (int, float))),
    ("balance 数字", isinstance(d.get("balance"), (int, float))),
    ("position.dir str", isinstance(pos.get("dir"), str)),
    ("daily.trades 数字", isinstance(daily.get("trades"), (int, float))),
    ("risk.cb bool", isinstance(risk.get("circuit_breaker"), bool)),
    ("★ equity > 0", d.get("equity", 0) > 0),
    ("★ balance > 0", d.get("balance", 0) > 0),
    ("★ price 不为空", d.get("current_price") is not None and d.get("current_price", 0) > 0),
]
ok = True
for label, result in results:
    s = "OK" if result else "FAIL"
    if not result: ok = False
    print("  [%s] %s" % (s, label))

print()
print(">>> ALL OK" if ok else ">>> FAILURES FOUND")
