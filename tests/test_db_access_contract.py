from pathlib import Path
import sqlite3

from execution.event_sizing import EventSizing


def test_business_code_uses_db_helpers_for_direct_connections():
    repo = Path(__file__).resolve().parents[1]
    allowed_prefixes = {
        "backend/core/db.py",
        "backend/core/state_store.py",
        "execution/event_sizing.py",
        "research/",
        "scripts/",
        "tests/",
    }
    patterns = ("sqlite3.connect", "duckdb.connect", "psycopg.connect")
    offenders: list[str] = []
    for folder in ("backend", "execution", "risk", "alpha", "monitor"):
        for path in (repo / folder).rglob("*.py"):
            rel = path.relative_to(repo).as_posix()
            if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in allowed_prefixes):
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            for pattern in patterns:
                if pattern in text:
                    offenders.append(f"{rel}: {pattern}")
    assert offenders == []


def test_event_sizing_allows_legacy_sqlite_event_files(tmp_path):
    db = tmp_path / "events.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events(date TEXT, type TEXT, description TEXT, importance INTEGER)")
    conn.execute("INSERT INTO events VALUES ('2026-07-03', 'NFP', 'jobs', 3)")
    conn.commit()
    conn.close()

    sizing = EventSizing(db_path=str(db), enabled=True)

    assert sizing.enabled is True
    assert sizing._events


def test_runtime_state_scripts_do_not_open_state_sqlite_directly():
    repo = Path(__file__).resolve().parents[1]
    allowed = {
        "scripts/migrate_state_sqlite_to_pg.py",
        "scripts/verify_state_pg_parity.py",
    }
    offenders: list[str] = []
    for path in (repo / "scripts").rglob("*.py"):
        rel = path.relative_to(repo).as_posix()
        text = path.read_text(encoding="utf-8", errors="replace")
        if "sqlite3.connect" not in text:
            continue
        touches_default_state = "STATE_DB" in text or "data/state.db" in text
        if touches_default_state and rel not in allowed:
            offenders.append(rel)
    assert offenders == []


def test_state_query_helper_is_postgres_only():
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts/state_query.py").read_text(encoding="utf-8")

    assert "get_state_pg_conn" in text
    assert "sqlite3" not in text
    assert "data/state.db" in text
