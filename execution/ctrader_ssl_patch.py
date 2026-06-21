"""Monkey-patch ctrader_open_api Client 的 bare ssl: endpoint → 带 SNI.

ctrader_open_api 0.9.2 用 clientFromString(reactor, "ssl:host:port")
创建 endpoint, 不走 optionsForClientTLS → 缺少 SNI → TLS 握手被拒.
此模块在 CTraderBridge.connect() 创建 Client 之前调用 _patch_ctrader_ssl_endpoint().
"""


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
                ctx = _tw_ssl.optionsForClientTLS(host)
                return SSL4ClientEndpoint(reactor, host, port, ctx)
        return _orig_cfs(reactor, desc)

    _ct_client.clientFromString = _patched_cfs
    _ct_client._patched_ssl_endpoint = True
