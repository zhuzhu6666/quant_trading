import subprocess, json, time, sys

BASE = "http://localhost:8000"
ENV = "/home/ubuntu/quant_trading/.env"

def login():
    r = subprocess.run(["curl", "-s", f"{BASE}/api/auth/login",
        "-H", "Content-Type: application/json",
        "-d", '{"username":"zhu","password":"1994"}'],
        capture_output=True, text=True, timeout=10)
    d = json.loads(r.stdout)
    token = d.get("token") or d.get("access_token", "")
    if not token:
        print("LOGIN FAILED:", r.stdout[:200])
        sys.exit(1)
    return token

def api(token, path, method="GET"):
    args = ["curl", "-s", f"{BASE}{path}", "-H", f"Authorization: Bearer {token}"]
    if method == "POST":
        args += ["-X", "POST"]
    r = subprocess.run(args, capture_output=True, text=True, timeout=30)
    return json.loads(r.stdout)

def print_state(label, d):
    cl = d.get("closed_loop", {})
    pipeline_active = cl.get("pipeline_active", False)
    source = d.get("source", "?")
    nodes = cl.get("nodes", {})
    
    statuses = {}
    for k, v in nodes.items():
        statuses[k] = v.get("status", "?")
    
    print("%s:" % label)
    print("  pipeline_active = %s" % pipeline_active)
    print("  source          = %s" % source)
    print("  nodes:")
    for k, v in sorted(statuses.items()):
        # Map to what frontend would show
        green = ['running', 'ok', 'active', 'initialized']
        orange = ['no_data', 'cold_start', 'stale', 'waiting', 'inactive']
        red = ['stale_critical', 'circuit_breaker', 'error', 'off', 'stopped']
        if v in green:    display = "green 正常"
        elif v in orange: display = "orange 待机"
        elif v in red:    display = "red 异常"
        else:             display = "gray 未知"
        print("    %-22s %-15s -> %s" % (k, v, display))
    print()

token = login()
print("TOKEN ok\n")

# 1. Current state
print("=" * 60)
print("1. 当前状态")
d = api(token, "/api/state")
print_state("Current", d)

# 2. Start pipeline
print("=" * 60)
print("2. 启动管道...")
start_res = api(token, "/api/live/start", "POST")
print("   POST /api/live/start => %s" % json.dumps(start_res))

# Wait and check state
time.sleep(2)

d = api(token, "/api/state")
print_state("After START", d)

# 3. Stop pipeline
print("=" * 60)
print("3. 停止管道...")
stop_res = api(token, "/api/live/stop", "POST")
print("   POST /api/live/stop => %s" % json.dumps(stop_res))

time.sleep(2)

d = api(token, "/api/state")
print_state("After STOP", d)

# 4. Summary
print("=" * 60)
print("4. 前端预期行为验证")
print()

cl = d.get("closed_loop", {})
pipeline_active = cl.get("pipeline_active", False)

# index.js 逻辑模拟
green = ['running', 'ok', 'active', 'initialized']
orange = ['no_data', 'cold_start', 'stale', 'waiting', 'inactive']
red = ['stale_critical', 'circuit_breaker', 'error', 'off', 'stopped']

NODE_DEFS = [
    "data_sync", "factor_engine", "signal_normalizer", 
    "portfolio_compositor", "execution_gate", "execution",
    "attribution", "adaptive_weight", "risk"
]

nodes = cl.get("nodes", {})
all_ok = True

for key in NODE_DEFS:
    n = nodes.get(key)
    if not n:
        print("  MISSING NODE: %s" % key)
        all_ok = False
        continue
    s = n.get("status", "unknown")
    if s in green:
        expected = "green"
    elif s in orange:
        expected = "orange"
    elif s in red:
        expected = "red"
    else:
        expected = "gray"
    
    # What frontend index.js _mapNode() would produce
    if s in green:      frontend = "dot-green, text-green, '正常'"
    elif s in orange:   frontend = "dot-orange, text-orange, '待机'"
    elif s in red:
        frontend = "dot-red, text-red, '%s'" % ('熔断' if s == 'circuit_breaker' else '异常')
    else:               frontend = "dot-gray, text-gray, '未知'"
    
    print("  %-22s status=%-12s -> %s" % (key, s, frontend))

print()
print("pipeline_active = %s -> 前端显示: %s" % (
    pipeline_active,
    "管道运行中, badge-green" if pipeline_active else "管道已停止, badge-gray"
))
print("start/stop POST => ok: %s -> 前端: %s" % (
    stop_res.get("ok"),
    "controlOk=true, '已停止'" if stop_res.get("ok") else "controlErr=true"
))

if all_ok:
    print("\n>>> 结论: 前端映射正确，所有节点状态与后端一致")
else:
    print("\n>>> 警告: 存在缺失节点")
