from pathlib import Path

from backend.api import logs as logs_api


def test_tail_logs_reads_only_allowlisted_source_and_returns_metadata(auth_client, monkeypatch, tmp_path):
    (tmp_path / "backend.log").write_text(
        "2026-08-14 10:00:00 | INFO | first\n"
        "2026-08-14 10:00:01 | ERROR | second\n"
        "2026-08-14 10:00:02 | INFO | third\n",
        encoding="utf-8",
    )
    (tmp_path / "debug.log").write_text("DEBUG detail\n", encoding="utf-8")
    monkeypatch.setattr(logs_api, "LOGS_DIR", Path(tmp_path))

    response = auth_client.get("/api/logs/tail?source=backend&lines=2")

    assert response.status_code == 200
    body = response.json()
    assert body["source"] == "backend"
    assert body["file"] == "backend.log"
    assert body["lines"] == [
        "2026-08-14 10:00:01 | ERROR | second",
        "2026-08-14 10:00:02 | INFO | third",
    ]
    assert body["size_bytes"] > 0
    assert isinstance(body["observed_at"], float)

    debug = auth_client.get("/api/logs/tail?source=debug&lines=10")
    assert debug.status_code == 200
    assert debug.json()["lines"] == ["DEBUG detail"]


def test_tail_logs_rejects_unknown_source(auth_client):
    response = auth_client.get("/api/logs/tail?source=arbitrary-file")

    assert response.status_code == 422
