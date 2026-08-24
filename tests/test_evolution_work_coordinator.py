import pytest

from backend.services.evolution_work_coordinator import EvolutionWorkCoordinator, coordinated_job


class _Cursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _Connection:
    def __init__(self, acquired=True):
        self.acquired = acquired
        self.calls = []
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql, params):
        self.calls.append((sql, params))
        if "pg_try_advisory_lock" in sql:
            return _Cursor((self.acquired,))
        return _Cursor((True,))

    def close(self):
        self.closed = True

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_coordinator_runs_one_job_and_releases_session_lock():
    conn = _Connection(acquired=True)
    coordinator = EvolutionWorkCoordinator(conn_factory=lambda: conn)

    result = coordinator.run("evolution", lambda: {"status": "completed"})

    assert result == {"status": "completed"}
    assert any("pg_try_advisory_lock" in sql for sql, _ in conn.calls)
    assert conn.commits == 1
    assert conn.rollbacks == 0
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.calls)
    assert conn.closed is True


def test_coordinator_skips_when_another_autonomous_job_holds_lock():
    conn = _Connection(acquired=False)
    called = []
    coordinator = EvolutionWorkCoordinator(conn_factory=lambda: conn)

    result = coordinator.run("governance", lambda: called.append(True))

    assert result["status"] == "skipped_busy"
    assert called == []
    assert conn.commits == 0
    assert conn.rollbacks == 1
    assert not any("pg_advisory_unlock" in sql for sql, _ in conn.calls)
    assert conn.closed is True


def test_coordinator_releases_lock_when_job_fails():
    conn = _Connection(acquired=True)
    coordinator = EvolutionWorkCoordinator(conn_factory=lambda: conn)

    with pytest.raises(RuntimeError, match="job failed"):
        coordinator.run("nursery", lambda: (_ for _ in ()).throw(RuntimeError("job failed")))

    assert conn.commits == 1
    assert any("pg_advisory_unlock" in sql for sql, _ in conn.calls)
    assert conn.closed is True


def test_coordinated_job_preserves_scheduler_identity(monkeypatch):
    calls = []
    monkeypatch.setattr(
        EvolutionWorkCoordinator,
        "run",
        lambda self, name, fn: calls.append(name) or fn(),
    )
    wrapped = coordinated_job("feature_eng", lambda: 42)

    assert wrapped.__name__ == "coordinated_feature_eng"
    assert wrapped() == 42
    assert calls == ["feature_eng"]
