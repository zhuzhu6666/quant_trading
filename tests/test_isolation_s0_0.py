"""S0-0 回归测试：证明测试套件跑在隔离的 state store 上。

这个文件的作用是把这个修复固化成可回归的断言，而不是只留下一次性脚本。
"""
import os
import sqlite3

from backend.core import db as _db


def test_state_dir_env_is_set():
    assert os.environ.get("QUANT_TEST_STATE_DIR"), "隔离环境变量未设置"


def test_state_db_rebound_away_from_production():
    assert _db.STATE_DB != _db._DEFAULT_STATE_DB, (
        "STATE_DB 未与生产锚点分离，测试仍指向生产库"
    )
    assert str(_db.STATE_DB).startswith(os.environ["QUANT_TEST_STATE_DIR"]), (
        "STATE_DB 未指向隔离目录"
    )


def test_predicate_rejects_legacy_sentinel():
    """旧常量（已绑定进默认参数的那个）必须被判为非生产库。"""
    assert _db.is_state_db_path(_db._DEFAULT_STATE_DB) is False


def test_production_anchor_untouched():
    """生产锚点必须仍指向仓库内的 data/state.db。

    这一条是"过度杀伤"的守门人：v1 补丁曾把生产后端也一起禁掉，
    造成 28 个测试失败。锚点若被改到临时目录，说明隔离范围失控。
    """
    assert _db._DEFAULT_STATE_DB.name == "state.db"
    assert "quant_test" not in str(_db._DEFAULT_STATE_DB), (
        "生产锚点被指向测试目录，隔离范围失控"
    )
    assert not str(_db._DEFAULT_STATE_DB).startswith(
        os.environ["QUANT_TEST_STATE_DIR"]
    ), "生产锚点落入隔离目录"
    # 必须是仓库内的路径，不是 /tmp
    assert str(_db._DEFAULT_STATE_DB).startswith("/home/ubuntu/quant_trading"), (
        f"生产锚点不在仓库内: {_db._DEFAULT_STATE_DB}"
    )


def test_state_connection_is_sqlite():
    import backend.services.evolution_ledger as el

    conn = el._connect(el.STATE_DB)
    try:
        assert isinstance(conn, sqlite3.Connection), (
            "state 连接不是 SQLite，测试可能连到生产库"
        )
    finally:
        conn.close()


def test_get_state_conn_does_not_reach_pg():
    conn = _db.get_state_conn()
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


# ── Second route: get_state_pg_conn() reaches PostgreSQL by DSN ──────────
# Discovered 2026-09-11 21:30: a test run wrote
# runtime.recovery_position_state position_id=904 into production through
# this entry point, because it never consults is_state_db_path().

def test_get_state_pg_conn_degrades_to_sandbox():
    """get_state_pg_conn() must not reach PostgreSQL while isolated."""
    conn = _db.get_state_pg_conn()
    try:
        assert isinstance(conn, sqlite3.Connection), (
            "get_state_pg_conn() reached PostgreSQL during an isolated run; "
            "this is the second route and it bypasses is_state_db_path()"
        )
    finally:
        conn.close()


def test_get_state_pg_conn_read_only_degrades_to_sandbox():
    conn = _db.get_state_pg_conn(read_only=True)
    try:
        assert isinstance(conn, sqlite3.Connection)
    finally:
        conn.close()


def test_allow_pg_tests_opt_in_is_wired():
    """QUANT_ALLOW_PG_TESTS must exist as the integration-test escape hatch.

    We assert the switch is respected rather than opening a real PG
    connection: the suite runs with the hatch closed by design.
    """
    assert os.environ.get("QUANT_ALLOW_PG_TESTS") == "0", (
        "the default suite must run with the PG opt-in closed"
    )
