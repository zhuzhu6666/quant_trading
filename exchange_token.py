import json, urllib.request, urllib.parse, os, time

# Use clash proxy
os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890"
os.environ["HTTP_PROXY"] = "http://127.0.0.1:7890"

# Get code from server log
import subprocess
log_result = subprocess.run(["grep", "-oP", "code=[a-f0-9]+", "/home/ubuntu/quant_trading/logs/backend.log"], 
    capture_output=True, text=True, cwd="/home/ubuntu/quant_trading")
if log_result.stdout:
    codes = log_result.stdout.strip().split("\n")
    code = codes[-1].replace("code=", "")
    print(f"Found code in log: {code[:30]}...")
else:
    # Try nginx/caddy access log
    import glob
    for logf in glob.glob("/var/log/caddy/*.log") + glob.glob("/var/log/nginx/*.log"):
        with open(logf) as f:
            for line in f:
                if "code=" in line and "callback" in line:
                    import re
                    m = re.search(r"code=([a-f0-9]+)", line)
                    if m:
                        code = m.group(1)
                        print(f"Found code in {logf}: {code[:30]}...")
                        break
    if not code:
        print("No code found in logs. Checking recent requests...")
        # Last resort: check if code was passed as arg
        import sys
        print("Usage: pass code as argument or ensure callback was hit")
        sys.exit(1)

env = {}
with open(".env") as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip("\"'")

data = urllib.parse.urlencode({
    "grant_type": "authorization_code",
    "code": code,
    "redirect_uri": "https://www.zhuzhu666.icu/api/ctrader/callback",
    "client_id": env["CTRADER_CLIENT_ID"],
    "client_secret": env["CTRADER_CLIENT_SECRET"],
}).encode()

req = urllib.request.Request("https://openapi.ctrader.com/apps/token", data=data, method="POST")
req.add_header("Content-Type", "application/x-www-form-urlencoded")

try:
    with urllib.request.urlopen(req, timeout=20) as resp:
        token_data = json.loads(resp.read())
    print("SUCCESS:", json.dumps(token_data, indent=2))
except urllib.request.HTTPError as e:
    body = e.read().decode()
    print(f"HTTP {e.code}: {body[:500]}")
except Exception as e:
    print(f"Error: {e}")
