"""
scripts/validate_ctrader_token.py — 验证 CTRADER_ACCESS_TOKEN 是否还有效

方法: 单次 TCP+TLS 连接,发 ProtoOAGetAccountListByAccessTokenReq
(官方用途 = 拿 access token 关联的账户列表; server 拒绝即 token 失效)。
只读探针,不发任何交易请求。

修复说明 (2026-08-19):
  旧版在一次进程里对同一个全局 Twisted reactor 多次调用 reactor.run()
  (main() 起一次 + run_no_auth_test() 又起),Twisted reactor 只能 run 一次,
  第二次直接抛 "Can't restart reactor"。这里改为单 reactor + 单 client,
  在后台线程 run 一次、全程复用,结束后 stopService 并正常退出。

用法:
  CTRADER_CLIENT_ID=... CTRADER_CLIENT_SECRET=... CTRADER_ACCESS_TOKEN=... \\
     python scripts/validate_ctrader_token.py
  exit 0 = token 有效(拿到账户列表); 2 = token 被拒/连接失败/环境缺失。
"""
import os
import sys
import time
import logging
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env

load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("validate_token")

from ctrader_open_api import Client, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import (
    ProtoOAApplicationAuthReq,
    ProtoOAGetAccountListByAccessTokenReq,
)
from ctrader_open_api.protobuf import Protobuf

HOST = os.environ.get("CTRADER_HOST", "demo.ctraderapi.com")
PORT = 5035
TIMEOUT = 12.0


class TokenProbe:
    def __init__(self, host: str, port: int, timeout: float = TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout
        self.client = Client(host, port, TcpProtocol)
        self.result = {}  # 单消息结果槽
        self._done = False

    def on_connected(self, _c):
        log.info("[probe] TCP connected")

    def on_disconnected(self, _c, reason):
        log.info(f"[probe] disconnected: {type(reason.value).__name__}: {reason.value}")

    def on_message(self, _c, message):
        try:
            payload = Protobuf.extract(message)
        except Exception as e:  # noqa: BLE001 探针只记录
            log.error(f"[probe] extract failed: {e!r}")
            self.result = {"error": repr(e)}
            return
        name = type(payload).__name__
        log.info(f"[probe] received: {name}")
        self.result = {"type": name, "payload": payload}
        if name in (
            "ProtoOAGetAccountListByAccessTokenRes",
            "ProtoOAErrorRes",
            "ProtoOAVersionRes",
        ):
            from twisted.internet import reactor
            reactor.callFromThread(reactor.stop)

    def start(self):
        from twisted.internet import reactor
        from threading import Thread

        self.client.setConnectedCallback(self.on_connected)
        self.client.setDisconnectedCallback(self.on_disconnected)
        self.client.setMessageReceivedCallback(self.on_message)
        self.client.startService()
        t = Thread(target=lambda: reactor.run(installSignalHandlers=False), daemon=True)
        t.start()
        return t

    def wait_connected(self):
        t0 = time.time()
        while not self.client.isConnected and time.time() - t0 < self.timeout:
            time.sleep(0.1)
        return self.client.isConnected

    def send_and_wait(self, message, timeout=None):
        """send + 等 result 就位(polling, 同主线程),返回 payload 或 None。"""
        from twisted.internet import reactor
        # 重置结果槽,等待 on_message 填
        self.result = {}
        self.client.send(message, clientMsgId=str(uuid.uuid4()), responseTimeoutInSeconds=timeout or self.timeout)
        t0 = time.time()
        while not self.result and time.time() - t0 < (timeout or self.timeout):
            time.sleep(0.05)
        return self.result.get("payload") if self.result else None

    def stop(self):
        try:
            self.client.stopService()
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
    access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "")

    if not (client_id and client_secret and access_token):
        log.error("缺少环境变量 CTRADER_CLIENT_ID / CTRADER_CLIENT_SECRET / CTRADER_ACCESS_TOKEN")
        return 2
    log.info(f"client_id: {client_id[:10]}...{client_id[-4:]} (masked)")
    log.info(f"access_token: {access_token[:10]}...{access_token[-4:]} len={len(access_token)}")
    log.info(f"target: {HOST}:{PORT}")

    probe = TokenProbe(HOST, PORT)
    probe.start()

    if not probe.wait_connected():
        log.error(f"TCP connect timeout ({TIMEOUT}s)")
        probe.stop()
        return 2
    log.info("TCP connected ✓")

    # 1) App auth (client secret 验证)
    auth = ProtoOAApplicationAuthReq()
    auth.clientId = client_id
    auth.clientSecret = client_secret
    log.info("sending ProtoOAApplicationAuthReq (app auth)...")
    res = probe.send_and_wait(auth, timeout=TIMEOUT)
    if res is None:
        log.error("app auth: 无响应(超时)")
        probe.stop()
        return 2
    if type(res).__name__ == "ProtoOAErrorRes":
        log.error(f"app auth REJECTED: errorCode={res.errorCode} description={res.description!r}")
        probe.stop()
        return 2
    log.info("app auth OK ✓")

    # 2) Token 验证
    tok = ProtoOAGetAccountListByAccessTokenReq()
    tok.accessToken = access_token
    log.info("sending ProtoOAGetAccountListByAccessTokenReq (token check)...")
    res = probe.send_and_wait(tok, timeout=TIMEOUT)
    if res is None:
        log.error("token check: 无响应(超时)")
        probe.stop()
        return 2
    if type(res).__name__ == "ProtoOAErrorRes":
        log.error(f"Token REJECTED: errorCode={res.errorCode} description={res.description!r}")
        probe.stop()
        return 2
    if type(res).__name__ == "ProtoOAGetAccountListByAccessTokenRes":
        accounts = list(res.ctidTraderAccount)
        log.info(f"Token VALID ✓ ctidTraderAccount count={len(accounts)}")
        for a in accounts[:5]:
            # 字段名跨版本有差异(旧 ProtoOACtidTraderAccount 无 brokerTitle),安全打印
            acc_id = getattr(a, "ctidTraderAccountId", getattr(a, "id", "?"))
            is_live = bool(getattr(a, "isLive", False))
            broker = getattr(a, "brokerTitle", None)
            broker_str = f" brokerTitle={broker!r}" if broker is not None else ""
            log.info(f"  account: id={acc_id} isLive={is_live}{broker_str}")
        probe.stop()
        return 0
    log.warning(f"unexpected response type: {type(res).__name__}")
    probe.stop()
    return 2


if __name__ == "__main__":
    sys.exit(main())
