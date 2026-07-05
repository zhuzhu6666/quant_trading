from backend.services.live_ctrader_runtime import CTraderRuntime


class _Logger:
    def __getattr__(self, _name):
        return lambda *args, **kwargs: None


class _ImmediateThread:
    def __init__(self, target=None, args=(), name=None, daemon=None):
        self.target = target
        self.args = args
        self.name = name
        self.daemon = daemon
        self._alive = False

    def start(self):
        self._alive = True
        if self.target:
            self.target(*self.args)
        self._alive = False

    def is_alive(self):
        return self._alive


class _Bridge:
    def __init__(self, *, token=True, connect_ok=True):
        self.send_orders = False
        self.is_connected = False
        self.is_connecting = False
        self.token = token
        self.connect_ok = connect_ok
        self.connect_calls = 0
        self.account_prime_calls = 0
        self.position_prime_calls = 0

    def has_token(self):
        return self.token

    def connect(self):
        self.connect_calls += 1
        self.is_connected = bool(self.connect_ok)
        return bool(self.connect_ok)

    def connect_backoff_seconds(self):
        return 2.0

    def refresh_account_info(self):
        self.account_prime_calls += 1

    def refresh_positions(self):
        self.position_prime_calls += 1


def test_ctrader_runtime_starts_background_connect_and_reuses_bridge(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.services.live_ctrader_runtime.threading.Thread", _ImmediateThread)
    runtime = CTraderRuntime(lock_path=tmp_path / "ctrader.lock")
    bridge = _Bridge(token=True, connect_ok=True)

    def _make_bridge(**kwargs):
        bridge.send_orders = bool(kwargs.get("send_orders"))
        return bridge, None

    result = runtime.get_or_start(
        make_bridge=_make_bridge,
        should_send_orders=lambda broker: broker == "ctrader",
        apply_runtime_config=lambda item: setattr(item, "runtime_applied", True),
        logger=_Logger(),
    )

    assert result == (bridge, None, True)
    assert bridge.connect_calls == 1
    assert bridge.is_connected is True
    assert bridge.account_prime_calls == 1
    assert bridge.position_prime_calls == 1

    result = runtime.get_or_start(
        make_bridge=_make_bridge,
        should_send_orders=lambda broker: False,
        apply_runtime_config=lambda item: setattr(item, "runtime_applied", True),
        logger=_Logger(),
    )

    assert result == (bridge, None, False)
    assert bridge.send_orders is False


def test_ctrader_runtime_rejects_missing_token(tmp_path):
    runtime = CTraderRuntime(lock_path=tmp_path / "ctrader.lock")
    bridge = _Bridge(token=False)

    result = runtime.get_or_start(
        make_bridge=lambda **kwargs: (bridge, None),
        should_send_orders=lambda broker: False,
        apply_runtime_config=lambda item: None,
        logger=_Logger(),
    )

    assert result == (
        None,
        "no cTrader credentials in .env (CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN/ACCOUNT_ID)",
        False,
    )
    assert runtime.bridge is None


def test_ctrader_runtime_failed_connect_sets_retry(monkeypatch, tmp_path):
    monkeypatch.setattr("backend.services.live_ctrader_runtime.threading.Thread", _ImmediateThread)
    runtime = CTraderRuntime(lock_path=tmp_path / "ctrader.lock")
    bridge = _Bridge(token=True, connect_ok=False)

    result = runtime.get_or_start(
        make_bridge=lambda **kwargs: (bridge, None),
        should_send_orders=lambda broker: False,
        apply_runtime_config=lambda item: None,
        logger=_Logger(),
    )

    assert result == (bridge, None, True)
    assert runtime.last_error == "cTrader connect failed (check credentials / network)"
    assert runtime.retry_remaining() > 0
