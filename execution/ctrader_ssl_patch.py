"""Monkey-patch ctrader_open_api Client 的 bare ssl: endpoint → 带 SNI.

ctrader_open_api 0.9.2 用 clientFromString(reactor, "ssl:host:port")
创建 endpoint, 不走 optionsForClientTLS → 缺少 SNI → TLS 握手被拒.

Windows + PyOpenSSL 环境里, Twisted 默认 trust store 偶发读不到系统证书,
会在 cTrader 握手时报 ``certificate verify failed``. 这里优先加载 certifi
CA bundle 作为 trust root, 没装 certifi 时再回退到 Twisted 默认行为。

此模块在 CTraderBridge.connect() 创建 Client 之前调用 _patch_ctrader_ssl_endpoint().
"""

from __future__ import annotations

import re
from functools import lru_cache


@lru_cache(maxsize=1)
def _certifi_trust_root():
    try:
        import certifi
        from twisted.internet import _sslverify, ssl as _tw_ssl
    except Exception:
        return None
    try:
        pem_text = open(certifi.where(), "r", encoding="ascii", errors="ignore").read()
    except Exception:
        return None
    blocks = re.findall(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        pem_text,
        flags=re.DOTALL,
    )
    if not blocks:
        return None
    certificates = []
    for block in blocks:
        try:
            certificates.append(_tw_ssl.Certificate.loadPEM(block.encode("ascii")))
        except Exception:
            continue
    if not certificates:
        return None
    try:
        return _sslverify.trustRootFromCertificates(certificates)
    except Exception:
        return None


def _patch_ctrader_ssl_endpoint() -> None:
    import ctrader_open_api.client as _ct_client
    if getattr(_ct_client, "_patched_ssl_endpoint", False):
        return
    _orig_cfs = _ct_client.clientFromString

    def _patched_cfs(reactor, desc):
        if desc.startswith("ssl:"):
            parts = desc[4:].rsplit(":", 1)
            if len(parts) == 2:
                host, port_str = parts
                try:
                    port = int(port_str)
                except ValueError:
                    return _orig_cfs(reactor, desc)
                from twisted.internet import ssl as _tw_ssl
                from twisted.internet.endpoints import SSL4ClientEndpoint
                trust_root = _certifi_trust_root()
                if trust_root is not None:
                    ctx = _tw_ssl.optionsForClientTLS(host, trustRoot=trust_root)
                else:
                    ctx = _tw_ssl.optionsForClientTLS(host)
                return SSL4ClientEndpoint(reactor, host, port, ctx)
        return _orig_cfs(reactor, desc)

    _ct_client.clientFromString = _patched_cfs
    _ct_client._patched_ssl_endpoint = True
