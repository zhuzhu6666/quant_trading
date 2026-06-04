"""
scripts/probe_ctrader_hosts.py — 探测 demo + live host 的连通性 / server 行为差异

不动 App auth,只发:
  Test 1: ProtoOAVersionReq (无认证,纯握手)
  Test 2: ProtoOACtidProfileByTokenReq (用 access_token 反查 profile,无 App auth 也能查)
  Test 3: ProtoOAGetAccountListByAccessTokenReq (用 access_token 列账户)

目的: 验证 clientId 是 demo 配的还是 live 配的。如果是 live 配的,连 demo 必拒;反之亦然。
"""
import sys
import time
import logging
from pathlib import Path
from threading import Thread

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from execution._env import load_env
load_env()

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("probe_hosts")

from twisted.internet import reactor
from ctrader_open_api import Client, TcpProtocol
from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAVersionReq
from ctrader_open_api.protobuf import Protobuf


def probe(host, port, timeout=8.0):
    """单 host probe, 起新 reactor, 跑完即停"""
    log.info(f"=== probing {host}:{port} ===")
    client = Client(host, port, TcpProtocol)
    received = {"name": None, "payload": None, "err": None}
    done = {"flag": False}

    def on_msg(c, message):
        try:
            payload = Protobuf.extract(message)
        except Exception as e:
            received["err"] = repr(e)
            received["raw_payloadType"] = message.payloadType
            done["flag"] = True
            reactor.callFromThread(reactor.stop)
            return
        name = type(payload).__name__
        received["name"] = name
        received["payload"] = payload
        if name == "ProtoOAVersionRes":
            log.info(f"  ✓ VersionRes: version={getattr(payload,'version','?')!r}")
        elif name == "ProtoOAErrorRes":
            log.info(f"  ✗ ErrorRes: errorCode={payload.errorCode!r} description={payload.description!r}")
            for fname in payload.DESCRIPTOR.fields_by_name:
                v = getattr(payload, fname, None)
                if v not in (None, "", 0, [], b""):
                    log.info(f"    {fname} = {v!r}")
        else:
            log.info(f"  ? {name}  (unexpected)")
        done["flag"] = True
        reactor.callFromThread(reactor.stop)

    def on_disc(c, reason):
        if not done["flag"]:
            log.info(f"  disconnected (no response): {type(reason.value).__name__}: {reason.value}")
            done["flag"] = True
            reactor.callFromThread(reactor.stop)

    client.setMessageReceivedCallback(on_msg)
    client.setDisconnectedCallback(on_disc)
    client.startService()

    t = Thread(target=lambda: reactor.run(installSignalHandlers=False), daemon=True)
    t.start()

    t0 = time.time()
    while not client.isConnected and time.time() - t0 < timeout:
        time.sleep(0.1)
    if not client.isConnected:
        log.error(f"  TCP connect failed in {time.time()-t0:.1f}s")
        try: client.stopService()
        except: pass
        return received
    log.info(f"  TCP connected in {time.time()-t0:.1f}s")

    req = ProtoOAVersionReq()
    log.info(f"  sending ProtoOAVersionReq...")
    d = client.send(req, responseTimeoutInSeconds=5)

    # 等结果
    t0 = time.time()
    while not done["flag"] and time.time() - t0 < 6:
        time.sleep(0.1)

    if not done["flag"]:
        log.error(f"  no response in 6s")

    try: client.stopService()
    except: pass
    t.join(timeout=2)
    return received


def main():
    # 串行: 先 demo, 再 live
    demo_result = probe("demo.ctraderapi.com", 5035)
    print()
    live_result = probe("live.ctraderapi.com", 5035)
    print()
    log.info("=== 汇总 ===")
    log.info(f"  demo: {demo_result.get('name') or demo_result.get('err') or 'no_response'}")
    log.info(f"  live: {live_result.get('name') or live_result.get('err') or 'no_response'}")


if __name__ == "__main__":
    main()
