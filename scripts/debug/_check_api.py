import urllib.request, json

# Login
req = urllib.request.Request("http://localhost:8000/api/auth/login",
    data=json.dumps({"username":"zhu","password":"anything"}).encode(),
    headers={"Content-Type": "application/json"})
resp = urllib.request.urlopen(req)
token = json.loads(resp.read())["token"]
print("Got token:", token[:30], "...")

# Check status
req2 = urllib.request.Request("http://localhost:8000/api/live/status",
    headers={"Authorization": f"Bearer {token}"})
resp2 = urllib.request.urlopen(req2)
print("Status:", json.dumps(json.loads(resp2.read()), indent=2, ensure_ascii=False)[:500])

# Check account
req3 = urllib.request.Request("http://localhost:8000/api/live/account",
    headers={"Authorization": f"Bearer {token}"})
try:
    resp3 = urllib.request.urlopen(req3)
    data = json.loads(resp3.read())
    print("\nAccount:", json.dumps(data, indent=2, ensure_ascii=False)[:500])
except Exception as e:
    print(f"Account error: {e}")
    if hasattr(e, 'read'):
        print(e.read().decode())
