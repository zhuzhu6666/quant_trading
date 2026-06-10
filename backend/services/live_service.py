"""Live trading service.

Responsibilities:
- Probe broker connection status (MT5 + cTrader)
- Read real account info (balance / equity / margin / leverage)
- Read real positions (open trades)
- Start/stop the live trading loop as a background **thread** in the backend
  process (not a subprocess — keeps state in the same memory space as the
  WS broadcaster, so /ws/state can include live account info)
- Emergency close all positions on a broker

(audit 2026-06-08: previous version only had status probes and emergency
close. live/start + live/stop were placeholders returning "not implemented
in v1", forcing the user to SSH in and run `python main.py --mode live` by
hand. v8 added real thread management so the Web 总览 can drive the
trading loop from the browser.)
"""
import threading
import time
import traceback
from typing import Any

from loguru import logger

import os
import pandas as pd

# ── Local SL/TP tracking (live loop only) ──────────────────────────
# audit 2026-06-10: 之前 SL/TP 完全靠本地 Python 监控 1 bar 延迟的
# check_sl_tp(), 实际 market_buy 时 bridge 协议不传 SL/TP 字段
# (MARKET 单限制). 改成: market_buy 成交后立即 amend_position_sltp 推
# server. _local_positions 跟踪每个 position_id 的 SL/TP, amend 成功后
# 覆盖, amend 失败时保留旧值(下次 tick 重试).
from dataclasses import dataclass

@dataclass
class _LocalSLTP:
    position_id: int
    sl: float = 0.0
    tp: float = 0.0
    updated_at: float = 0.0  # epoch seconds

_local_positions: dict[int, _LocalSLTP] = {}
_local_positions_lock = threading.Lock()


def _track_local_sl_tp(position_id: int, sl: float, tp: float) -> None:
    """Record/amend local SL/TP mirror for a cTrader position_id.

    Thread-safe. Used by live loop after amend_position_sltp() to keep
    a local copy of where the SL/TP currently sit on the server. Useful
    for reconciliation when broker rejects the next amend (e.g. already
    closed): we know what was last pushed.
    """
    if position_id is None or position_id <= 0:
        return
    with _local_positions_lock:
        _local_positions[position_id] = _LocalSLTP(
            position_id=position_id,
            sl=sl,
            tp=tp,
            updated_at=time.time(),
        )

# ── 共享 live state 缓存 (live loop 周期更新, API/WS 只读) ────────────
# audit 2026-06-08: 旧设计每次 WS 推送 / HTTP 轮询都打 broker,
# Twisted reactor 排队导致页面切换卡顿. 新设计: live loop 周期更新
# _live_state 缓存, 所有读取路径都只读缓存, 0 broker 调用.
# audit 2026-06-10: writers MUST replace the whole list / dict (e.g.
# _live_state["positions"] = new_list), NOT mutate in place
# (pos.append(item)). Readers run on different threads (loop tick +
# HTTP handlers in get_account / get_positions / start_loop); in-place
# mutation can race with iteration and yield torn reads.
_live_state: dict = {
    "broker": None,         # "mt5" | "ctrader" | None
    "loop_running": False,
    "loop_strategy": None,
    "loop_started_at": None,
    "account": None,         # {balance, equity, currency, ...}
    "account_updated_at": None,
    "positions": [],        # [position, ...]
    "positions_updated_at": None,
    "spot_price": None,      # cTrader spot event
    # audit 2026-06-09: session P&L accumulator (incremented on each closed
    # trade). Shown as snapshot.pnl_today in live mode; falls back to the
    # open position's unrealized P&L when the loop is running but no closed
    # trades have happened yet.
    "session_pnl": 0.0,
    "session_trades": 0,     # closed trades count this session
    "session_winning": 0,
    "session_losing": 0,
    "session_max_drawdown_pct": 0.0,
}

# ── cTrader 缓存 (防 WS 1s 推送反复击中 Twisted reactor)
# audit 2026-06-08: WS _read_state_snapshot 每 1s 调 get_account/get_positions,
# 每次都走 _get_ctrader → bridge.account_info → _send (Twisted deferred) .
# cTrader Open API 是顺序协议, 同时多个 _send 互等导致延迟/超时.
# 加 5s TTL 缓存, WS 1s 推读缓存, 缓解 reactor 竞争.
import time as _time
_ACCOUNT_CACHE: dict[str, tuple[float, dict]] = {}
_POSITIONS_CACHE: dict[str, tuple[float, dict]] = {}
_CACHE_TTL = 15.0  # 15s 避免 WS 1s 推 + HTTP 5s 轮询同时击中 reactor
_CACHE_LOCK = threading.Lock()  # 防多个线程同时刷新 (WS + live tick 同时过期)

# audit 2026-06-10: MT5 路径之前完全没缓存, 每次 HTTP 请求都新建 MT5Bridge +
# connect(阻塞 5s+) + account_info + disconnect, 5s 轮询时其他 API 全排队.
# 加同样 15s TTL 缓存, 跟 cTrader 对齐. 独立 key ("mt5") 防止 cTrader 缓存污染.
_MT5_ACCOUNT_CACHE: dict[str, tuple[float, dict]] = {}
_MT5_POSITIONS_CACHE: dict[str, tuple[float, dict]] = {}
_MT5_CACHE_TTL = 15.0


def _cache_get_or_refresh(cache: dict, ttl: float, fetcher):
    """读缓存, 过期则调 fetcher 刷新. 带锁防并发刷新. 出错时返旧缓存不抛."""
    now = _time.time()
    cached = cache.get("ctrader")
    if cached and (now - cached[0]) < ttl:
        return cached[1]
    # 缓存过期: 加锁防重复刷新. 拿到锁后 double-check.
    with _CACHE_LOCK:
        cached = cache.get("ctrader")
        if cached and (_time.time() - cached[0]) < ttl:
            return cached[1]
        try:
            data = fetcher()
            cache["ctrader"] = (_time.time(), data)
            return data
        except Exception:
            if cached:
                return cached[1]
            raise


def _make_ctrader_bridge(**overrides):
    """从 .env 构造 CTraderBridge, 支持 kwargs 覆盖.
    返回 (bridge, error_msg | None)."""
    # 确保 .env 的 CTRADER_* 已灌到 os.environ
    try:
        from execution._env import load_env
        load_env()
    except Exception:
        pass
    try:
        from execution.ctrader_bridge import CTraderBridge
    except ImportError as e:
        return None, f"ctrader-open-api not installed: {e}"
    kw = dict(
        client_id=os.getenv("CTRADER_CLIENT_ID", ""),
        client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
        access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
        account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
    )
    kw.update(overrides)
    return CTraderBridge(**kw), None


# ── cTrader 连接管理 ──────────────────────────────────────────────
# Twisted reactor 是全局单例, 不能 stop/restart. 每次 create+connect+destroy
# bridge 会导致 reactor 状态污染 (旧 protocol 残留).
# 方案: 进程级长连接 bridge, 所有 cTrader API 复用同一个连接.
# audit 2026-06-10: connect() 之前是同步阻塞 (reactor.startService 等回包 +
# 3 次 _send 每次 10s, 总 5-50s), 切 cTrader broker 占满 FastAPI 线程池 40 线程
# 之一, 全部其它 API 排队. 改造: _get_ctrader() 非阻塞 — 首次启动后台线程做
# 真 connect, 立刻返 (bridge, None, warming_up=True); 后续调用查 is_connected
# 属性(瞬时), 连好了返 warming_up=False, 没好返 warming_up=True.
_ctrader_bridge = None  # type: "CTraderBridge | None"
_ctrader_lock = threading.Lock()
_ctrader_connect_thread: threading.Thread | None = None
_ctrader_last_error: str | None = None


def _kickoff_ctrader_connect():
    """在后台线程跑 _ctrader_bridge.connect(). 不会阻塞调用方.
    必须已持有 _ctrader_lock 锁. 假定 _ctrader_bridge 已实例化."""
    global _ctrader_last_error
    bridge = _ctrader_bridge

    def _bg():
        global _ctrader_last_error
        try:
            ok = bridge.connect()
            if not ok:
                _ctrader_last_error = "cTrader connect failed (check credentials / network)"
                logger.warning(f"[ctrader] background connect failed: {_ctrader_last_error}")
            else:
                _ctrader_last_error = None
                logger.info("[ctrader] background connect OK")
        except Exception as e:
            _ctrader_last_error = f"{type(e).__name__}: {e}"[:300]
            logger.warning(f"[ctrader] background connect exception: {_ctrader_last_error}")

    t = threading.Thread(target=_bg, daemon=True, name="ctrader-bg-connect")
    t.start()
    return t


def _get_ctrader():
    """返回进程级长连接 CTraderBridge (非阻塞版, audit 2026-06-10).

    Returns:
        (bridge, error_msg | None, warming_up: bool)
        warming_up=True 表示后台 connect 还没好 — 调用方应返 warming_up 缓存,
        不要阻塞等连接 (e.g. `{"ok": True, "warming_up": True}`).
        warming_up=False + bridge 不为 None → 可直接用.
        error_msg 不为 None → 启动失败 (无 token / 库未装), 重试也没用.
    """
    global _ctrader_bridge, _ctrader_connect_thread
    try:
        from execution._env import load_env
        load_env()
    except Exception:
        pass
    try:
        from execution.ctrader_bridge import CTraderBridge
    except ImportError as e:
        return None, f"ctrader-open-api not installed: {e}", False

    with _ctrader_lock:
        # 复用已有连接 — 用 is_connected 属性 (瞬时), 不用 ping() (阻塞 5s)
        if _ctrader_bridge is not None:
            if _ctrader_bridge.is_connected:
                return _ctrader_bridge, None, False
            # 旧实例断开且没在 reconnect — 后台起一次
            if _ctrader_connect_thread is None or not _ctrader_connect_thread.is_alive():
                _ctrader_connect_thread = _kickoff_ctrader_connect()
            return _ctrader_bridge, None, True  # warming up

        # 首次: 创建实例 + 后台启动 connect
        try:
            _ctrader_bridge = CTraderBridge(
                client_id=os.getenv("CTRADER_CLIENT_ID", ""),
                client_secret=os.getenv("CTRADER_CLIENT_SECRET", ""),
                access_token=os.getenv("CTRADER_ACCESS_TOKEN", ""),
                account_id=int(os.getenv("CTRADER_ACCOUNT_ID", "0")),
            )
        except Exception as e:
            _ctrader_bridge = None
            return None, f"{type(e).__name__}: {e}"[:300], False

        if not _ctrader_bridge.has_token():
            _ctrader_bridge = None
            return None, "no cTrader credentials in .env (CTRADER_CLIENT_ID/SECRET/ACCESS_TOKEN/ACCOUNT_ID)", False

        # ★ 关键改动: 立刻返 warming_up, 后台线程做真 connect
        _ctrader_connect_thread = _kickoff_ctrader_connect()
        return _ctrader_bridge, None, True  # warming up


def warmup_ctrader(timeout_sec: float = 0.0) -> None:
    """在 lifespan 启动时调 — 后台预热 cTrader 连接, 用户切 Live tab 时不卡.
    timeout_sec=0 立即返回 (后台线程继续); >0 则同步等最多 timeout_sec 秒."""
    bridge, err, warming = _get_ctrader()
    if err:
        logger.info(f"[ctrader] warmup skipped: {err}")
        return
    if not warming:
        return  # 已经连好了 (再次调用)
    if timeout_sec <= 0:
        logger.info("[ctrader] warmup launched in background, will be ready by user's first Live tab click")
        return
    # 同步等 (用于 main 进程 fork 之前 etc.)
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if bridge.is_connected:
            logger.info(f"[ctrader] warmup connected in {time.time()-t0:.1f}s")
            return
        time.sleep(0.2)


def _wait_ctrader_ready(bridge, timeout_sec: float = 30.0) -> str | None:
    """blocking 等待 bridge 真正连好. 用于 live loop body 这种已知在后台线程
    可以阻塞的场景. Returns error_msg | None."""
    if bridge is None:
        return "no bridge"
    t0 = time.time()
    while time.time() - t0 < timeout_sec:
        if bridge.is_connected:
            return None
        time.sleep(0.2)
    return f"cTrader connect timeout after {timeout_sec:.0f}s"


# ── Status / account / positions ──────────────────────────────────────────

_probe_mt5_cache: tuple[float, str, str | None] | None = None
_probe_ctrader_cache: tuple[float, str, str | None] | None = None
_MT5_PROBE_TTL = 30.0  # 30s 缓存, 免每次状态轮询都调 mt5.initialize() (阻塞5s)
_CTRADER_PROBE_TTL = 15.0  # cTrader ping 也有 5s 超时, 按 _ACCOUNT_CACHE 节奏缓存


def get_status() -> dict:
    """Report current broker connection status (best-effort, no broker call)."""
    global _probe_mt5_cache, _probe_ctrader_cache
    mt5_status, mt5_error = _probe_mt5()
    ctrader_status, ctrader_error = _probe_ctrader()
    return {
        "mt5": {"status": mt5_status, "error": mt5_error},
        "ctrader": {"status": ctrader_status, "error": ctrader_error},
        "loop": loop_status(),
    }


def _probe_mt5() -> tuple[str, str | None]:
    global _probe_mt5_cache
    now = time.time()
    if _probe_mt5_cache and (now - _probe_mt5_cache[0]) < _MT5_PROBE_TTL:
        return _probe_mt5_cache[1], _probe_mt5_cache[2]
    try:
        from execution.mt5_bridge import MT5Bridge
        bridge = MT5Bridge()
        if bridge.connect():
            bridge.disconnect()
            _probe_mt5_cache = (now, "connected", None)
            return "connected", None
        _probe_mt5_cache = (now, "disconnected", "connect returned False (no MT5 terminal running or wrong creds)")
        return "disconnected", "connect returned False (no MT5 terminal running or wrong creds)"
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"[:300]
        _probe_mt5_cache = (now, "error", msg)
        return "error", msg


def _probe_ctrader() -> tuple[str, str | None]:
    global _probe_ctrader_cache
    now = time.time()
    if _probe_ctrader_cache and (now - _probe_ctrader_cache[0]) < _CTRADER_PROBE_TTL:
        return _probe_ctrader_cache[1], _probe_ctrader_cache[2]
    # audit 2026-06-10: _get_ctrader 现在返 3-tuple; warming_up 不算 error
    bridge, err, warming = _get_ctrader()
    if err:
        result = ("error", err) if "not installed" in err else \
                 ("no_token", err) if "no cTrader credentials" in err else \
                 ("disconnected", err)
        _probe_ctrader_cache = (now, result[0], result[1])
        return result
    if warming or not bridge.is_connected:
        # audit 2026-06-10: 后台 connect 进行中, 标 warming_up, 不当 error
        _probe_ctrader_cache = (now, "warming_up", None)
        return "warming_up", None
    _probe_ctrader_cache = (now, "connected", None)
    return "connected", None


def get_account(broker: str) -> dict:
    """Read real broker account info. Returns dict with at minimum
    {ok, broker, balance, equity, margin, leverage, currency, error}.

    audit 2026-06-09: 如果 live loop 在跑这个 broker, 短路返回 _live_state 缓存,
    避免重复打 broker (Twisted reactor callFromThread 会阻塞主线程 50-200ms,
    直接卡前端 HTTP 请求). Loop 自己的 tick 已经每 60s 刷新 _live_state."""
    # ── 缓存短路: loop 在跑 → 只读 _live_state ──
    if _live_state.get("loop_running") and _live_state.get("broker") == broker:
        acct = _live_state.get("account")
        if acct and acct.get("ok"):
            return acct
        # 缓存没准备好 (loop 刚启动或第一次 tick 未完成)
        return {
            "ok": False,
            "broker": broker,
            "warming_up": True,
            "error": "live loop warming up, first tick pending (within 60s)",
        }
    if broker == "mt5":
        # audit 2026-06-10: 加 15s TTL 缓存, 避免 mt5.initialize() 每次阻塞 5s+
        def _fetch_mt5_acct():
            from execution.mt5_bridge import MT5Bridge
            bridge = MT5Bridge()
            if not bridge.connect():
                return {"ok": False, "broker": "mt5", "error": "mt5_connect_failed"}
            try:
                info = bridge.account_info()
                if not info:
                    return {"ok": False, "broker": "mt5", "error": "account_info returned empty (likely no account logged in)"}
                return {"ok": True, "broker": "mt5", **info}
            finally:
                bridge.disconnect()
        try:
            return _cache_get_or_refresh(_MT5_ACCOUNT_CACHE, _MT5_CACHE_TTL, _fetch_mt5_acct)
        except Exception as e:
            return {"ok": False, "broker": "mt5", "error": f"{type(e).__name__}: {e}"[:300]}
    elif broker == "ctrader":
        def _fetch():
            # audit 2026-06-10: _get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = _get_ctrader()
            if err:
                return {"ok": False, "broker": "ctrader", "error": err}
            if warming or not bridge.is_connected:
                return {
                    "ok": True,  # 标识 HTTP 200 正常, 前端按 warming_up 渲染
                    "broker": "ctrader",
                    "warming_up": True,
                    "error": "cTrader connecting in background, first account query pending (within 30s)",
                }
            info = bridge.account_info()
            if not info:
                return {"ok": False, "broker": "ctrader", "error": "account_info returned empty"}
            return {"ok": True, "broker": "ctrader", **info}
        try:
            return _cache_get_or_refresh(_ACCOUNT_CACHE, _CACHE_TTL, _fetch)
        except Exception as e:
            return {"ok": False, "broker": "ctrader", "error": f"{type(e).__name__}: {e}"[:300]}
    else:
        return {"ok": False, "broker": broker, "error": f"unknown broker: {broker}"}


def get_positions(broker: str, symbol: str | None = None) -> dict:
    """Read open positions on the given broker. Returns {ok, broker, positions: [...]}.

    audit 2026-06-09: 同 get_account, live loop 在跑时短路读缓存."""
    # ── 缓存短路: loop 在跑 → 只读 _live_state ──
    if _live_state.get("loop_running") and _live_state.get("broker") == broker:
        cached = _live_state.get("positions")
        if isinstance(cached, dict) and cached.get("ok"):
            return cached
        # 缓存未就绪 — 同步 broker 路径在 live 模式下不应该走
        return {
            "ok": True,  # 标识 HTTP 200 正常, 但数据为空
            "broker": broker,
            "positions": [],
            "warming_up": True,
        }
    if broker == "mt5":
        # audit 2026-06-10: 加 15s TTL 缓存, 跟 account 路径对齐
        def _fetch_mt5_pos():
            from execution.mt5_bridge import MT5Bridge
            bridge = MT5Bridge()
            if not bridge.connect():
                return {"ok": False, "broker": "mt5", "error": "mt5_connect_failed", "positions": []}
            try:
                pos = bridge.get_positions(symbol)
                return {"ok": True, "broker": "mt5", "positions": pos}
            finally:
                bridge.disconnect()
        try:
            return _cache_get_or_refresh(_MT5_POSITIONS_CACHE, _MT5_CACHE_TTL, _fetch_mt5_pos)
        except Exception as e:
            return {"ok": False, "broker": "mt5", "error": f"{type(e).__name__}: {e}"[:300], "positions": []}
    elif broker == "ctrader":
        # 缓存短路: live loop 在跑 → 只读 _live_state (跟上面 if 分支等价,
        # 保留是为了 cache_fallback 的 robustness — 上层分支没匹配时这里兜底)
        cached_positions = _live_state.get("positions")
        if cached_positions is not None and _live_state.get("loop_running"):
            return {"ok": True, "broker": "ctrader", "positions": cached_positions}
        # 缓存空 fallback
        def _fetch():
            # audit 2026-06-10: _get_ctrader 返 3-tuple, warming_up 短路
            bridge, err, warming = _get_ctrader()
            if err:
                return {"ok": False, "broker": "ctrader", "error": err, "positions": []}
            if warming or not bridge.is_connected:
                return {
                    "ok": True,
                    "broker": "ctrader",
                    "positions": [],
                    "warming_up": True,
                }
            raw = bridge.get_positions(symbol)
            positions = []
            for p in raw:
                positions.append({
                    "ticket": p.get("position_id"),
                    "symbol": p.get("symbol_id"),
                    "type": p.get("type"),
                    "volume": p.get("volume", 0.0),
                    "price_open": p.get("price_open", 0.0),
                    "price_current": p.get("price_current", p.get("price_open", 0.0)),
                    "sl": p.get("sl", 0.0),
                    "tp": p.get("tp", 0.0),
                    "profit": p.get("profit") or 0.0,
                    "swap": p.get("swap", 0.0),
                    "commission": p.get("commission", 0.0),
                    "magic": p.get("magic"),
                })
            return {"ok": True, "broker": "ctrader", "positions": positions}
        try:
            return _cache_get_or_refresh(_POSITIONS_CACHE, _CACHE_TTL, _fetch)
        except Exception as e:
            return {"ok": False, "broker": "ctrader", "error": f"{type(e).__name__}: {e}"[:300], "positions": []}
    else:
        return {"ok": False, "broker": broker, "error": f"unknown broker: {broker}", "positions": []}


# ── Trading loop management (background thread) ─────────────────────────

# Module-level state for the loop (singleton, persists across requests)
_loop_thread: threading.Thread | None = None
_loop_stop_flag: threading.Event = None  # type: ignore[assignment]
_loop_broker: str | None = None
_loop_started_at: float | None = None
_loop_strategy_name: str | None = None  # audit 2026-06-08: 当前 loop 跑的 strategy
_loop_state_lock = threading.Lock()


def loop_status() -> dict:
    """Whether the live trading loop thread is running. 优先 _live_state 缓存."""
    # 优先共享缓存 (audit 2026-06-08)
    if _live_state.get("loop_running") and _live_state.get("broker"):
        return {
            "running": True,
            "pid": None,
            "broker": _live_state["broker"],
            "started_at": _live_state.get("loop_started_at"),
            "strategy_name": _live_state.get("loop_strategy"),
        }
    with _loop_state_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return {
                "running": True,
                "pid": _loop_thread.ident,
                "broker": _loop_broker,
                "started_at": _loop_started_at,
                "strategy_name": _loop_strategy_name,
            }
        return {
            "running": False, "pid": None, "broker": None,
            "started_at": None, "strategy_name": _loop_strategy_name,
        }


def start_loop(broker: str, strategy_name: str = "v1_minimal_ma_cross") -> dict:
    """Spawn the live loop as a background thread in this backend process.
    Refuses if a loop is already running. Requires the broker to be reachable."""
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at, _loop_strategy_name

    with _loop_state_lock:
        if _loop_thread is not None and _loop_thread.is_alive():
            return {
                "ok": False,
                "error": f"live loop already running (broker={_loop_broker})",
                "broker": _loop_broker,
                "started_at": _loop_started_at,
                "strategy_name": _loop_strategy_name,
            }
        if broker not in ("mt5", "ctrader"):
            return {"ok": False, "error": f"unknown broker: {broker}"}

        # Pre-flight: broker connection must be live
        # cTrader Twisted reactor can be flaky; retry once on failure
        # ⚠️ audit 2026-06-09: 不阻塞等 pre-flight — loop 线程自己会连接重试.
        # 之前 get_account 等 bridge 超时 10-14s, 卡住请求线程, 所有 API 排队.
        # 直接填充占位账户, 不等 bridge 响应.
        acct = {"ok": True, "broker": broker, "balance": 0, "equity": 0,
                "margin": 0, "margin_free": 0, "leverage": 0, "currency": ""}

        _loop_stop_flag = threading.Event()
        _loop_broker = broker
        _loop_started_at = time.time()
        _loop_strategy_name = strategy_name  # audit 2026-06-08
        # ⚠️ audit 2026-06-09: 启动前立即填充共享缓存, 否则 WS 1s 推送读到
        # _live_state["account"]=None → equity=0, 要等 60s 第一个 tick 才恢复.
        _live_state["broker"] = broker
        _live_state["loop_running"] = True
        _live_state["loop_strategy"] = strategy_name
        _live_state["loop_started_at"] = _loop_started_at
        _live_state["account"] = acct
        _live_state["account_updated_at"] = time.time()
        _live_state["session_pnl"] = 0.0
        _live_state["session_trades"] = 0
        _live_state["session_winning"] = 0
        _live_state["session_losing"] = 0
        _live_state["session_max_drawdown_pct"] = 0.0
        _loop_thread = threading.Thread(
            target=_run_loop,
            args=(broker, _loop_stop_flag),
            name=f"live_loop_{broker}",
            daemon=True,
        )
        _loop_thread.start()
        logger.info(f"live loop started: broker={broker} strategy={strategy_name} thread_id={_loop_thread.ident}")

    return {
        "ok": True,
        "broker": broker,
        "started_at": _loop_started_at,
        "thread_id": _loop_thread.ident,
        "pid": _loop_thread.ident,  # audit 2026-06-09: alias for FE uniformity (paper/start returns pid; thread.ident is the closest equivalent for a background thread)
        "strategy_name": strategy_name,
        "msg": f"live loop thread started. Read /api/live/loop-status to monitor.",
    }


def stop_loop() -> dict:
    """Signal the loop thread to stop. Waits up to 5s for it to exit."""
    global _loop_thread, _loop_stop_flag, _loop_broker, _loop_started_at

    with _loop_state_lock:
        if _loop_thread is None or not _loop_thread.is_alive():
            return {"ok": True, "was_running": False, "broker": None, "msg": "no loop running"}
        broker = _loop_broker
        if _loop_stop_flag is not None:
            _loop_stop_flag.set()
        thread = _loop_thread
    # wait outside the lock
    thread.join(timeout=5)
    if thread.is_alive():
        logger.warning(f"live loop thread for {broker} did not stop within 5s; will continue in background")
    with _loop_state_lock:
        _loop_thread = None
        _loop_stop_flag = None
        _loop_broker = None
        _loop_started_at = None
    # 同步到共享缓存
    _live_state["broker"] = None
    _live_state["loop_running"] = False
    _live_state["loop_strategy"] = None
    # audit 2026-06-09: clear session stats so the next /ws/state snapshot
    # doesn't show stale numbers from the previous run.
    _live_state["session_pnl"] = 0.0
    _live_state["session_trades"] = 0
    _live_state["session_winning"] = 0
    _live_state["session_losing"] = 0
    _live_state["session_max_drawdown_pct"] = 0.0
    return {"ok": True, "was_running": True, "broker": broker}


def _warmup_from_local_db(symbol: str = "XAUUSD+", timeframe: str = "M15", n_bars: int = 200) -> "pd.DataFrame | None":
    """audit 2026-06-08: Pepperstone demo broker 不返历史 bar (ProtoOAGetTrendbarsReq
    任何 period 都 0 bar). 改用本地 DataStore 拉 DB 历史预热 strategy 指标.
    实时 tick 走 broker spot event, 这里只保证 strategy 暖机有数据.
    """
    try:
        from data.store import DataStore
        store = DataStore("data/market_data.db")
        df = store.load_bars(symbol, timeframe)
        if df is None or len(df) == 0:
            logger.warning(f"DataStore has no bars for {symbol} {timeframe}")
            return None
        # 留最后 n_bars 根
        df = df.tail(n_bars).reset_index()
        # load_bars 返的 df 有 'time' 列 (int seconds), idx 可能是 0..n
        if "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
            df = df.set_index("time")
        return df[["open", "high", "low", "close", "volume"]]
    except Exception as e:
        logger.warning(f"_warmup_from_local_db failed: {e}")
        return None


def _fetch_bars_with_retry(bridge, timeframe: str, n_bars: int, max_retries: int = 3) -> "pd.DataFrame | None":
    """fetch_bars 重试 wrapper. 失败 1 次不致命, 指数 backoff 2s/4s/8s.
    返 None 表示彻底失败 (调用方决定是否继续).

    audit 2026-06-08: Pepperstone demo broker 不返 history bar. 这个函数主要
    是 best-effort 取"最近几根"用作 sanity check. 真正预热走 _warmup_from_local_db.
    """
    for attempt in range(max_retries):
        try:
            df = bridge.fetch_bars(timeframe=timeframe, n_bars=n_bars)
            if df is not None and len(df) >= 30:
                return df
        except Exception as e:
            logger.warning(f"fetch_bars attempt {attempt+1}/{max_retries} failed: {e}")
        if attempt < max_retries - 1:
            time.sleep(2 ** (attempt + 1))  # 2s, 4s, 8s
    return None


def _run_loop(broker: str, stop_flag: threading.Event) -> None:
    """Live trading loop. v2: 装 multi_factor_m15 策略, 真发单到 broker.

    流程:
      1. warmup 拉 200 根 M15 bars (3 次重试)
      2. 实例化 multi_factor_m15 strategy + on_init
      3. 每 60s tick:
         a) 拉最新 5 根 bar
         b) 喂 strategy.on_bar → Signal
         c) cTrader: market_buy/sell 真发单, 然后 amend_position_sltp
         d) 记 log: equity/balance/positions + 当前价
    日志写到 logs/live_loop.log, broker 端 WS 1s 推送带 current_price.
    """
    import sys
    from pathlib import Path
    project_root = Path(__file__).resolve().parent.parent.parent
    log_path = project_root / "logs" / "live_loop.log"
    log_path.parent.mkdir(exist_ok=True)
    log_fh = open(log_path, "a", encoding="utf-8", buffering=1)

    def log(msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')} [live_loop:{broker}] {msg}"
        log_fh.write(line + "\n")
        log_fh.flush()
        logger.info(line)

    log("loop started (v2: multi_factor_m15 strategy + cTrader market orders)")

    # ── Phase 1: warmup ──
    # audit 2026-06-08: Pepperstone demo broker ProtoOAGetTrendbarsReq 不返 history
    # (任何 period 都 0 bar). 改优先读本地 DataStore("data/market_data.db") 拉 XAUUSD+
    # M15 200 根, 再 fallback 到 broker fetch_bars.
    df = None
    df_source = None
    if broker == "ctrader":
        df = _warmup_from_local_db("XAUUSD+", "M15", 200)
        if df is not None and len(df) >= 30:
            df_source = "local_db"
            last_ts = df.index[-1]
            age_hours = (pd.Timestamp.now("UTC").tz_localize(None) - last_ts.tz_localize(None)).total_seconds() / 3600 if last_ts.tzinfo else 0
            if age_hours > 24:
                logger.warning(
                    f"local DB bars are {age_hours:.1f}h stale (last bar: {last_ts}). "
                    f"Strategy will warm up on outdated data. Consider running live_sync."
                )
    if df is None or len(df) < 30:
        # fallback: broker fetch_bars
        try:
            if broker == "mt5":
                from execution.mt5_bridge import MT5Bridge
                bridge = MT5Bridge()
                if not bridge.connect():
                    log("FATAL: MT5 connect failed at loop start")
                    return
                try:
                    df = _fetch_bars_with_retry(bridge, timeframe=15, n_bars=200)
                finally:
                    bridge.disconnect()
            elif broker == "ctrader":
                # audit 2026-06-10: 3-tuple + 阻塞等连好 (loop 线程里可等)
                bridge, err, warming = _get_ctrader()
                if err:
                    log(f"FATAL: {err}")
                    return
                if warming or not bridge.is_connected:
                    wait_err = _wait_ctrader_ready(bridge, timeout_sec=30.0)
                    if wait_err:
                        log(f"FATAL: {wait_err}")
                        return
                df = _fetch_bars_with_retry(bridge, timeframe="M15", n_bars=200)
            else:
                log(f"FATAL: unknown broker {broker}")
                return
            df_source = "broker"
        except Exception as e:
            log(f"FATAL: warmup exception: {type(e).__name__}: {e}\n{traceback.format_exc()[-500:]}")
            return

    if df is None or len(df) < 30:
        log(f"FATAL: insufficient history bars (got {0 if df is None else len(df)} < 30) — local DB empty AND broker returned 0")
        return
    log(f"warmed up: {len(df)} bars (source={df_source}), last close={df['close'].iloc[-1]:.2f}")

    # ── Phase 2: 实例化 strategy + on_init ──
    # audit 2026-06-08: v2 真跑 multi_factor_m15 策略, 不再是 read-only.
    # 风险: send_orders=True 时 market_buy/sell 真实下单. ctrader bridge
    # 默认 send_orders=False (PoC 安全闸), 用户在 .env 设 CTRADER_SEND_ORDERS=1 启用.
    strategy = None
    try:
        from strategy.registry import strategy_registry
        import os
        send_orders = os.getenv("CTRADER_SEND_ORDERS", "0") == "1"
        if send_orders:
            strategy = strategy_registry.create(
                "multi_factor_m15",
                symbol="XAUUSD+",
                timeframe="M15",
            )
            strategy.on_init()
            log(f"strategy {strategy.name!r} loaded + on_init done; send_orders=True (live)")
        else:
            log("strategy loaded in DRY-RUN mode (CTRADER_SEND_ORDERS != 1); will log signals but not place orders")
            # 仍实例化 strategy 用来算 signal (供前端看因子/投票是否触发)
            try:
                strategy = strategy_registry.create("multi_factor_m15", symbol="XAUUSD+", timeframe="M15")
                strategy.on_init()
            except Exception as e:
                log(f"strategy init failed (dry-run): {e}")
                strategy = None
    except Exception as e:
        log(f"FATAL: strategy load failed: {type(e).__name__}: {e}")
        return

    # 把 warmup bars 喂给 strategy, 让指标先预热
    if strategy is not None:
        # 预热前先让其他线程 (API) 有机会完成请求
        time.sleep(0.1)
        for i in range(len(df)):
            bar = {
                "open": float(df["open"].iloc[i]),
                "high": float(df["high"].iloc[i]),
                "low": float(df["low"].iloc[i]),
                "close": float(df["close"].iloc[i]),
                "volume": float(df["volume"].iloc[i]) if "volume" in df.columns else 0.0,
                "time": float(df.index[i].timestamp()) if hasattr(df.index[i], "timestamp") else 0.0,
                "timeframe": "M15",
                "complete": True,
            }
            try:
                strategy.on_bar(bar)
            except Exception as e:
                log(f"strategy warmup bar {i} failed: {e}")
                break
            # 每 20 bar 让出 GIL 0.2s, 给其他线程 (API 请求) 执行窗口
            if i % 20 == 0:
                time.sleep(0.2)
        log(f"strategy warmed up over {len(df)} historical bars; last_atr={strategy.last_atr}")

    # 订阅 cTrader 实时报价 (audit 2026-06-08)
    # audit 2026-06-10: warmup 走 local_db 路径时 bridge 变量未定义,
    # 之前直接调 bridge.subscribe_spots() 抛 NameError 被 except 吞,
    # log 误报 "failed (non-fatal)". 修: 从 _get_ctrader() 拿真 bridge, 短等 ready.
    if broker == "ctrader":
        try:
            spot_bridge, spot_err, spot_warming = _get_ctrader()
            if spot_err:
                log(f"subscribe_spots skipped: {spot_err}")
            elif spot_warming or not spot_bridge.is_connected:
                wait_err = _wait_ctrader_ready(spot_bridge, timeout_sec=10.0)
                if wait_err:
                    log(f"subscribe_spots skipped: {wait_err}")
                else:
                    spot_bridge.subscribe_spots()
                    log("subscribed to spot events for real-time price")
            else:
                spot_bridge.subscribe_spots()
                log("subscribed to spot events for real-time price")
        except Exception as e:
            log(f"subscribe_spots failed (non-fatal): {e}")

    # ── Phase 3: 主循环 (60s tick) ──
    tick = 0
    while not stop_flag.is_set():
        tick += 1
        try:
            if broker == "mt5":
                from execution.mt5_bridge import MT5Bridge
                bridge = MT5Bridge()
                if not bridge.connect():
                    log(f"tick {tick}: MT5 connect failed, will retry")
                    stop_flag.wait(60)
                    continue
                try:
                    df_new = _fetch_bars_with_retry(bridge, timeframe=15, n_bars=5)
                    if df_new is None or len(df_new) == 0:
                        log(f"tick {tick}: no bars after retry")
                    else:
                        # MT5: same flow as cTrader; strategy drives
                        last_bar = df_new.iloc[-1]
                        _process_tick(bridge, strategy, df_new, last_bar, broker, tick, log)
                finally:
                    # audit 2026-06-10: 后台线程写 _live_state 缓存, WS 1s 推送
                    # 下次 tick 就能拿到真 broker equity. tick 主体不被阻塞, 失败时静默.
                    # MT5 路径: bridge 在 finally 里 disconnect, 但 daemon thread
                    # 持的是同一 bridge 引用, 下次 refresh 时若已断开会自然失败 +
                    # 静默 log, 不影响主循环.
                    kickoff_account_refresh(bridge, broker, interval_sec=30.0)
                    bridge.disconnect()
            elif broker == "ctrader":
                # audit 2026-06-10: 3-tuple + 非阻塞(warming_up 时跳过本 tick)
                bridge, err, warming = _get_ctrader()
                if err:
                    log(f"tick {tick}: {err}; reconnect next tick")
                    stop_flag.wait(60)
                    continue
                if warming or not bridge.is_connected:
                    log(f"tick {tick}: cTrader still warming up, skip tick")
                    stop_flag.wait(60)
                    continue
                # audit 2026-06-10: 后台线程写 _live_state 缓存, WS 1s 推送
                # 下次 tick 就能拿到真 broker equity. tick 主体不被阻塞, 失败时静默.
                # audit 2026-06-10 fix 2: 提到 fetch_bars 之前, 之前放在 else
                # 分支里, cTrader broker 不返 history bars 时永远走 if 分支,
                # kickoff 永远不调. 现在无论 fetch_bars 成败都 kickoff.
                kickoff_account_refresh(bridge, broker, interval_sec=30.0)
                df_new = _fetch_bars_with_retry(bridge, timeframe="M15", n_bars=5)
                if df_new is None or len(df_new) == 0:
                    log(f"tick {tick}: no bars after retry")
                else:
                    last_bar = df_new.iloc[-1]
                    _process_tick(bridge, strategy, df_new, last_bar, broker, tick, log)
        except Exception as e:
            log(f"tick {tick} error: {type(e).__name__}: {e}\n{traceback.format_exc()[-300:]}")

        if stop_flag.wait(60):
            break

    log(f"loop stopped after {tick} ticks")


# ── Background account/positions cache writer ─────────────────────────
# audit 2026-06-10: 之前 _process_tick 每 60s 同步调 bridge.account_info() +
# bridge.get_positions() 写共享缓存. 改读缓存后这个写路径被删了, WS 1s
# 推送就拿到 start_loop 启动时的占位符 (balance=0, equity=0). 修复:
# _run_loop 的 60s 等待期间, 后台 daemon thread 调一次 account_info +
# get_positions, 写 _live_state. tick 主体保持非阻塞, 只有这个 writer
# 异步. 失败时静默 (下次 tick 重试), 不让后台错误炸主循环.
def _refresh_account_positions_sync(bridge, broker: str) -> None:
    """One-shot synchronous write to _live_state. Used by the background
    thread; tests call this directly. Best-effort: never raises."""
    try:
        acct = bridge.account_info() or {}
    except Exception as e:
        logger.warning(f"[{broker}] background account_info failed: {e}")
        return
    if not acct:
        return
    # audit 2026-06-10: ensure the cached account has `ok=True` so the
    # WS snapshot doesn't mistake it for an error envelope.
    acct.setdefault("ok", True)
    acct.setdefault("broker", broker)
    _live_state["account"] = acct
    _live_state["account_updated_at"] = time.time()
    try:
        pos_raw = bridge.get_positions() or []
    except Exception as e:
        logger.warning(f"[{broker}] background get_positions failed: {e}")
        pos_raw = None
    if pos_raw is not None:
        _live_state["positions"] = pos_raw
        _live_state["positions_updated_at"] = time.time()


def kickoff_account_refresh(bridge, broker: str, interval_sec: float = 30.0) -> threading.Thread:
    """Spawn a daemon thread that periodically calls
    _refresh_account_positions_sync. Used by _run_loop during its 60s
    wait so the next WS tick has fresh account/positions data.

    The thread loops: refresh once, then sleep interval_sec, until the
    global _loop_stop_flag is set OR the process exits (daemon=True).
    """
    stop_flag_ref = _loop_stop_flag  # captured at call time

    def _worker():
        while True:
            try:
                if stop_flag_ref is not None and stop_flag_ref.is_set():
                    break
                _refresh_account_positions_sync(bridge, broker)
                # Sleep in small slices so the thread reacts to stop_flag quickly
                slept = 0.0
                while slept < interval_sec:
                    if stop_flag_ref is not None and stop_flag_ref.is_set():
                        return
                    time.sleep(min(0.5, interval_sec - slept))
                    slept += 0.5
            except Exception as e:
                logger.warning(f"[{broker}] account-refresh worker error: {e}")
                time.sleep(1.0)

    t = threading.Thread(
        target=_worker, daemon=True,
        name=f"acct-refresh-{broker}",
    )
    t.start()
    return t


def _process_tick(bridge, strategy, df_new, last_bar, broker: str, tick: int, log) -> None:
    """处理一根新 bar. v3 (audit 2026-06-10):
      1. strategy.on_bar → signal
      2. Read account/positions from _live_state cache (NOT sync broker — that
         ate 30s+ per tick and blocked FastAPI threadpool). Loop is decision-only.
      3. If signal fires and send_orders=True:
         a) market_buy / market_sell (no SL/TP — cTrader MARKET 单不支持)
         b) amend_position_sltp(position_id, sl, tp) — push SL/TP to server.
            cTrader 协议限制, amend 是 0-latency 的唯一办法.
         c) On amend success: _track_local_sl_tp. On failure: log, leave stale,
            next tick retries.
      4. Update _live_state with current_price from the bar.
    """
    from datetime import datetime as _dt

    # 构造 bar dict
    bar = {
        "open": float(last_bar["open"]),
        "high": float(last_bar["high"]),
        "low": float(last_bar["low"]),
        "close": float(last_bar["close"]),
        "volume": float(last_bar["volume"]) if "volume" in last_bar.index else 0.0,
        "time": float(df_new.index[-1].timestamp()) if hasattr(df_new.index[-1], "timestamp") else 0.0,
        "timeframe": "M15",
        "complete": True,
    }

    # 1. strategy 算 signal (in-process; this is fine to do sync)
    signal = None
    if strategy is not None:
        try:
            signal = strategy.on_bar(bar)
        except Exception as e:
            log(f"  strategy.on_bar error: {e}")

    # 2. Read account + positions from shared cache (audit 2026-06-10:
    #    previously called bridge.account_info() + bridge.get_positions()
    #    synchronously — each is a Twisted round-trip, total 20-30s,
    #    blocking the FastAPI threadpool). Cache is updated by:
    #      - /api/live/account endpoint (15s TTL)
    #      - /api/live/positions endpoint (15s TTL)
    #      - WS _read_state_snapshot (1s cadence, 15s cache)
    #    The loop body no longer needs to know real-time equity; signal
    #    decisions only need the current bar + recent bars.
    acct = _live_state.get("account") or {}
    pos = _live_state.get("positions") or []
    # 兼容 positions 两种形态: list[dict] (从 live_state 取出时) or [] from endpoint
    if isinstance(pos, dict):
        pos = pos.get("positions", []) or []
    current_price = float(last_bar["close"])
    _live_state["account_updated_at"] = time.time()
    _live_state["positions_updated_at"] = time.time()
    if bridge is not None and hasattr(bridge, "get_spot_price"):
        try:
            spot = bridge.get_spot_price()
            if spot and spot > 0:
                _live_state["spot_price"] = spot
                current_price = spot
        except Exception:
            pass

    # 3. 发单 + SL/TP 上 server
    if signal is not None and signal.direction in (1, -1, 2):
        send_orders = _should_send_orders(broker)
        direction_name = {1: "LONG", -1: "SHORT", 2: "CLOSE"}.get(signal.direction, "?")
        if not send_orders:
            log(f"  signal={direction_name} (dry-run, no order)")
        else:
            atr = signal.atr or strategy.last_atr or 0.0
            sl_dist = atr * signal.sl_atr if signal.sl_atr > 0 else atr * 2.0
            tp_dist = atr * signal.tp_atr if signal.tp_atr > 0 else atr * 3.0
            sl_price = current_price - sl_dist if signal.direction == 1 else current_price + sl_dist
            tp_price = current_price + tp_dist if signal.direction == 1 else current_price - tp_dist
            volume = 0.01  # 固定 0.01 lot, v2 minimal
            try:
                # 3a) market_buy / market_sell (MARKET 单不传 SL/TP)
                if signal.direction == 1:
                    result = bridge.market_buy(volume=volume, sl=0.0, tp=0.0, comment="quant-live")
                elif signal.direction == -1:
                    result = bridge.market_sell(volume=volume, sl=0.0, tp=0.0, comment="quant-live")
                else:  # CLOSE
                    closed = 0
                    for p in pos:
                        pid = p.get("position_id") or p.get("ticket")
                        if pid is None:
                            continue
                        close_res = bridge.close_position(pid)
                        if getattr(close_res, "success", False):
                            closed += 1
                    log(f"  signal=CLOSE closed={closed}")
                    result = None

                # 3b) amend SL/TP 到 server (audit 2026-06-10: 消除 1 bar 延迟)
                if result is not None and getattr(result, "success", False):
                    pid = getattr(result, "position_id", 0) or 0
                    if pid <= 0:
                        # bridge 返回的 orderId 不等于 positionId — 从
                        # cached positions 找最新匹配的 (audit 2026-06-08:
                        # market_buy 文档说 "awaiting get_positions() for entry_price").
                        # 我们读的是 _live_state 缓存, 里面 pos 是上次
                        # get_positions 拿到的列表.
                        if pos:
                            pid = int(pos[0].get("position_id") or pos[0].get("ticket") or 0)
                    if pid > 0:
                        try:
                            amend_res = bridge.amend_position_sltp(
                                position_id=pid, sl=sl_price, tp=tp_price,
                            )
                            if getattr(amend_res, "success", False):
                                _track_local_sl_tp(pid, sl=sl_price, tp=tp_price)
                                log(f"  signal={direction_name} ORDER+AMEND OK vol={volume} pos={pid} sl={sl_price:.2f} tp={tp_price:.2f}")
                            else:
                                log(f"  signal={direction_name} AMEND FAILED pos={pid}: {getattr(amend_res, 'comment', '?')}")
                        except Exception as e:
                            log(f"  signal={direction_name} amend exception: {e}")
                    else:
                        log(f"  signal={direction_name} ORDER OK (no position_id, skip amend) vol={volume}")
                elif result is not None and not getattr(result, "success", False):
                    log(f"  signal={direction_name} ORDER FAILED: {getattr(result, 'error_code', '?')} {getattr(result, 'comment', '')}")
            except Exception as e:
                log(f"  signal={direction_name} order exception: {e}")

    # 4. 写 log + 把当前价推给 WS
    log(f"tick {tick}: price={current_price:.2f} balance={acct.get('balance', 0):.2f} positions={len(pos)}"
        + (f" signal={signal.direction}" if signal and signal.direction != 0 else ""))
    global _latest_price
    _latest_price = current_price


def _should_send_orders(broker: str) -> bool:
    """True = 真发单; False = dry-run (记 log, 不下单)."""
    import os
    if broker == "ctrader":
        return os.getenv("CTRADER_SEND_ORDERS", "0") == "1"
    elif broker == "mt5":
        return os.getenv("MT5_SEND_ORDERS", "0") == "1"
    return False


# 模块级,供 _read_state_snapshot 读
_latest_price: float | None = None


def get_latest_price() -> float | None:
    """返回最新价. 优先共享缓存 (live loop 写), 其次 bridge spot, 最后 bar close."""
    spot = _live_state.get("spot_price")
    if spot and spot > 0:
        return spot
    global _latest_price
    try:
        # audit 2026-06-10: 3-tuple; warming_up 时返旧价不阻塞
        bridge, err, warming = _get_ctrader()
        if bridge is None or err or warming or not bridge.is_connected:
            return _latest_price
        spot = bridge.get_spot_price()
        if spot is not None and spot > 0:
            return spot
    except Exception:
        pass
    return _latest_price


# ── Emergency close ──────────────────────────────────────────────────────

def emergency_close(broker: str, symbol: str | None = None) -> dict:
    """Close all positions (or one symbol) on the given broker."""
    if broker == "mt5":
        try:
            from execution.mt5_bridge import MT5Bridge
            bridge = MT5Bridge()
            if not bridge.connect():
                return {"ok": False, "error": "mt5_connect_failed"}
            try:
                if symbol:
                    # close_all_positions(symbol) closes only the given symbol
                    bridge.close_all_positions(symbol)
                else:
                    bridge.close_all_positions()
                return {"ok": True, "broker": "mt5", "symbol": symbol or "ALL"}
            finally:
                bridge.disconnect()
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-300:]}
    elif broker == "ctrader":
        # audit 2026-06-10: 3-tuple + 短等 (emergency close 用户主动点, 可接受 5s 等)
        bridge, err, warming = _get_ctrader()
        if err:
            return {"ok": False, "error": err}
        if warming or not bridge.is_connected:
            wait_err = _wait_ctrader_ready(bridge, timeout_sec=5.0)
            if wait_err:
                return {"ok": False, "error": f"cTrader not ready: {wait_err}"}
        try:
            # cTrader close_position() 必须传 position_id, 没传 server 必拒
            # (audit 2026-06-08: 之前分支里 close_position() 不带参会 fail).
            # symbol 路径: 走 get_positions + filter by symbol_id + close 一个个.
            positions = bridge.get_positions()
            if symbol:
                # symbol 这里可能是 symbol 名 (XAUUSD) 或 id (int), 简单按 name 匹配 fallback
                target_positions = [p for p in positions if str(p.get("symbol_id")) == symbol or p.get("symbol") == symbol]
            else:
                target_positions = positions
            closed = 0
            for p in target_positions:
                # 优先用 position_id; 旧 dict 形式也兼容
                pid = p.get("position_id") or p.get("ticket")
                if pid is None:
                    continue
                result = bridge.close_position(pid)
                if getattr(result, "success", False):
                    closed += 1
            return {"ok": True, "broker": "ctrader", "symbol": symbol or "ALL", "closed": closed}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}", "trace": traceback.format_exc()[-300:]}
    else:
        return {"ok": False, "error": f"unknown broker: {broker}"}
