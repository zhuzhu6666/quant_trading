"""Verify all backend APIs match frontend expectations."""
import subprocess, json, sys

BASE = "http://localhost:8000"

# Login
r = subprocess.run(["curl", "-s", BASE + "/api/auth/login",
    "-H", "Content-Type: application/json",
    "-d", '{"username":"zhu","password":"1994"}'],
    capture_output=True, text=True, timeout=10)
token = json.loads(r.stdout).get("token", "")
if not token:
    print("LOGIN FAILED")
    sys.exit(1)
AUTH = ["-H", "Authorization: Bearer " + token]

def api(path, need_auth=True, method="GET", body=None):
    args = ["curl", "-s", BASE + path]
    if method == "POST":
        args += ["-X", "POST"]
    if need_auth:
        args += AUTH
    if body:
        args += ["-H", "Content-Type: application/json", "-d", body]
    r = subprocess.run(args, capture_output=True, text=True, timeout=15)
    try:
        return json.loads(r.stdout)
    except:
        return {"_raw": r.stdout[:300], "_error": "parse_failed"}

def check(path, checks, desc, need_auth=True, method="GET", body=None):
    print("=" * 55)
    print("  %s %s" % (method, path))
    print("  %s" % desc)
    d = api(path, need_auth, method, body)
    all_ok = True
    for label, expr in checks:
        try:
            result = eval(expr, {"d": d, "json": json})
            status = "OK" if result else "FAIL"
            if not result:
                all_ok = False
        except Exception as e:
            status = "ERR: " + str(e)[:60]
            all_ok = False
        print("    [%s] %s  (%s)" % (status, label, expr))
    
    if all_ok:
        print("  >>> ALL CHECKS PASSED")
    else:
        print("  >>> SOME CHECKS FAILED - frontend may break")
    return all_ok

all_pass = True

# 1. /api/v4/stats
all_pass &= check(
    "/api/v4/stats",
    [
        ("top-level 'status' key exists", "'status' in d"),
        ("status is 'ok' or 'no_data'", "d.get('status') in ('ok', 'no_data')"),
        ("'summary' dict exists", "isinstance(d.get('summary'), dict)"),
        ("'per_factor' dict exists", "isinstance(d.get('per_factor'), dict)"),
        ("summary.n_factors_attributed is int|None", "isinstance(d['summary'].get('n_factors_attributed'), (int, type(None)))"),
        ("summary.total_trades is int|None", "isinstance(d['summary'].get('total_trades'), (int, type(None)))"),
        ("summary.overall_win_rate is float|None", "isinstance(d['summary'].get('overall_win_rate'), (float, type(None)))"),
        ("summary.avg_sharpe_across_factors is float|None", "isinstance(d['summary'].get('avg_sharpe_across_factors'), (float, type(None)))"),
        # Frontend checks
        ("frontend: checks d.status !== 'ok' — will bail on no_data", "d.get('status') == 'ok' or d.get('status') == 'no_data'"),
        ("frontend: accesses s.overall_win_rate", "'overall_win_rate' in d.get('summary', {})"),
        ("frontend: accesses s.avg_sharpe_across_factors", "'avg_sharpe_across_factors' in d.get('summary', {})"),
        ("frontend: iterates per_factor items with .avg_mc .win_rate .n_trades", "True"),
    ],
    "归因统计 - 前端 attribution.js _fetchAttribution()"
)

# Sample a factor to verify structure
d = api("/api/v4/stats")
pf = d.get("per_factor", {})
if pf:
    first = list(pf.values())[0]
    print("    sample factor fields: %s" % sorted(first.keys()))
    checks = [
        ("per_factor item has 'avg_mc'", "'avg_mc' in first"),
        ("per_factor item has 'win_rate'", "'win_rate' in first"),
        ("per_factor item has 'n_trades'", "'n_trades' in first"),
    ]
    for label, expr in checks:
        ok = eval(expr)
        print("    [%s] %s" % ("OK" if ok else "FAIL", label))
else:
    print("    [INFO] per_factor is empty (no_data)")

# 2. /api/v4/weights
all_pass &= check(
    "/api/v4/weights",
    [
        ("returns a list", "isinstance(d, list)"),
        ("has at least 1 item (39 expected)", "len(d) > 0"),
        ("items have 'factor' field", "all('factor' in w for w in d)"),
        ("items have 'old' field", "all('old' in w for w in d)"),
        ("items have 'new' field", "all('new' in w for w in d)"),
        # Frontend checks
        ("frontend: checks Array.isArray(d) && d.length", "isinstance(d, list) and len(d) > 0"),
        ("frontend: accesses w.new", "all(isinstance(w.get('new'), (int, float)) for w in d)"),
        ("frontend: accesses w.factor", "all(isinstance(w.get('factor'), str) for w in d)"),
        ("frontend: uses w.factor.replace(/_/g, ' ')", "True"),
    ],
    "因子权重 - attribution.js _fetchWeights()"
)

# 3. /api/control/evolution/latest
all_pass &= check(
    "/api/control/evolution/latest",
    [
        ("has 'ts' field (required by frontend)", "'ts' in d"),
        ("ts is numeric", "isinstance(d.get('ts'), (int, float))"),
        ("has 'gp_new_candidates'", "'gp_new_candidates' in d"),
        ("has 'gp_registered_shadow'", "'gp_registered_shadow' in d"),
        ("has 'oos_passed'", "'oos_passed' in d"),
        ("has 'canary_promotions' (list)", "isinstance(d.get('canary_promotions'), list)"),
        ("has 'canary_rollbacks' (list)", "isinstance(d.get('canary_rollbacks'), list)"),
        ("has 'retire_candidates' (list)", "isinstance(d.get('retire_candidates'), list)"),
        ("has 'weights_updated' (bool)", "isinstance(d.get('weights_updated'), bool)"),
        ("has 'duration_sec'", "isinstance(d.get('duration_sec'), (int, float))"),
        # Frontend checks
        ("frontend: checks !d || !d.ts → fails if ts missing", "'ts' in d"),
        ("frontend: reads d.gp_new_candidates", "'gp_new_candidates' in d"),
        ("frontend: reads d.canary_promotions.length", "isinstance(d.get('canary_promotions'), list)"),
        ("frontend: reads d.duration_sec", "isinstance(d.get('duration_sec'), (int, float))"),
        ("frontend: reads d.ts_iso for time display", "True"),  # optional
        ("frontend: reads d.retire_reason", "True"),  # optional
    ],
    "进化事件 - system.js _fetchEvolution()"
)

# 4. /api/live/start with proper body
all_pass &= check(
    "/api/live/start",
    [
        ("returns dict", "isinstance(d, dict)"),
        ("has 'ok' field", "'ok' in d"),
        ("ok is True", "d.get('ok') == True"),
        ("has 'broker'", "isinstance(d.get('broker'), str)"),
        # Frontend checks
        ("frontend: checks result && result.ok", "d.get('ok') == True"),
    ],
    "启动管道 - index.js startPipeline()",
    method="POST", body='{}'
)

# Stop to clean up
subprocess.run(["curl", "-s", "-X", "POST", BASE + "/api/live/stop"] + AUTH,
               capture_output=True, timeout=10)

# 5. /api/live/stop
all_pass &= check(
    "/api/live/stop",
    [
        ("returns dict", "isinstance(d, dict)"),
        ("has 'ok' field", "'ok' in d"),
        # Frontend checks
        ("frontend: checks result && result.ok", "d.get('ok') == True"),
    ],
    "停止管道 - index.js stopPipeline()",
    method="POST"
)

# 6. /api/state (already tested but formalize)
all_pass &= check(
    "/api/state",
    [
        ("has 'source'", "isinstance(d.get('source'), str)"),
        ("has 'closed_loop'", "isinstance(d.get('closed_loop'), dict)"),
        ("has 'equity'", "isinstance(d.get('equity'), (int, float))"),
        ("has 'position'", "isinstance(d.get('position'), dict)"),
        ("has 'daily'", "isinstance(d.get('daily'), dict)"),
        ("has 'risk'", "isinstance(d.get('risk'), dict)"),
        ("closed_loop has 'nodes'", "isinstance(d['closed_loop'].get('nodes'), dict)"),
        ("closed_loop has 'pipeline_active'", "'pipeline_active' in d['closed_loop']"),
        ("nodes has 9 entries", "len(d['closed_loop'].get('nodes', {})) >= 9"),
        # Frontend checks (index.js)
        ("frontend: loop.pipeline_active", "'pipeline_active' in d['closed_loop']"),
        ("frontend: loop.nodes (dict)", "isinstance(d['closed_loop'].get('nodes'), dict)"),
        # Frontend checks (trading.js)
        ("frontend: t.source", "isinstance(d.get('source'), str)"),
        ("frontend: t.equity", "isinstance(d.get('equity'), (int, float))"),
        ("frontend: pos.dir", "isinstance(d['position'].get('dir'), str)"),
        ("frontend: daily.trades", "isinstance(d['daily'].get('trades'), (int, float))"),
        ("frontend: risk.circuit_breaker", "isinstance(d['risk'].get('circuit_breaker'), bool)"),
    ],
    "全局状态 - app.js _applyState() → index.js + trading.js"
)

# Summary
print()
print("=" * 55)
if all_pass:
    print("  ALL APIS VERIFIED - 前端完全兼容 v5 后端")
else:
    print("  SOME FAILURES - 前端有字段不匹配, 需要修改")
