from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.services import sync_service


class _Health:
    def __init__(self) -> None:
        self.attempts = 0
        self.success_payload = None
        self.failures: list[str] = []

    def record_attempt(self) -> None:
        self.attempts += 1

    def record_success(self, last_bar_ts_by_tf=None) -> None:
        self.success_payload = last_bar_ts_by_tf

    def record_failure(self, error: str) -> None:
        self.failures.append(error)


class _State:
    def emit_metric(self, *_args, **_kwargs) -> None:
        return None


@pytest.mark.asyncio
async def test_do_one_sync_updates_last_bar_timestamps(monkeypatch) -> None:
    health = _Health()

    class _Puller:
        def pull_history(self, symbol: str, timeframe: str, n: int = 50):
            return SimpleNamespace(n_bars=50, last_time=1710000000 if timeframe == "M5" else 1710000900)

    class _Story:
        def append(self, *_args, **_kwargs) -> None:
            return None

    monkeypatch.setattr(sync_service, "_stdlib_logger", SimpleNamespace(info=lambda *a, **k: None, exception=lambda *a, **k: None))

    import sys

    monkeypatch.setitem(sys.modules, "monitor.evolution_story", SimpleNamespace(EvolutionStory=SimpleNamespace(shared=lambda: _Story())))
    monkeypatch.setitem(sys.modules, "data.live_sync.ctrader_puller", SimpleNamespace(CTraderPuller=_Puller))
    monkeypatch.setitem(sys.modules, "config.runtime_config", SimpleNamespace(shared=lambda: (lambda: SimpleNamespace(enabled_symbols=["XAUUSD+"])) ))

    await sync_service._do_one_sync(_State(), health)

    assert health.attempts == 1
    assert health.failures == []
    assert health.success_payload == {"M5": 1710000000.0, "M15": 1710000900.0}
