import subprocess, json, time

BASE = "http://localhost:8000"

# Login
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
AUTH = ["-H", "Authorization: Bearer " + token]

def get_state():
    r = subprocess.run(["curl", "-s", BASE + "/api/state"] + AUTH,
        capture_output=True, text=True, timeout=10)
    return json.loads(r.stdout)

def show_state(label):
    d = get_state()
    cl = d.get("closed_loop", {})
    print(label + ":")
    print("  pipeline_active = %s, source = %s" % (cl.get("pipeline_active"), d.get("source")))
    green = ['running', 'ok', 'active', 'initialized']
    orange = ['no_data', 'cold_start', 'stale', 'waiting', 'inactive']
    for k, v in sorted(cl.get("nodes", {}).items()):
        s = v.get("status", "?")
        if s in green:       icon = "GREEN 正常"
        elif s in orange:    icon = "ORANGE 待机"
        else:                icon = "RED/GRAY"
        print("  %-22s %-12s -> %s" % (k, s, icon))
    return d

# 1. Current
show_state("1. 当前状态")

# 2. START
print("\n2. POST /api/live/start (body={})")
r = subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/live/start",
    "-H", "Content-Type: application/json", "-d", "{}"] + AUTH,
    capture_output=True, text=True, timeout=30)
print("  => " + r.stdout.strip())

time.sleep(3)
show_state("   after START")

# 3. STOP
print("\n3. POST /api/live/stop")
r = subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/live/stop"] + AUTH,
    capture_output=True, text=True, timeout=30)
print("  => " + r.stdout.strip())

time.sleep(2)
show_state("   after STOP")
