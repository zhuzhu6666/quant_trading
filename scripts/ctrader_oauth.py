"""
scripts/ctrader_oauth.py — cTrader Open API OAuth2 完整流程 (本地化, 不走沙箱)

3 模式:
  (1) print-auth-url   — 拼 auth URL, 用户浏览器跳
  (2) listen-callback  — 起 127.0.0.1:8080 收 authCode (默认行为)
  (3) exchange         — 用 authCode 换 access_token + refresh_token, 落 .env

设计:
  - 全走本机 HTTP (requests 库), 不依赖沙箱
  - 处理 4 种错: redirect_uri_mismatch / invalid_client / invalid_grant /
    access_denied
  - 凭证源: env (CTRADER_CLIENT_ID/SECRET/REDIRECT_URI) 或 CLI 参数

用法:
  # 1) 先看 auth URL
  python scripts/ctrader_oauth.py print-auth-url

  # 2) 起本地 HTTP server 收 authCode (用户浏览器跳完后自动接)
  python scripts/ctrader_oauth.py listen-callback
  # 用户浏览器跳 https://openapi.ctrader.com/apps/auth?client_id=...&...
  # 登 cTrader ID → 授权 → 跳回 127.0.0.1:8080/callback?code=XXX
  # 脚本自动接 code → 换 token → 落 .env

  # 3) 也可手动给 authCode
  python scripts/ctrader_oauth.py exchange AUTH_CODE_HERE
"""
from __future__ import annotations

import argparse
import http.server
import json
import logging
import os
import sys
import time
import urllib.parse
from pathlib import Path
from threading import Thread

# 项目根
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env  # noqa: E402
load_env()  # 自动从 .env 读 CTRADER_*, 无需手动 export

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("ctrader_oauth")

# ── 端点 (从 ctrader_open_api.endpoints 镜像) ──────────────
AUTH_URI = "https://openapi.ctrader.com/apps/auth"
TOKEN_URI = "https://openapi.ctrader.com/apps/token"
REDIRECT_URI = os.environ.get("CTRADER_REDIRECT_URI", "http://127.0.0.1:8080/callback")
CALLBACK_PORT = int(urllib.parse.urlparse(REDIRECT_URI).port or 8080)
CALLBACK_PATH = urllib.parse.urlparse(REDIRECT_URI).path or "/callback"

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"


# ── 凭证读取 ────────────────────────────────────────────

def get_creds():
    """读 CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET; 若 .env 没生成, 报明确错并提示 bootstrap"""
    cid = os.environ.get("CTRADER_CLIENT_ID", "").strip()
    sec = os.environ.get("CTRADER_CLIENT_SECRET", "").strip()
    if not cid or not sec:
        # 首次 bootstrap: .env 不存在, 提醒用户写一个
        log.error("CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET 未设 (env 也没有 .env)")
        log.error("首次跑需先在项目根写 .env (一行一对):")
        log.error("  CTRADER_CLIENT_ID=27394_REAHuZKx8ImKjcqa7XN4DoySzmxyakuaNaBbSjqIIWwyEMRCtH")
        log.error("  CTRADER_CLIENT_SECRET=pB0QaHI667DhFHntiXPJrSZ2DTZd82IDBlfgMSZGlwGXHxbcEV")
        log.error("  CTRADER_REDIRECT_URI=http://127.0.0.1:8080/callback")
        log.error("保存后重跑本脚本")
        sys.exit(1)
    return cid, sec


def build_auth_url(scope: str = "trading") -> str:
    cid, _ = get_creds()
    url = (f"{AUTH_URI}"
           f"?client_id={urllib.parse.quote(cid)}"
           f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
           f"&scope={scope}")
    return url


# ── Token 端点 ────────────────────────────────────────────

def exchange_code(auth_code: str) -> dict:
    """authCode → access_token + refresh_token, 写 .env"""
    import requests
    cid, sec = get_creds()
    log.info(f"POST {TOKEN_URI} (grant_type=authorization_code)")
    r = requests.get(TOKEN_URI, params={
        "grant_type": "authorization_code",
        "code": auth_code,
        "redirect_uri": REDIRECT_URI,
        "client_id": cid,
        "client_secret": sec,
    }, timeout=15)
    return _handle_token_response(r)


def refresh_access_token(refresh_token: str) -> dict:
    """refresh_token → 新 access_token (refresh_token 可能也轮换)"""
    import requests
    cid, sec = get_creds()
    log.info(f"POST {TOKEN_URI} (grant_type=refresh_token)")
    r = requests.get(TOKEN_URI, params={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": cid,
        "client_secret": sec,
    }, timeout=15)
    return _handle_token_response(r)


def _handle_token_response(r) -> dict:
    """解 cTrader OAuth 响应: 成功 200+JSON, 失败可能是 JSON error 也可能 HTML"""
    if r.status_code != 200:
        log.error(f"HTTP {r.status_code}: {r.text[:500]}")
        return {"error": f"http_{r.status_code}", "raw": r.text}
    try:
        data = r.json()
    except Exception as e:
        log.error(f"JSON decode failed: {e}; body={r.text[:300]}")
        return {"error": "json_decode", "raw": r.text}
    if "error" in data:
        err = data.get("error")
        desc = data.get("error_description", "")
        log.error(f"OAuth error: {err} — {desc}")
        return data
    if "access_token" not in data:
        log.error(f"No access_token in response: {data}")
        return {"error": "no_access_token", "raw": data}
    # 成功
    log.info(f"✓ Got access_token (len={len(data['access_token'])}), "
             f"refresh_token (len={len(data.get('refresh_token', ''))})")
    if "expires_in" in data:
        log.info(f"  expires_in = {data['expires_in']}s "
                 f"({data['expires_in']/3600:.1f}h)")
    _write_env(data)
    return data


def _write_env(data: dict):
    """access_token + refresh_token 落 .env (覆盖已有, 不入 git)"""
    access = data["access_token"]
    refresh = data.get("refresh_token", "")
    expires_in = data.get("expires_in", 0)

    # 读现有 .env
    existing = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            existing[k.strip()] = v.strip()

    # 覆盖
    existing["CTRADER_ACCESS_TOKEN"] = access
    if refresh:
        existing["CTRADER_REFRESH_TOKEN"] = refresh
    if expires_in:
        existing["CTRADER_TOKEN_EXPIRES_AT"] = str(int(time.time()) + int(expires_in))
    existing["CTRADER_REDIRECT_URI"] = REDIRECT_URI  # 顺手记

    # 写回
    lines = [f"{k}={v}" for k, v in existing.items()]
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log.info(f"✓ Wrote {ENV_FILE} ({len(existing)} keys)")
    # 提醒 .gitignore
    gitignore = Path(__file__).resolve().parent.parent / ".gitignore"
    if gitignore.exists():
        gi = gitignore.read_text(encoding="utf-8")
        if ".env" not in gi:
            log.warning("⚠ .env 不在 .gitignore; 建议加一行 '.env' 防止凭证泄漏")


# ── 本地 HTTP server 收 authCode ──────────────────────────

class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """单请求 server, 收 ?code=... 立刻 shutdown"""
    auth_code_holder = {"code": None, "error": None}

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        qs = urllib.parse.parse_qs(parsed.query)
        if parsed.path != CALLBACK_PATH:
            self.send_response(404)
            self.end_headers()
            return
        if "error" in qs:
            err = qs["error"][0]
            desc = qs.get("error_description", [""])[0]
            self._reply_page(f"<h1>❌ OAuth 失败</h1><p>{err}: {desc}</p>"
                            f"<p>回到终端看详细日志。</p>", status=400)
            _CallbackHandler.auth_code_holder["error"] = err
            return
        if "code" in qs:
            code = qs["code"][0]
            _CallbackHandler.auth_code_holder["code"] = code
            self._reply_page(f"<h1>✓ AuthCode 收到</h1>"
                            f"<p>code = <code>{code[:30]}...</code></p>"
                            f"<p>关闭此页, 回到终端看 token 落盘结果。</p>")
            return
        self._reply_page("<h1>⚠ 无 code 也没 error</h1>", status=400)

    def _reply_page(self, body: str, status: int = 200):
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode("utf-8"))

    def log_message(self, fmt, *args):
        # 静默默认 access log
        pass


def listen_for_callback(timeout: int = 120) -> str | None:
    """
    起 127.0.0.1:CALLBACK_PORT server 等 ?code=... 跳回.
    返回 authCode, 失败返回 None.
    """
    server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    server.timeout = 5  # 短轮询, 便于优雅 shutdown
    log.info(f"Listening on http://127.0.0.1:{CALLBACK_PORT}{CALLBACK_PATH} "
             f"(timeout={timeout}s)")

    deadline = time.time() + timeout
    while time.time() < deadline:
        server.handle_request()
        if _CallbackHandler.auth_code_holder["code"]:
            return _CallbackHandler.auth_code_holder["code"]
        if _CallbackHandler.auth_code_holder["error"]:
            log.error(f"OAuth 失败: {_CallbackHandler.auth_code_holder['error']}")
            return None
    log.error("Timeout, 没人跳 callback")
    return None


# ── CLI 入口 ─────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["print-auth-url", "listen-callback", "exchange", "refresh", "validate"],
                   help="print-auth-url: 只拼 URL; listen-callback: 起 server 收 code 自动 exchange; "
                        "exchange: 手动给 authCode; refresh: 用 refresh_token 续期; "
                        "validate: 用已知 invalid code 测 client credentials 是否配对 (无浏览器)")
    p.add_argument("code", nargs="?", help="authCode (exchange 模式用)")
    p.add_argument("--scope", default="trading", help="OAuth scope, 默认 trading")
    p.add_argument("--timeout", type=int, default=120, help="listen-callback 超时秒")
    args = p.parse_args()

    if args.mode == "print-auth-url":
        url = build_auth_url(args.scope)
        print("=" * 70)
        print("  浏览器跳这个 URL (cTrader ID 登录 → 授权 → 跳回 127.0.0.1:8080/callback?code=...)")
        print("=" * 70)
        print(url)
        print("=" * 70)
        return

    if args.mode == "listen-callback":
        url = build_auth_url(args.scope)
        log.info("=" * 70)
        log.info("  Step 1: 浏览器跳这个 URL")
        log.info("=" * 70)
        log.info(url)
        log.info("=" * 70)
        log.info("  Step 2: 登 cTrader ID → 授权 application → 跳回 callback")
        log.info(f"  Step 3: 本脚本自动接 code → exchange token → 落 .env")
        log.info("=" * 70)
        # 试着帮用户自动开浏览器
        try:
            import webbrowser
            log.info("  [auto] 试着开浏览器...")
            webbrowser.open(url)
        except Exception as e:
            log.warning(f"  webbrowser 失败: {e}; 手动复制上面 URL")
        # 起 server 等
        code = listen_for_callback(timeout=args.timeout)
        if not code:
            sys.exit(1)
        log.info(f"Got authCode: {code[:20]}...")
        result = exchange_code(code)
        if "access_token" in result:
            log.info("✓ Done. 现在可以跑 python scripts/ctrader_poc.py")
        else:
            sys.exit(1)
        return

    if args.mode == "exchange":
        if not args.code:
            log.error("exchange 模式需要 authCode 作为参数")
            sys.exit(1)
        result = exchange_code(args.code)
        if "access_token" not in result:
            sys.exit(1)
        return

    if args.mode == "refresh":
        rt = os.environ.get("CTRADER_REFRESH_TOKEN", "").strip()
        if not rt:
            log.error("CTRADER_REFRESH_TOKEN 未设")
            sys.exit(1)
        result = refresh_access_token(rt)
        if "access_token" not in result:
            sys.exit(1)
        return

    if args.mode == "validate":
        # 拿已知 invalid code 测 client credentials, 不跳浏览器
        # cTrader server 会回 'invalid_grant' (code 错) 或 'Client credentials invalid' (secret 错)
        # 这俩错能区分是 secret 配对问题, 还是 code 过期问题
        import requests
        cid, sec = get_creds()
        log.info(f"validate: client_id={cid[:10]}...{cid[-4:]} (len={len(cid)})")
        log.info(f"validate: client_secret=...{sec[-4:]} (len={len(sec)})")
        log.info("POST apps/token (intentionally invalid code='validate-test')")
        r = requests.get(TOKEN_URI, params={
            "grant_type": "authorization_code",
            "code": "validate-test",
            "redirect_uri": REDIRECT_URI,
            "client_id": cid,
            "client_secret": sec,
        }, timeout=15)
        log.info(f"HTTP {r.status_code}")
        log.info(f"body: {r.text[:500]}")
        try:
            data = r.json()
        except Exception:
            log.error("非 JSON 响应, 看上面 body")
            return
        err = data.get("errorCode") or data.get("error") or ""
        desc = data.get("description") or data.get("error_description") or ""
        if "invalid_grant" in err.lower() or "invalid_grant" in desc.lower():
            log.info("✓ client_id + client_secret 配对 OK (server 拒的是 code, 不是 credentials)")
        elif "Client credentials invalid" in desc or "invalid_client" in err.lower():
            log.error("✗ client_id 跟 client_secret 不配对, 或 application 未激活")
            log.error("  → 回去 cTrader ID Portal 重置 client secret")
        else:
            log.warning(f"未预期响应: {err} / {desc}")
        return


if __name__ == "__main__":
    main()
