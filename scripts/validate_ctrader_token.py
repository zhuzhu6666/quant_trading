"""
scripts/validate_ctrader_token.py — 验证 CTRADER_ACCESS_TOKEN 是否还有效

方法: 不走完整 connect() 流程,直接发一个 ProtoOAGetAccountListByAccessTokenReq
(这个 Req 的官方目的就是 "拿 access token 关联的账户列表",server 拒就说明 token 失效)

注意: 这是只读探针,不发任何交易
"""
import sys
import time
import logging
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

HOST = "demo.ctraderapi.com"
PORT = 5035
TIMEOUT = 10.0


def main():
    import os
    client_id = os.environ.get("CTRADER_CLIENT_ID", "")
    client_secret = os.environ.get("CTRADER_CLIENT_SECRET", "")
    access_token = os.environ.get("CTRADER_ACCESS_TOKEN", "")

    log.info(f"client_id: {client_id[:10]}...{client_id[-4:]} (masked)")
    log.info(f"access_token: {access_token[:10]}...{access_token[-4:]} len={len(access_token)}")

    # ── TEST A: ProtoOAVersionReq on DEMO host (无认证,纯握手) ──
    log.info(f"=== TEST A: ProtoOAVersionReq on DEMO {HOST}:{PORT} (无认证) ===")
    test_a_result = run_no_auth_test(HOST, PORT, client_id, client_secret)

    # ── TEST B: ProtoOAVersionReq on LIVE host (无认证,纯握手) ──
    log.info(f"=== TEST B: ProtoOAVersionReq on LIVE live.ctraderapi.com:5035 (无认证) ===")
    test_b_result = run_no_auth_test("live.ctraderapi.com", 5035, client_id, client_secret)

    # 起 reactor (跟 ctrader_bridge.py 一样的模式)
    from twisted.internet import reactor, defer
    from threading import Thread

    client = Client(HOST, PORT, TcpProtocol)
    results = {}

    def on_connected(c):
        log.info("[twisted] connected")

    def on_disconnected(c, reason):
        log.info(f"[twisted] disconnected: {reason}")

    def on_msg_received(c, message):
        # 关键 debug: 看 wire message 跟 Protobuf.extract 解出来是不是一致
        log.info(f"[twisted] raw ProtoMessage: payloadType={message.payloadType} clientMsgId={message.clientMsgId}")
        try:
            payload = Protobuf.extract(message)
        except Exception as e:
            log.error(f"[twisted] Protobuf.extract FAILED: {e!r} (raw payloadType={message.payloadType})")
            results["extract_error"] = repr(e)
            results["raw_payloadType"] = message.payloadType
            reactor.callFromThread(reactor.stop)
            return
        name = type(payload).__name__
        log.info(f"[twisted] extracted: {name}  attrs={[f for f in dir(payload) if not f.startswith('_')][:15]}")
        # 把所有 string 字段都打出来,看 errorCode / description 实际叫什么
        if name == "ProtoOAErrorRes":
            log.info(f"  errorCode  attr exists: {hasattr(payload,'errorCode')}  val={getattr(payload,'errorCode','<none>')!r}")
            log.info(f"  error_code attr exists: {hasattr(payload,'error_code')} val={getattr(payload,'error_code','<none>')!r}")
            log.info(f"  description attr exists: {hasattr(payload,'description')} val={getattr(payload,'description','<none>')!r}")
            log.info(f"  desc attr exists: {hasattr(payload,'desc')} val={getattr(payload,'desc','<none>')!r}")
            log.info(f"  ALL fields: {payload.DESCRIPTOR.fields_by_name.keys()}")
            # Dump EVERY field
            for fname in payload.DESCRIPTOR.fields_by_name:
                fval = getattr(payload, fname, None)
                if fval not in (None, "", 0, [], b""):
                    log.info(f"  field[{fname}] = {fval!r}")
            # SerializeToString 看 raw bytes
            raw = payload.SerializeToString()
            log.info(f"  raw bytes (hex): {raw.hex()}")
            log.info(f"  raw bytes (len={len(raw)})")
        results.setdefault("msgs", []).append(name)
        if name in ("ProtoOAGetAccountListByAccessTokenRes", "ProtoOAErrorRes"):
            reactor.callFromThread(reactor.stop)

    client.setConnectedCallback(on_connected)
    client.setDisconnectedCallback(on_disconnected)
    client.setMessageReceivedCallback(on_msg_received)
    client.startService()

    t = Thread(target=lambda: reactor.run(installSignalHandlers=False), daemon=True)
    t.start()

    # 等连接
    t0 = time.time()
    while not client.isConnected and time.time() - t0 < TIMEOUT:
        time.sleep(0.1)
    if not client.isConnected:
        log.error(f"TCP connect timeout ({time.time()-t0:.1f}s)")
        return
    log.info(f"TCP connected in {time.time()-t0:.1f}s")

    # App auth — TEST 1: 正确 clientId + 故意错 clientSecret
    # 看 server 对"secret 错"和"client 错"是不是报同样的错
    t0 = time.time()
    auth = ProtoOAApplicationAuthReq()
    auth.clientId = client_id
    auth.clientSecret = "TOTALLY_WRONG_SECRET_xxxx"  # 故意错
    import uuid
    test_msg_id = str(uuid.uuid4())
    log.info(f"=== TEST 1: 故意错 clientSecret ===")
    log.info(f"sending ProtoOAApplicationAuthReq with WRONG clientSecret, clientMsgId={test_msg_id!r}...")
    d = client.send(auth, clientMsgId=test_msg_id, responseTimeoutInSeconds=TIMEOUT)
    res = wait_deferred(d, TIMEOUT)
    log.info(f"App auth (wrong secret) result: {type(res).__name__ if res else 'None'} (took {time.time()-t0:.1f}s)")
    # 重新连一次
    log.info("reconnecting for test 2...")
    t1 = time.time()
    while client.isConnected:
        time.sleep(0.1)
        if time.time() - t1 > 3:
            break
    if not client.isConnected:
        # Twisted reactor 还在跑但连接断了, 暂时跳过 test 2, 走现有连接
        log.warning("connection lost, skipping test 2 (need reconnect logic)")

    # App auth — TEST 2: 正确 clientId + 正确 clientSecret (重复一次, 拿 raw 错误)
    t0 = time.time()
    auth = ProtoOAApplicationAuthReq()
    auth.clientId = client_id
    auth.clientSecret = client_secret  # 用对的
    test_msg_id = str(uuid.uuid4())
    log.info(f"=== TEST 2: 正确 clientSecret ===")
    log.info(f"sending ProtoOAApplicationAuthReq with CORRECT clientSecret, clientMsgId={test_msg_id!r}...")
    d = client.send(auth, clientMsgId=test_msg_id, responseTimeoutInSeconds=TIMEOUT)
    res = wait_deferred(d, TIMEOUT)
    log.info(f"App auth result type: {type(res).__name__ if res else 'None'} (took {time.time()-t0:.1f}s)")
    if res is None:
        log.error("App auth no response — check clientSecret?")
        return
    if type(res).__name__ == "ProtoOAErrorRes":
        log.error(f"App auth REJECTED: errorCode={res.errorCode} description={res.description!r}")
        return
    log.info("App auth OK ✓")

    # Token 验证: ProtoOAGetAccountListByAccessTokenReq
    t0 = time.time()
    tok_req = ProtoOAGetAccountListByAccessTokenReq()
    tok_req.accessToken = access_token
    log.info("sending ProtoOAGetAccountListByAccessTokenReq...")
    d = client.send(tok_req, responseTimeoutInSeconds=TIMEOUT)
    res = wait_deferred(d, TIMEOUT)
    log.info(f"Token check result type: {type(res).__name__ if res else 'None'} (took {time.time()-t0:.1f}s)")
    if res is None:
        log.error("Token check no response — timeout?")
    elif type(res).__name__ == "ProtoOAErrorRes":
        log.error(f"Token REJECTED: errorCode={res.errorCode} description={res.description!r}")
    elif type(res).__name__ == "ProtoOAGetAccountListByAccessTokenRes":
        log.info(f"Token VALID ✓ ctidTraderAccount count={len(res.ctidTraderAccount)}")
        for a in res.ctidTraderAccount[:3]:
            log.info(f"  account: id={a.ctidTraderAccountId} isLive={a.isLive} brokerTitle={a.brokerTitle}")
        log.info(f"  permissionScope = {res.permissionScope}")
    else:
        log.warning(f"Unexpected response type: {type(res).__name__}")

    # cleanup
    try:
        client.stopService()
    except Exception:
        pass


def wait_deferred(d, timeout):
    """Twisted Deferred 同步等 — 简单的 polling"""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if d.called:
            if isinstance(d.result, Exception):
                log.error(f"Deferred errback: {d.result}")
                return None
            return d.result
        time.sleep(0.05)
    log.error(f"Deferred timeout after {timeout}s")
    return None


def run_no_auth_test(host, port, client_id, client_secret):
    """起新 reactor,发 ProtoOAVersionReq 看 server 响应."""
    from twisted.internet import reactor, defer
    from threading import Thread
    from ctrader_open_api import Client, TcpProtocol
    from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAVersionReq
    from ctrader_open_api.protobuf import Protobuf

    client = Client(host, port, TcpProtocol)
    received = {"payload": None, "name": None}

    def on_msg(c, message):
        try:
            payload = Protobuf.extract(message)
        except Exception as e:
            log.error(f"  extract err: {e!r} (raw payloadType={message.payloadType})")
            reactor.callFromThread(reactor.stop)
            return
        name = type(payload).__name__
        log.info(f"  received: {name}")
        if name == "ProtoOAVersionRes":
            log.info(f"  version = {getattr(payload, 'version', '?')!r}")
        elif name == "ProtoOAErrorRes":
            log.info(f"  errorCode={payload.errorCode!r} description={payload.description!r}")
        received["payload"] = payload
        received["name"] = name
        reactor.callFromThread(reactor.stop)

    def on_disc(c, reason):
        log.info(f"  disconnected: {type(reason.value).__name__}: {reason.value}")

    client.setMessageReceivedCallback(on_msg)
    client.setDisconnectedCallback(on_disc)
    client.startService()

    t = Thread(target=lambda: reactor.run(installSignalHandlers=False), daemon=True)
    t.start()

    t0 = time.time()
    while not client.isConnected and time.time() - t0 < 8:
        time.sleep(0.1)
    if not client.isConnected:
        log.error(f"  TCP connect failed in {time.time()-t0:.1f}s")
        try:
            client.stopService()
        except Exception:
            pass
        return None
    log.info(f"  TCP connected in {time.time()-t0:.1f}s")

    t0 = time.time()
    req = ProtoOAVersionReq()
    log.info(f"  sending ProtoOAVersionReq...")
    d = client.send(req, responseTimeoutInSeconds=5)
    res = wait_deferred(d, 5)
    log.info(f"  result: {received['name']} (took {time.time()-t0:.1f}s)")

    try:
        client.stopService()
    except Exception:
        pass
    # reactor.stop 已在 on_msg 调
    t.join(timeout=2)
    return received["payload"]


if __name__ == "__main__":
    main()
