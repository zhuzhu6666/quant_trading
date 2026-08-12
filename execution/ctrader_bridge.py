"""
execution/ctrader_bridge.py — cTrader Open API 桥接（当前实盘主链）

设计目标:
  - 提供统一执行接口: connect/disconnect / market_buy / market_sell /
    close_position / get_positions / account_info / fetch_bars
  - 走 Twisted 异步 + Protobuf 消息 + 回调转 Deferred
  - Pepperstone demo 默认: host=demo.ctraderapi.com:5035, 无 password (走 access_token)
  - 安全闸: send_orders=False 时 market_buy/sell 仅打印不真发

当前项目实盘主链固定经此文件对接 cTrader，其它 MT5 路径属于历史/兼容残留。
"""
from __future__ import annotations

import logging
import math
import os
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Callable
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# Phase 4: 统一接口
from execution.base import (
    AccountInfo,
    AccountReconcileResult,
    BaseBrokerBridge,
    OrderResult,
    PositionInfo,
    PositionReconcileResult,
    ReconcileComponentFact,
)
from execution.ctrader_ssl_patch import _patch_ctrader_ssl_endpoint

try:
    from ctrader_open_api import Client, Protobuf, TcpProtocol
    from ctrader_open_api.messages import (
        OpenApiCommonMessages_pb2 as CommonMsg,
        OpenApiMessages_pb2 as TradeMsg,
    )
    from ctrader_open_api.messages.OpenApiCommonMessages_pb2 import (
        ProtoHeartbeatEvent,
    )
    from ctrader_open_api.messages.OpenApiModelMessages_pb2 import (
        ProtoOASymbol, ProtoOATrendbar, ProtoOAPosition, ProtoOATrader,
    )
    HAS_CTRADER = True
except ImportError:  # 包未装
    HAS_CTRADER = False


# ── 公共结果类型 ──────────────────────────────────────────

_ORDER_OUTCOMES = frozenset({"confirmed", "rejected", "unknown", "simulated"})
_RISK_REDUCTION_ACTIONS = frozenset({
    "amend_position_sltp",
    "close_position",
    "reduce_position",
})
_POSITION_SPOT_COMPONENT_MAX_AGE_SECONDS = 15.0

# cTrader Open API period enum values used by both historical and live
# trendbar requests.  Keep the mapping in one place so the online feed and
# the low-frequency durable replica cannot disagree on timeframe identity.
_CTRADER_PERIOD_MAP = {
    "M1": 1,
    "M2": 2,
    "M3": 3,
    "M4": 4,
    "M5": 5,
    "M10": 6,
    "M15": 7,
    "M30": 8,
    "H1": 9,
    "H4": 10,
    "H12": 11,
    "D1": 12,
    "W1": 13,
    "MN1": 14,
}
_CTRADER_PERIOD_TO_TIMEFRAME = {
    value: key for key, value in _CTRADER_PERIOD_MAP.items()
}
_LIVE_TRENDBAR_CACHE_LIMIT = 5000


def _parse_broker_raw_price(value: Any) -> float:
    """Parse protobuf double price fields without applying money scaling."""
    try:
        price = float(value)
    except (TypeError, ValueError):
        return 0.0
    return price if math.isfinite(price) and price > 0.0 else 0.0


def _parse_broker_money_amount(value: Any, money_digits: Any, *, default_digits: int = 2) -> float:
    """Parse protobuf monetary integers using their own moneyDigits field."""
    try:
        digits = default_digits if money_digits is None else int(money_digits)
        amount = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if digits < 0 or not math.isfinite(amount):
        return 0.0
    return amount / (10.0 ** digits)


def _extract_broker_schedule(symbol: Any) -> dict[str, Any]:
    """Keep the broker's weekly symbol intervals for session evaluation."""
    intervals: list[dict[str, int]] = []
    for interval in getattr(symbol, "schedule", ()) or ():
        try:
            start_second = int(getattr(interval, "startSecond", -1))
            end_second = int(getattr(interval, "endSecond", -1))
        except (TypeError, ValueError):
            continue
        if 0 <= start_second <= 604800 and 0 <= end_second <= 604800 and start_second != end_second:
            intervals.append(
                {
                    "start_second": start_second,
                    "end_second": end_second,
                }
            )
    if not intervals:
        return {}
    return {
        "timezone": str(getattr(symbol, "scheduleTimeZone", "") or "UTC"),
        "intervals": intervals,
    }


def _build_symbol_meta(symbol: Any, symbol_name: str = "") -> dict[str, Any]:
    meta = {
        "symbol_id": symbol.symbolId,
        "symbol_name": symbol_name or str(getattr(symbol, "symbolName", "") or ""),
        "digits": symbol.digits,
        "lot_size": symbol.lotSize,
        "api_min_volume": symbol.minVolume,
        "api_step_volume": symbol.stepVolume,
        "api_max_volume": symbol.maxVolume,
        "volume_unit": "api",
        "pip_position": symbol.pipPosition,
    }
    broker_schedule = _extract_broker_schedule(symbol)
    if broker_schedule:
        meta["broker_schedule"] = broker_schedule
    return meta


def _parse_broker_relative_price(value: Any, symbol_digits: Any) -> float:
    """Parse spot/trendbar relative integers; never use this for deal prices."""
    try:
        digits = int(symbol_digits)
        raw = float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
    if digits < 0 or not math.isfinite(raw) or raw <= 0.0:
        return 0.0
    return round(raw / 100_000.0, digits)


def _trendbar_to_row(trendbar: Any, symbol_digits: Any) -> dict[str, Any] | None:
    """Decode one cTrader relative-price trendbar into the shared bar shape."""
    try:
        ts = int(getattr(trendbar, "utcTimestampInMinutes", 0) or 0) * 60
        low_raw = int(getattr(trendbar, "low", 0) or 0)
        delta_open = int(getattr(trendbar, "deltaOpen", 0) or 0)
        delta_close = int(getattr(trendbar, "deltaClose", 0) or 0)
        delta_high = int(getattr(trendbar, "deltaHigh", 0) or 0)
        volume = int(getattr(trendbar, "volume", 0) or 0)
    except (TypeError, ValueError):
        return None
    if ts <= 0 or low_raw <= 0:
        return None
    digits = int(symbol_digits or 2)
    divisor = 100_000.0
    return {
        "time": ts,
        "open": round((low_raw + delta_open) / divisor, digits),
        "high": round((low_raw + delta_high) / divisor, digits),
        "low": round(low_raw / divisor, digits),
        "close": round((low_raw + delta_close) / divisor, digits),
        "volume": volume,
    }


def _classify_broker_deal_price(
    execution_price: Any,
    *,
    entry_price: Any = None,
) -> tuple[float, str]:
    """Keep raw deal prices only when their same-position scale is plausible."""
    price = _parse_broker_raw_price(execution_price)
    if price <= 0.0:
        return 0.0, "unknown"
    reference = _parse_broker_raw_price(entry_price)
    if reference > 0.0:
        ratio = price / reference
        if ratio < 0.1 or ratio > 10.0:
            return price, "unknown"
    return price, "broker_reported"


@dataclass(frozen=True)
class _BlockedRiskReductionIntent:
    intent_id: str
    status: str = "unknown"


@dataclass(frozen=True)
class CTraderOrderResult:
    """统一订单结果（供 cTrader 桥接返回并转换为 BaseBrokerBridge 的 OrderResult）"""
    success: bool
    order_id: int = 0
    position_id: int = 0
    error_code: str = ""
    comment: str = ""
    price: float = 0.0
    volume: float = 0.0
    outcome: str = ""
    intent_id: str = ""
    execution_intent_status: str = ""
    client_order_id: str = ""
    client_msg_id: str = ""

    def __post_init__(self) -> None:
        outcome = str(self.outcome or ("confirmed" if self.success else "rejected")).strip().lower()
        if outcome not in _ORDER_OUTCOMES:
            raise ValueError(f"invalid cTrader order outcome: {outcome!r}")
        expected_success = outcome in {"confirmed", "simulated"}
        if bool(self.success) != expected_success:
            raise ValueError(
                "CTraderOrderResult.success must be true only for "
                f"confirmed/simulated outcomes (outcome={outcome!r})"
            )
        object.__setattr__(self, "outcome", outcome)


# Phase 4: CTraderOrderResult -> OrderResult 转换
def _to_order_result(r: CTraderOrderResult) -> "OrderResult":
    return OrderResult(
        success=r.success, order_id=r.order_id, position_id=r.position_id,
        error_code=r.error_code, comment=r.comment, price=r.price, volume=r.volume,
        outcome=r.outcome, intent_id=r.intent_id,
        execution_intent_status=r.execution_intent_status,
        client_order_id=r.client_order_id, client_msg_id=r.client_msg_id,
    )


# ── 常量映射（保留历史命名风格，便于兼容抽象层对齐） ────────────

# cTrader order type 常量 (ProtoOAOrderType enum)
ORDER_TYPE = {
    "MARKET": 1,
    "LIMIT": 2,
    "STOP": 3,
    "STOP_LIMIT": 4,
}
# 方向
TRADE_SIDE = {
    "BUY": 1,
    "SELL": 2,
}
# Time-in-force
TIME_IN_FORCE = {
    "GOOD_TILL_CANCEL": 0,
    "FILL_OR_KILL": 1,
    "IMMEDIATE_OR_CANCEL": 2,
    "MARKET_ON_OPEN": 3,
    "MARKET_ON_CLOSE": 4,
}

# cTrader depositAssetId → ISO currency code
# ⚠️ Pepperstone demo 的资产ID映射与标准cTrader不同:
#   depositAssetId=4 → EUR (非JPY), 已由账户5817896实测确认
# 标准cTrader: 1=USD, 2=EUR, 3=GBP, 4=JPY (适用于其他broker)
# Pepperstone demo实测: 4=EUR
# 未知ID走 fallback "ASSET_{id}" 字符串
_ASSET_ID_TO_CODE = {
    1:  "USD",
    2:  "EUR",
    3:  "GBP",
    4:  "EUR",   # Pepperstone实测=EUR, 标准cTrader=JPY
    5:  "CHF",
    6:  "AUD",
    7:  "CAD",
    8:  "NZD",
    35: "AUD",  # cTrader 历史 ID 偏移
    36: "GBP",
    37: "USD",
}


# ── 主类 ────────────────────────────────────────────────

class CTraderBridge(BaseBrokerBridge):
    """
    cTrader Open API 桥接（当前实盘执行主通道）.

    异步客户端基于 Twisted Reactor; 对外暴露同步接口, 内部用 Deferred 等回包.
    Reactor 只在 connect()/disconnect() 期间运行, 其余方法阻塞等响应.
    """

    def __init__(self,
                 client_id: str = "",
                 client_secret: str = "",
                 access_token: str = "",
                 account_id: int = 0,
                 host: str = "demo.ctraderapi.com",
                 port: int = 5035,
                 symbol: str = "XAUUSD",
                 rate_limit_per_sec: int = 5,
                 request_timeout_sec: float = 10.0,
                 send_orders: bool = False,
                 proxy_url: str = "",
                 proxy_rdns: bool = True,
                 forced_symbol_id: int | None = None,
                 execution_outcome_v2_enabled: bool | None = None,
                 execution_intent_store: Any | None = None):
        if not HAS_CTRADER:
            raise ImportError("ctrader-open-api 未装; pip install ctrader-open-api")
        self.client_id = client_id
        self.client_secret = client_secret
        self.access_token = access_token
        self.account_id = int(account_id)
        self.host = host
        self.port = port
        self.symbol = symbol
        self.rate_limit_per_sec = rate_limit_per_sec
        self.request_timeout_sec = request_timeout_sec
        self.send_orders = send_orders  # 安全闸: False 时只打 log 不真发
        self.proxy_url = str(proxy_url or "").strip()
        self.proxy_rdns = bool(proxy_rdns)

        self._client: "Client | None" = None
        self._reactor = None
        self._connected = False
        self._connected_lock = threading.Lock()
        self._app_authed = False
        self._account_authed = False
        self._symbol_id: int | None = None
        self._symbol_meta: dict[str, Any] = {}
        self._forced_symbol_id = forced_symbol_id  # ProtoOASymbol 无 name, 需外部指定 ID
        if execution_outcome_v2_enabled is None:
            try:
                from backend.core.static_feature_flags import shared_static_feature_flags

                execution_outcome_v2_enabled = bool(
                    shared_static_feature_flags().ctrader_execution_outcome_v2_enabled
                )
            except Exception:
                # Configuration resolution must not make bridge construction
                # fail; when v2 is explicitly enabled, its persistence gate
                # still fails closed before any broker mutation.
                execution_outcome_v2_enabled = False
        self._execution_outcome_v2_enabled = bool(execution_outcome_v2_enabled)
        self._execution_intent_store = execution_intent_store
        self._server_version: str = "v0"  # ★ VersionReq 拿, 给后续 Req clientMsgId 用
        self._trader_login: int = 0  # account_info 返回的 traderLogin, 下单 fallback
        # audit 2026-06-08: 实时报价 (ProtoOASpotEvent 回调更新)
        self._spot_price: float | None = None
        self._spot_bid: float | None = None
        self._spot_ask: float | None = None
        self._spot_ts: float = 0.0
        self._spot_lock = threading.Lock()
        self._spot_subscribed_symbol_ids: set[int] = set()
        # Live trendbars are the hot-path market-data source.  They are kept
        # in the bridge process only; data_sync remains the sole durable bar
        # writer for the monthly DuckDB replica.
        self._live_trendbars: dict[str, dict[int, dict[str, Any]]] = {}
        self._live_trendbar_lock = threading.RLock()
        self._live_trendbar_subscribed_periods: set[int] = set()
        # ── 熔断 / 退避 ──
        self._fail_count: int = 0
        self._last_fail_time: float = 0.0
        self._backoff_lock = threading.Lock()
        self._last_error_time: float = 0.0
        self._last_error_msg: str = ""

        # ── 心跳 (每 10 秒 ProtoHeartbeatEvent, 防服务端空闲断开) ──
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None


        # ── 持久 reactor 线程 (auth 后不停, 保持连接活跃) ──
        self._reactor_started = False
        self._reactor_thread: threading.Thread | None = None
        self._connect_state_lock = threading.Lock()
        self._connect_inflight = False
        self._proxy_patched = False
        self._connect_attempt_id = 0
        self._connect_timeout_call = None
        self._positions_cache: dict[int, PositionInfo] = {}
        self._positions_cache_lock = threading.Lock()
        self._account_cache = AccountInfo()
        self._account_cache_lock = threading.Lock()
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._event_listeners_lock = threading.Lock()
        self._last_reconcile_at: float = 0.0
        self._last_deals_fetch_ok: bool = False
        self._positions_cache_observed_at: float = 0.0
        self._positions_cache_source: str = "cache"
        self._account_cache_observed_at: float = 0.0
        self._account_cache_source: str = "cache"
        # All broker mutations share one process-local serial boundary.  The
        # live safety plane is single-threaded, but emergency-close is an API
        # escape hatch and can overlap the loop.  Serializing here keeps the
        # fresh pre-check, RPC and outcome resolution atomic with respect to
        # another mutation in this bridge instance.
        self._broker_mutation_lock = threading.RLock()
        self._local_unknown_risk_mutations: dict[
            tuple[str, int], _BlockedRiskReductionIntent
        ] = {}

    def has_token(self) -> bool:
        """检查必要凭证是否已设置 (client_id + client_secret + access_token).
        用于 live_service 的 pre-flight 检查, 不做网络调用."""
        return bool(self.client_id and self.client_secret and self.access_token)

    def _apply_proxy_socket_patch(self) -> None:
        """Optionally route cTrader sockets through a configured proxy."""
        if not self.proxy_url or self._proxy_patched:
            return
        try:
            import socks  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "proxy configured but PySocks is not installed; run `pip install PySocks`"
            ) from e

        parsed = urlparse(self.proxy_url)
        scheme = (parsed.scheme or "").lower()
        host = parsed.hostname or ""
        port = int(parsed.port or 0)
        if not host or port <= 0:
            raise RuntimeError(f"invalid CTRADER proxy URL: {self.proxy_url!r}")
        proxy_type_map = {
            "socks5": socks.SOCKS5,
            "socks5h": socks.SOCKS5,
            "socks4": socks.SOCKS4,
            "http": socks.HTTP,
            "https": socks.HTTP,
        }
        proxy_type = proxy_type_map.get(scheme)
        if proxy_type is None:
            raise RuntimeError(
                f"unsupported CTRADER proxy scheme {scheme!r}; use socks5:// or http://"
            )
        socks.set_default_proxy(
            proxy_type,
            host,
            port,
            rdns=self.proxy_rdns,
            username=parsed.username,
            password=parsed.password,
        )
        if socket.socket is not socks.socksocket:
            socket.socket = socks.socksocket
        self._proxy_patched = True
        logger.info("cTrader proxy enabled via %s://%s:%s", scheme, host, port)

    def _ensure_reactor(self):
        """确保 Twisted reactor 在后台 daemon 线程运行。幂等。
        Reactor 进程级单例, 启动后不再停止, 保持 cTrader 长连接活跃。"""
        if self._reactor_started:
            return
        self._reactor_started = True
        try:
            from twisted.internet import reactor as default_reactor
        except Exception as e:
            self._reactor_started = False
            raise RuntimeError(f"Twisted reactor import failed: {e}")
        self._reactor = default_reactor
        if self._reactor.running:
            return
        # 用 callWhenRunning 等 reactor 就绪
        _ready = threading.Event()
        self._reactor.callWhenRunning(lambda: _ready.set())

        def _run():
            self._reactor.run(installSignalHandlers=False)

        t = threading.Thread(target=_run, daemon=True, name="ctrader-reactor")
        t.start()
        self._reactor_thread = t
        if not _ready.wait(timeout=5.0):
            raise RuntimeError("Reactor failed to start within 5s")

    def _teardown_client(self) -> None:
        """Stop the current SDK client and drop the reference.

        When auth fails, leaving the client alive lets the SDK keep reconnecting
        in the background, which can create a sustained CPU storm. We keep the
        process-level reactor, but aggressively dispose the per-connect client.
        """
        client = self._client
        self._client = None
        if client is None:
            return
        try:
            if self._reactor is not None and self._reactor_thread is not None and threading.current_thread() != self._reactor_thread:
                self._reactor.callFromThread(client.stopService)
            else:
                client.stopService()
        except Exception:
            pass

    # ── 连接管理 ──

    def connect(self) -> bool:
        """连 broker + App auth + Account auth + Symbol resolve。
        首次调用启动持久 reactor 线程 (auth 后不停, 保持 TCP 长连接);
        后续调用复用已运行的 reactor, 只做 auth 链。"""
        with self._connect_state_lock:
            if self.is_connected:
                logger.info("Already connected")
                return True
            if self._connect_inflight:
                logger.debug("cTrader connect already in progress")
                return False
            if self._should_backoff():
                logger.info(
                    "cTrader connect backoff active (retry in %.1fs)",
                    self.connect_backoff_seconds(),
                )
                return False
            self._connect_inflight = True
            self._connect_attempt_id += 1
            attempt_id = self._connect_attempt_id
            self._spot_subscribed_symbol_ids.clear()
            self._live_trendbar_subscribed_periods.clear()

        try:
            self._apply_proxy_socket_patch()
            # TLS SNI patch (reactor 启动前)
            _patch_ctrader_ssl_endpoint()
            self._ensure_reactor()

            # 结果通过 Event 传递
            self._conn_ready = threading.Event()
            self._auth_ok = False
            self._conn_ok = False

            def _do_connect():
                """在 reactor 线程内: 创建 Client → 等连接 → auth"""
                nonlocal self
                try:
                    if self._client:
                        self._teardown_client()
                    client = Client(self.host, self.port, TcpProtocol)
                    self._client = client
                except Exception as e:
                    logger.error(f"Client create failed: {e}")
                    self._conn_ready.set()
                    return

                def _is_stale() -> bool:
                    return attempt_id != self._connect_attempt_id or self._client is not client

                def _finish_connect() -> None:
                    timeout_call = self._connect_timeout_call
                    self._connect_timeout_call = None
                    try:
                        if timeout_call is not None and timeout_call.active():
                            timeout_call.cancel()
                    except Exception:
                        pass

                def _on_conn(c):
                    if _is_stale():
                        logger.debug("skip stale cTrader connect callback attempt=%s", attempt_id)
                        try:
                            client.stopService()
                        except Exception:
                            pass
                        return
                    logger.info("cTrader TCP+TLS connected, starting auth")
                    self._conn_ok = True
                    from twisted.internet import defer
                    import uuid
                    from ctrader_open_api import Protobuf

                    def _unwrap(resp):
                        return Protobuf.extract(resp)

                    def _step_app(_dummy=None):
                        if _is_stale():
                            raise RuntimeError("stale connect attempt during app auth")
                        req = TradeMsg.ProtoOAApplicationAuthReq()
                        req.clientId = self.client_id
                        req.clientSecret = self.client_secret
                        d = client.send(
                            req,
                            clientMsgId=str(uuid.uuid4()),
                            responseTimeoutInSeconds=self.request_timeout_sec,
                        )
                        d.addCallback(_unwrap)
                        def _check(resp):
                            if type(resp).__name__ == "ProtoOAErrorRes":
                                raise RuntimeError(f"App auth rejected: code={resp.errorCode} {resp.description!r}")
                            self._app_authed = True
                            logger.info(f"App auth OK (clientId={self.client_id})")
                        d.addCallback(_check)
                        return d

                    def _step_account(_dummy):
                        if _is_stale():
                            raise RuntimeError("stale connect attempt during account list")
                        rl = TradeMsg.ProtoOAGetAccountListByAccessTokenReq()
                        rl.accessToken = self.access_token
                        d = client.send(
                            rl,
                            clientMsgId=str(uuid.uuid4()),
                            responseTimeoutInSeconds=self.request_timeout_sec,
                        )
                        d.addCallback(_unwrap)
                        def _check(resp):
                            if type(resp).__name__ == "ProtoOAErrorRes":
                                raise RuntimeError(f"Account list rejected: code={resp.errorCode}")
                            accts = [a.ctidTraderAccountId for a in resp.ctidTraderAccount]
                            logger.info(f"accessToken 绑定账户: {accts}")
                            if not accts:
                                raise RuntimeError("accessToken 没绑任何 ctid 账户")
                            if self.account_id not in accts:
                                raise RuntimeError(f"account_id={self.account_id} 不在列表 {accts}")
                            r2 = TradeMsg.ProtoOAAccountAuthReq()
                            r2.ctidTraderAccountId = self.account_id
                            r2.accessToken = self.access_token
                            d2 = client.send(
                                r2,
                                clientMsgId=str(uuid.uuid4()),
                                responseTimeoutInSeconds=self.request_timeout_sec,
                            )
                            d2.addCallback(_unwrap)
                            def _check2(resp2):
                                if type(resp2).__name__ == "ProtoOAErrorRes":
                                    raise RuntimeError(f"Account auth rejected: code={resp2.errorCode}")
                                self._account_authed = True
                                logger.info(f"Account auth OK (account={self.account_id})")
                            d2.addCallback(_check2)
                            return d2
                        d.addCallback(_check)
                        return d

                    def _step_symbol(_dummy):
                        if _is_stale():
                            raise RuntimeError("stale connect attempt during symbol list")
                        req = TradeMsg.ProtoOASymbolsListReq()
                        req.ctidTraderAccountId = self.account_id
                        d = client.send(
                            req,
                            clientMsgId=str(uuid.uuid4()),
                            responseTimeoutInSeconds=self.request_timeout_sec,
                        )
                        d.addCallback(_unwrap)
                        def _check(resp):
                            if type(resp).__name__ == "ProtoOAErrorRes":
                                raise RuntimeError(f"Symbol list rejected: code={resp.errorCode}")
                            for s in resp.symbol:
                                if s.symbolName == self.symbol:
                                    self._symbol_id = s.symbolId
                                    logger.info(f"Symbol {self.symbol} id={self._symbol_id}")
                                    req2 = TradeMsg.ProtoOASymbolByIdReq()
                                    req2.ctidTraderAccountId = self.account_id
                                    req2.symbolId.append(s.symbolId)
                                    d2 = client.send(
                                        req2,
                                        clientMsgId=str(uuid.uuid4()),
                                        responseTimeoutInSeconds=self.request_timeout_sec,
                                    )
                                    d2.addCallback(_unwrap)

                                    def _check_full(resp2):
                                        if type(resp2).__name__ == "ProtoOAErrorRes":
                                            logger.warning(
                                                "Symbol metadata unavailable: code=%s",
                                                getattr(resp2, "errorCode", ""),
                                            )
                                            return resp2
                                        if not resp2.symbol:
                                            logger.warning("Symbol metadata response is empty")
                                            return resp2
                                        try:
                                            self._symbol_meta = _build_symbol_meta(resp2.symbol[0], self.symbol)
                                        except Exception as exc:
                                            logger.warning("Symbol metadata parse failed: %s", exc)
                                            return resp2
                                        logger.info(
                                            "Symbol schedule loaded: timezone=%s intervals=%d",
                                            self._symbol_meta.get("broker_schedule", {}).get("timezone", ""),
                                            len(self._symbol_meta.get("broker_schedule", {}).get("intervals", [])),
                                        )
                                        return resp2

                                    def _metadata_failed(failure):
                                        logger.warning("Symbol metadata request failed: %s", failure)
                                        return None

                                    d2.addCallback(_check_full)
                                    d2.addErrback(_metadata_failed)
                                    return d2
                            raise RuntimeError(f"Symbol {self.symbol} not found in list")
                        d.addCallback(_check)
                        return d

                    def _on_done(_):
                        if _is_stale():
                            logger.debug("ignore stale cTrader auth success attempt=%s", attempt_id)
                            return
                        _finish_connect()
                        self._auth_ok = True
                        self._record_success()
                        self._start_heartbeat()
                        logger.info("cTrader fully authenticated")
                        # ★ 在 reactor 线程内设置 _connected, 避免竞态
                        with self._connected_lock:
                            self._connected = True
                        self._conn_ready.set()
                        # ★ 不再 stop reactor — 保持 TCP 长连接活跃

                    def _on_error(f):
                        if _is_stale():
                            logger.debug("ignore stale cTrader auth error attempt=%s", attempt_id)
                            return
                        _finish_connect()
                        logger.error(f"cTrader auth failed: {f.getErrorMessage()}")
                        self._teardown_client()
                        self._mark_disconnected()
                        self._conn_ready.set()
                        # Keep the process-level reactor, but tear down the failed
                        # client instance so the SDK cannot spin in reconnect loops.

                    chain = defer.succeed(None)
                    chain.addCallback(_step_app)
                    chain.addCallback(_step_account)
                    chain.addCallback(_step_symbol)
                    chain.addCallback(_on_done)
                    chain.addErrback(_on_error)

                client.setConnectedCallback(_on_conn)
                client.setDisconnectedCallback(lambda c, r: self._on_disconnected(c, r))
                client.setMessageReceivedCallback(lambda c, m: self._on_message(c, m))
                try:
                    client.startService()
                except Exception as e:
                    logger.warning(f"startService: {e}")
                    _finish_connect()
                    self._conn_ready.set()

            # 超时兜底
            timeout = max(self.request_timeout_sec * 4 + 5, 30.0)

            def _on_connect_timeout():
                if self._conn_ready.is_set():
                    return
                logger.error("cTrader connect/auth timeout after %.1fs", timeout)
                self._teardown_client()
                self._mark_disconnected()
                self._conn_ready.set()

            self._connect_timeout_call = self._reactor.callLater(timeout, _on_connect_timeout)

            # 在已运行的 reactor 线程内执行 _do_connect
            self._reactor.callFromThread(_do_connect)

            # 等 auth 完成 (reactor 线程会 set _conn_ready)
            if not self._conn_ready.wait(timeout=timeout + 5):
                logger.error(f"Connect/auth timeout after {timeout}s")
                self._teardown_client()
                self._mark_disconnected()
                self._record_failure()
                return False

            if not self._conn_ok or not self._auth_ok:
                logger.error(f"Connect/auth failed: conn={self._conn_ok} auth={self._auth_ok}")
                self._teardown_client()
                self._mark_disconnected()
                self._record_failure()
                return False

            return True
        finally:
            with self._connect_state_lock:
                self._connect_inflight = False

    def _version_handshake(self) -> bool:
        """保留方法, 当前未使用 (官方 sample 不发 VersionReq)"""
        return True  # skip

    def _on_connected(self):
        logger.debug("cTrader Twisted: connected callback fired")

    def _on_disconnected(self, client, reason):
        if client is not None and self._client is not None and client is not self._client:
            logger.debug("ignore stale cTrader disconnect callback")
            return
        logger.warning(f"cTrader Twisted: disconnected ({reason})")
        self._stop_heartbeat()
        self._client = None
        self._mark_disconnected()

    # ── 熔断 / 退避 ─────────────────────────────────────

    def _mark_disconnected(self):
        """Reset all connection flags. Thread-safe.
        Call when connection is confirmed dead — from _on_disconnected,
        _send timeouts, or API call failures."""
        with self._connected_lock:
            self._connected = False
        self._app_authed = False
        self._account_authed = False
        self._spot_subscribed_symbol_ids.clear()
        self._live_trendbar_subscribed_periods.clear()

    def _should_backoff(self) -> bool:
        """指数退避检查: 连续失败越久, 跳过时间越长.
        退避期内直接返回空值, 不调 _send(), 给 Twisted reactor
        和被 cTrader 限流的账号喘息时间.
        Backoff = min(300, 2^fail_count) 秒, max 5 分钟."""
        with self._backoff_lock:
            if self._fail_count == 0:
                return False
            elapsed = time.time() - self._last_fail_time
            # 2^fail_count 指数退避, cap 300s
            backoff = min(300, 1 << min(self._fail_count, 8))
            return elapsed < backoff

    def _record_failure(self):
        """记录一次失败, 增加退避计数."""
        with self._backoff_lock:
            self._fail_count += 1
            self._last_fail_time = time.time()

    def _record_success(self):
        """成功一次 → 清零退避计数."""
        with self._backoff_lock:
            self._fail_count = 0

    def connect_backoff_seconds(self) -> float:
        """Return remaining connect backoff seconds."""
        with self._backoff_lock:
            if self._fail_count == 0:
                return 0.0
            backoff = min(300, 1 << min(self._fail_count, 8))
            remaining = backoff - (time.time() - self._last_fail_time)
            return max(0.0, remaining)

    def _should_log_error(self, msg: str) -> bool:
        """相同错误聚合: 同一消息 60 秒内只打一次,
        避免超时风暴灌满磁盘."""
        now = time.time()
        if msg == self._last_error_msg and now - self._last_error_time < 60:
            return False
        self._last_error_msg = msg
        self._last_error_time = now
        return True

    def _is_soft_timeout_error(self, err: Exception) -> bool:
        text = f"{type(err).__name__}: {err}"
        return "TimeoutError" in text or "Deferred" in text and "10.0" in text

    # ── 心跳 (每 10s, 防服务端空闲断开) ──────────────────────

    def _start_heartbeat(self):
        """启动后台心跳线程, 每 10 秒发 ProtoHeartbeatEvent."""
        if self._heartbeat_thread and self._heartbeat_thread.is_alive():
            return  # 已在跑
        self._heartbeat_stop.clear()

        def _worker():
            while not self._heartbeat_stop.is_set():
                try:
                    if self._client and self._client.isConnected:
                        # cTrader heartbeat is fire-and-forget; consume the Deferred
                        # so Twisted timeout cancellations do not become unhandled.
                        def _send_hb():
                            try:
                                hb = ProtoHeartbeatEvent()
                                d = self._client.send(hb)
                                if hasattr(d, "addBoth"):
                                    d.addBoth(lambda result: None)
                            except Exception:
                                logger.debug("heartbeat send failed (non-fatal)")
                        self._reactor.callFromThread(_send_hb)
                except Exception:
                    logger.debug("heartbeat scheduling failed (non-fatal)")
                self._heartbeat_stop.wait(10.0)

        self._heartbeat_thread = threading.Thread(
            target=_worker, daemon=True, name="ctrader-heartbeat",
        )
        self._heartbeat_thread.start()
        logger.debug("[heartbeat] started (every 10s)")

    def _stop_heartbeat(self):
        """停止心跳线程."""
        self._heartbeat_stop.set()
        self._heartbeat_thread = None

    def _on_message(self, client, message):
        """消息回调: 提取 payload 分发到各处理器."""
        try:
            from ctrader_open_api import Protobuf
            payload = Protobuf.extract(message)
            self._handle_spot_event(payload)
            self._handle_execution_event(payload)
            self._handle_trader_update_event(payload)
        except Exception as e:
            logger.warning(f"_on_message parse failed: {e}")

    def add_event_listener(self, listener: Callable[[str, dict[str, Any]], None]) -> None:
        with self._event_listeners_lock:
            if listener not in self._event_listeners:
                self._event_listeners.append(listener)

    def _emit_event(self, event_type: str, payload: dict[str, Any]) -> None:
        with self._event_listeners_lock:
            listeners = list(self._event_listeners)
        for listener in listeners:
            try:
                listener(event_type, payload)
            except Exception as exc:
                logger.debug("cTrader event listener failed (%s): %s", event_type, exc)

    def _copy_account_cache(self) -> AccountInfo:
        with self._account_cache_lock:
            acct = self._account_cache
            return AccountInfo(
                balance=float(acct.balance or 0.0),
                equity=float(acct.equity or 0.0),
                margin=float(acct.margin or 0.0),
                margin_free=float(acct.margin_free or 0.0),
                margin_level=float(acct.margin_level or 0.0),
                leverage=float(acct.leverage or 0.0),
                currency=str(acct.currency or "USD"),
                account_id=int(acct.account_id or 0),
                name=str(acct.name or ""),
            )

    def _set_account_cache(self, account: AccountInfo, *, emit: bool = True, reason: str = "") -> AccountInfo:
        cached = AccountInfo(
            balance=float(account.balance or 0.0),
            equity=float(account.equity or 0.0),
            margin=float(account.margin or 0.0),
            margin_free=float(account.margin_free or 0.0),
            margin_level=float(account.margin_level or 0.0),
            leverage=float(account.leverage or 0.0),
            currency=str(account.currency or "USD"),
            account_id=int(account.account_id or 0),
            name=str(account.name or ""),
        )
        with self._account_cache_lock:
            self._account_cache = cached
            self._account_cache_observed_at = time.time()
            self._account_cache_source = (
                "event" if reason in {"trader_updated", "execution_event"} else "cache"
            )
        if emit:
            self._emit_event("account", {"account": cached, "reason": reason or "cache_update"})
        return cached

    def _copy_position(self, pos: PositionInfo) -> PositionInfo:
        return PositionInfo(
            position_id=int(pos.position_id or 0),
            symbol_id=int(pos.symbol_id or 0),
            symbol=str(pos.symbol or ""),
            direction=int(pos.direction or 0),
            volume=float(pos.volume or 0.0),
            entry_price=float(pos.entry_price or 0.0),
            current_price=float(pos.current_price or 0.0),
            sl=float(pos.sl or 0.0),
            tp=float(pos.tp or 0.0),
            pnl=float(pos.pnl or 0.0),
            commission=float(pos.commission or 0.0),
            swap=float(pos.swap or 0.0),
            open_timestamp=float(pos.open_timestamp or 0.0),
            current_price_state=str(pos.current_price_state or ""),
            current_price_source=str(pos.current_price_source or ""),
            current_price_observed_at=float(pos.current_price_observed_at or 0.0),
            current_price_reason_code=str(pos.current_price_reason_code or ""),
            pnl_state=str(pos.pnl_state or ""),
            pnl_source=str(pos.pnl_source or ""),
            pnl_observed_at=float(pos.pnl_observed_at or 0.0),
            pnl_reason_code=str(pos.pnl_reason_code or ""),
        )

    def _positions_snapshot(self) -> list[PositionInfo]:
        with self._positions_cache_lock:
            return [self._copy_position(pos) for pos in self._positions_cache.values()]

    def _set_positions_cache(self, positions: list[PositionInfo], *, emit: bool = True, reason: str = "") -> list[PositionInfo]:
        snapshot = [self._copy_position(pos) for pos in positions]
        with self._positions_cache_lock:
            self._positions_cache = {int(pos.position_id): self._copy_position(pos) for pos in snapshot if int(pos.position_id or 0) > 0}
            self._positions_cache_observed_at = time.time()
            self._positions_cache_source = "event" if reason == "execution_event" else "cache"
        if emit:
            self._emit_event("positions", {"positions": self._positions_snapshot(), "reason": reason or "cache_update"})
        return snapshot

    def _merge_position_cache(self, position: PositionInfo, *, emit: bool = True, reason: str = "") -> PositionInfo:
        copied = self._copy_position(position)
        with self._positions_cache_lock:
            self._positions_cache[int(copied.position_id)] = self._copy_position(copied)
            self._positions_cache_observed_at = time.time()
            self._positions_cache_source = "event"
        if emit:
            self._emit_event("positions", {"positions": self._positions_snapshot(), "reason": reason or "position_update"})
        return copied

    def _remove_position_cache(self, position_id: int, *, emit: bool = True, reason: str = "") -> None:
        with self._positions_cache_lock:
            self._positions_cache.pop(int(position_id or 0), None)
            self._positions_cache_observed_at = time.time()
            self._positions_cache_source = "event"
        if emit:
            self._emit_event("positions", {"positions": self._positions_snapshot(), "reason": reason or "position_remove"})

    def _recompute_account_equity_from_cache(self, *, emit: bool = True, reason: str = "") -> AccountInfo:
        account = self._copy_account_cache()
        positions = self._positions_snapshot()
        # An execution/reconcile identity event can arrive while the separate
        # unrealized-PnL RPC is unavailable.  Preserve the last account
        # projection instead of turning every unknown position into zero PnL.
        if positions and any(str(pos.pnl_state or "").lower() != "known" for pos in positions):
            return account
        unrealized = sum(float(pos.pnl or 0.0) for pos in positions)
        account.equity = float(account.balance or 0.0) + unrealized
        return self._set_account_cache(account, emit=emit, reason=reason or "equity_recompute")

    def _account_from_trader(self, trader: Any, *, unrealized: float | None = None) -> AccountInfo:
        if trader is None:
            return self._copy_account_cache()
        balance = float(getattr(trader, "balance", 0.0) or 0.0) / 100.0
        login = int(getattr(trader, "traderLogin", 0) or 0)
        self._trader_login = login or self._trader_login
        deposit_asset_id = int(getattr(trader, "depositAssetId", 0) or 0)
        currency = _ASSET_ID_TO_CODE.get(deposit_asset_id, f"ASSET_{deposit_asset_id}" if deposit_asset_id else "USD")
        leverage_in_cents = float(getattr(trader, "leverageInCents", 0) or 0)
        leverage = leverage_in_cents / 100.0 if leverage_in_cents > 0 else 0.0
        if unrealized is None:
            unrealized = sum(float(pos.pnl or 0.0) for pos in self._positions_snapshot())
        return AccountInfo(
            balance=balance,
            equity=balance + float(unrealized or 0.0),
            margin=0.0,
            margin_free=0.0,
            leverage=leverage,
            currency=currency,
            account_id=login,
            name=f"cTrader-{login}" if login else "",
        )

    def _position_from_proto(self, proto_position: Any) -> PositionInfo | None:
        if proto_position is None:
            return None
        td = getattr(proto_position, "tradeData", None)
        if td is None:
            return None
        symbol_id = int(getattr(td, "symbolId", 0) or 0)
        if symbol_id <= 0:
            return None
        direction = 1 if getattr(td, "tradeSide", 0) == TRADE_SIDE["BUY"] else -1
        current_price = float(getattr(proto_position, "currentPrice", 0.0) or 0.0)
        price_known = bool(current_price > 0)
        return PositionInfo(
            position_id=int(getattr(proto_position, "positionId", 0) or 0),
            symbol_id=symbol_id,
            symbol=self.symbol,
            direction=direction,
            volume=float(getattr(td, "volume", 0.0) or 0.0),
            entry_price=float(getattr(proto_position, "price", 0.0) or 0.0),
            current_price=current_price,
            sl=float(getattr(proto_position, "stopLoss", 0.0) or 0.0),
            tp=float(getattr(proto_position, "takeProfit", 0.0) or 0.0),
            pnl=float(getattr(proto_position, "netUnrealizedPnL", 0.0) or getattr(proto_position, "grossUnrealizedPnL", 0.0) or 0.0),
            commission=float(getattr(proto_position, "commission", 0.0) or 0.0) / 100.0,
            swap=float(getattr(proto_position, "swap", 0.0) or 0.0) / 100.0,
            open_timestamp=(float(getattr(td, "openTimestamp", 0.0) or 0.0) / 1000.0) if getattr(td, "openTimestamp", 0) else 0.0,
            current_price_state="known" if price_known else "unknown",
            current_price_source="ctrader_execution_event" if price_known else "",
            current_price_observed_at=time.time() if price_known else 0.0,
            current_price_reason_code="" if price_known else "execution_event_current_price_missing",
            # Protobuf scalar zero does not distinguish a known flat PnL from
            # an omitted projection on execution events.  Only the dedicated
            # PnL RPC establishes this component as known.
            pnl_state="unknown",
            pnl_source="ctrader_execution_event",
            pnl_reason_code="execution_event_pnl_not_authoritative",
        )

    def _handle_spot_event(self, payload):
        """处理实时报价更新."""
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
            if not isinstance(payload, ProtoOASpotEvent):
                return
            self._handle_live_trendbar_event(payload)
            raw_bid = payload.bid or 0
            raw_ask = payload.ask or 0
            meta = getattr(self, '_symbol_meta', None) or {}
            digits = meta.get('digits', 2)
            bid = _parse_broker_relative_price(raw_bid, digits)
            ask = _parse_broker_relative_price(raw_ask, digits)
            logger.debug(f"spot raw: bid={raw_bid} ask={raw_ask} digits={digits} → bid={bid:.2f} ask={ask:.2f}")
            with self._spot_lock:
                if bid > 0:
                    self._spot_bid = bid
                if ask > 0:
                    self._spot_ask = ask
                if bid > 0 and ask > 0:
                    self._spot_price = (bid + ask) / 2.0
                elif bid > 0:
                    self._spot_price = bid
                elif ask > 0:
                    self._spot_price = ask
                if self._spot_price and self._spot_price > 0:
                    self._spot_ts = time.time()
                spot = self._spot_price
            if spot and spot > 0:
                with self._positions_cache_lock:
                    for pos in self._positions_cache.values():
                        if self._symbol_id is not None and int(pos.symbol_id or 0) != int(self._symbol_id):
                            continue
                        pos.current_price = spot
                        pos.current_price_state = "known"
                        pos.current_price_source = "ctrader_spot"
                        pos.current_price_observed_at = float(self._spot_ts or time.time())
                        pos.current_price_reason_code = ""
                self._emit_event(
                    "spot",
                    {
                        "price": float(spot),
                        "bid": float(bid or 0.0),
                        "ask": float(ask or 0.0),
                        "ts": float(self._spot_ts or time.time()),
                        "symbol": self.symbol,
                    },
                )
        except Exception as e:
            logger.warning(f"spot event parse failed: {e}")

    def _handle_live_trendbar_event(self, payload) -> None:
        """Store the closed trendbar carried by a subscribed spot event."""
        raw_trendbars = getattr(payload, "trendbar", None)
        if raw_trendbars is None:
            return
        try:
            trendbars = tuple(raw_trendbars)
        except TypeError:
            trendbars = (raw_trendbars,)
        if not trendbars:
            return
        payload_symbol_id = int(getattr(payload, "symbolId", 0) or 0)
        if (
            payload_symbol_id
            and self._symbol_id is not None
            and payload_symbol_id != int(self._symbol_id)
        ):
            return
        for trendbar in trendbars:
            period = int(getattr(trendbar, "period", 0) or 0)
            timeframe = _CTRADER_PERIOD_TO_TIMEFRAME.get(period, "")
            if not timeframe:
                with self._live_trendbar_lock:
                    subscribed = tuple(self._live_trendbar_subscribed_periods)
                if len(subscribed) == 1:
                    timeframe = _CTRADER_PERIOD_TO_TIMEFRAME.get(subscribed[0], "")
            if not timeframe:
                logger.debug("live trendbar ignored: unknown period=%s", period)
                continue
            row = _trendbar_to_row(
                trendbar,
                (getattr(self, "_symbol_meta", None) or {}).get("digits", 2),
            )
            if row is None:
                logger.debug(
                    "live trendbar ignored: invalid payload timeframe=%s",
                    timeframe,
                )
                continue
            with self._live_trendbar_lock:
                bars = self._live_trendbars.setdefault(timeframe, {})
                is_new_bar = int(row["time"]) not in bars
                bars[int(row["time"])] = dict(row)
                if len(bars) > _LIVE_TRENDBAR_CACHE_LIMIT:
                    oldest = sorted(bars)[: len(bars) - _LIVE_TRENDBAR_CACHE_LIMIT]
                    for timestamp in oldest:
                        bars.pop(timestamp, None)
            log_fn = logger.info if is_new_bar else logger.debug
            log_fn(
                "live trendbar received: %s ts=%s close=%.5f%s",
                timeframe,
                int(row["time"]),
                float(row["close"]),
                "" if is_new_bar else " (update)",
            )
            self._emit_event(
                "trendbar",
                {
                    "symbol": self.symbol,
                    "timeframe": timeframe,
                    "bar": dict(row),
                    "source": "ctrader_live_trendbar",
                    "observed_at": time.time(),
                },
            )

    def _handle_execution_event(self, payload) -> None:
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOAExecutionEvent
            if not isinstance(payload, ProtoOAExecutionEvent):
                return
            position = self._position_from_proto(getattr(payload, "position", None))
            raw_position = getattr(payload, "position", None)
            deal = getattr(payload, "deal", None)
            reason = str(getattr(payload, "executionType", "") or "execution_event")
            position_id = int(getattr(position, "position_id", 0) or getattr(deal, "positionId", 0) or 0)
            close_detail = getattr(deal, "closePositionDetail", None)
            closed_volume = float(getattr(close_detail, "closedVolume", 0.0) or 0.0)
            position_status = int(getattr(raw_position, "positionStatus", 0) or 0)
            if position_id > 0:
                remaining_volume = float(getattr(position, "volume", 0.0) or 0.0)
                with self._positions_cache_lock:
                    cached_position = self._positions_cache.get(position_id)
                    cached_volume = float(getattr(cached_position, "volume", 0.0) or 0.0)
                is_closed_status = position_status == 2  # ProtoOAPositionStatus.POSITION_STATUS_CLOSED
                full_close = bool(
                    is_closed_status
                    or (position is None and closed_volume > 0)
                    or (remaining_volume <= 0 and closed_volume > 0)
                    or (cached_volume > 0 and closed_volume >= cached_volume)
                )
                if full_close:
                    self._remove_position_cache(position_id, emit=False, reason=reason)
                elif position is not None:
                    self._merge_position_cache(position, emit=False, reason=reason)
                else:
                    logger.debug(
                        "execution event for pos=%s without position snapshot; leaving cache unchanged",
                        position_id,
                    )
                self._emit_event("positions", {"positions": self._positions_snapshot(), "reason": reason})
                self._recompute_account_equity_from_cache(emit=True, reason=reason)
            self._emit_event(
                "execution",
                {
                    "reason": reason,
                    "position_id": position_id,
                    "deal_id": int(getattr(deal, "dealId", 0) or 0),
                    "closed_volume": closed_volume,
                    "is_server_event": bool(getattr(payload, "isServerEvent", False)),
                },
            )
        except Exception as exc:
            logger.debug("execution event parse failed: %s", exc)

    def _handle_trader_update_event(self, payload) -> None:
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOATraderUpdatedEvent
            if not isinstance(payload, ProtoOATraderUpdatedEvent):
                return
            trader = getattr(payload, "trader", None)
            account = self._account_from_trader(trader)
            self._set_account_cache(account, emit=True, reason="trader_updated")
        except Exception as exc:
            logger.debug("trader updated event parse failed: %s", exc)

    def subscribe_spots(self, symbol_id: int | None = None) -> bool:
        """订阅实时报价 (ProtoOASubscribeSpotsReq). 成功后 _on_message 会持续收到 spot event."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq
        sid = symbol_id or self._symbol_id
        if not sid:
            logger.error("subscribe_spots: no symbol_id")
            return False
        sid = int(sid)
        if sid in self._spot_subscribed_symbol_ids:
            return True
        try:
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId.append(sid)
            self._send(req, timeout=5.0)
            self._spot_subscribed_symbol_ids.add(sid)
            logger.info(f"subscribe_spots OK for symbol_id={sid}")
            return True
        except Exception as e:
            if "ALREADY_SUBSCRIBED" in str(e):
                self._spot_subscribed_symbol_ids.add(sid)
                logger.info(f"subscribe_spots already active for symbol_id={sid}")
                return True
            logger.warning(f"subscribe_spots failed: {e}")
            return False

    def live_trendbars_need_subscription(
        self,
        timeframes: tuple[str, ...] | list[str] | str | None = None,
    ) -> bool:
        """Return whether any requested live trendbar stream is not active."""
        if isinstance(timeframes, str):
            requested = (timeframes,)
        else:
            requested = tuple(timeframes or ("M5",))
        periods = {
            _CTRADER_PERIOD_MAP.get(str(timeframe or "").upper(), 0)
            for timeframe in requested
        }
        periods.discard(0)
        with self._live_trendbar_lock:
            return any(
                period not in self._live_trendbar_subscribed_periods
                for period in periods
            )

    def subscribe_live_trendbars(
        self,
        timeframes: tuple[str, ...] | list[str] | str | None = None,
        symbol_id: int | None = None,
    ) -> bool:
        """Subscribe to cTrader live closed trendbars for the requested frames."""
        if isinstance(timeframes, str):
            requested = (timeframes,)
        else:
            requested = tuple(timeframes or ("M5",))
        sid = symbol_id or self._symbol_id
        if not sid:
            logger.error("subscribe_live_trendbars: no symbol_id")
            return False
        ok = True
        for raw_timeframe in requested:
            timeframe = str(raw_timeframe or "").upper()
            period = _CTRADER_PERIOD_MAP.get(timeframe)
            if period is None:
                logger.error("subscribe_live_trendbars: unknown timeframe=%s", timeframe)
                ok = False
                continue
            with self._live_trendbar_lock:
                if period in self._live_trendbar_subscribed_periods:
                    continue
            try:
                req = TradeMsg.ProtoOASubscribeLiveTrendbarReq()
                req.ctidTraderAccountId = self.account_id
                req.symbolId = int(sid)
                req.period = period
                self._send(req, timeout=5.0)
                with self._live_trendbar_lock:
                    self._live_trendbar_subscribed_periods.add(period)
                logger.info(
                    "subscribe_live_trendbars OK: symbol_id=%s timeframe=%s",
                    sid,
                    timeframe,
                )
            except Exception as exc:
                if "ALREADY_SUBSCRIBED" in str(exc):
                    with self._live_trendbar_lock:
                        self._live_trendbar_subscribed_periods.add(period)
                    continue
                ok = False
                logger.warning(
                    "subscribe_live_trendbars failed: timeframe=%s error=%s",
                    timeframe,
                    exc,
                )
        return ok

    def seed_live_bars(self, timeframe: str, frame: Any) -> int:
        """Seed the in-memory online feed from startup history without writing DuckDB."""
        normalized_timeframe = str(timeframe or "").upper()
        if normalized_timeframe not in _CTRADER_PERIOD_MAP or frame is None:
            return 0
        rows: dict[int, dict[str, Any]] = {}
        try:
            for index, row in frame.iterrows():
                timestamp = int(
                    index.timestamp()
                    if hasattr(index, "timestamp")
                    else float(index)
                )
                if timestamp <= 0:
                    continue
                rows[timestamp] = {
                    "time": timestamp,
                    "open": float(row["open"]),
                    "high": float(row["high"]),
                    "low": float(row["low"]),
                    "close": float(row["close"]),
                    "volume": int(row.get("volume", 0) or 0),
                }
        except Exception as exc:
            logger.warning(
                "seed_live_bars failed: timeframe=%s error=%s",
                normalized_timeframe,
                exc,
            )
            return 0
        if not rows:
            return 0
        with self._live_trendbar_lock:
            bars = self._live_trendbars.setdefault(normalized_timeframe, {})
            bars.update(rows)
            if len(bars) > _LIVE_TRENDBAR_CACHE_LIMIT:
                oldest = sorted(bars)[: len(bars) - _LIVE_TRENDBAR_CACHE_LIMIT]
                for timestamp in oldest:
                    bars.pop(timestamp, None)
        return len(rows)

    def get_live_bars(self, timeframe: str = "M5", n_bars: int = 500) -> Any:
        """Return a copy of the in-memory online trendbar frame."""
        normalized_timeframe = str(timeframe or "").upper()
        try:
            import pandas as pd
        except ImportError:
            return None
        with self._live_trendbar_lock:
            rows = [
                dict(row)
                for _, row in sorted(
                    self._live_trendbars.get(normalized_timeframe, {}).items()
                )[-max(1, int(n_bars or 1)) :]
            ]
        if not rows:
            return None
        frame = pd.DataFrame(rows)
        frame["time"] = pd.to_datetime(frame["time"], unit="s", utc=True)
        frame = frame.set_index("time").sort_index()
        return frame[["open", "high", "low", "close", "volume"]]

    def get_spot_price(self) -> float | None:
        """线程安全读最新 spot 价."""
        with self._spot_lock:
            return self._spot_price

    def get_spot_quote(self) -> dict:
        """Return latest bid/ask/mid quote and timestamp for session/SL guards."""
        with self._spot_lock:
            return {
                "bid": self._spot_bid,
                "ask": self._spot_ask,
                "mid": self._spot_price,
                "ts": self._spot_ts,
                "source": "ctrader_spot",
            }

    def disconnect(self):
        """停 client service + 关连接; reactor 留着(全局 reactor 不能跨进程 stop)"""
        self._stop_heartbeat()
        if self._client:
            self._teardown_client()
        # 不调 reactor.stop() — 那是全局 reactor, 关了影响别的
        with self._connected_lock:
            self._connected = False
        self._app_authed = False
        self._account_authed = False
        self._spot_subscribed_symbol_ids.clear()
        logger.info("cTrader disconnected (reactor 留着, 给下次 connect 复用)")

    @property
    def is_connected(self) -> bool:
        with self._connected_lock:
            return (self._connected and self._app_authed
                    and self._account_authed and self._symbol_id is not None)

    @property
    def is_connecting(self) -> bool:
        with self._connect_state_lock:
            return bool(self._connect_inflight)

    # ── 内部: App auth / Account auth ──

    def _send(
        self,
        msg,
        timeout: float | None = None,
        *,
        client_msg_id: str = "",
    ) -> Any:
        """Proactive: 走 Client.send, 返回回包 protobuf. 同步阻塞.

        ⚠️ SDK Client.send() 回调给的是 ProtoMessage wrapper (payloadType + payload 字节),
        不是直接 Res 类. 必须 Protobuf.extract(message) 才解出真 Res, 才能 access 字段.

        ⚠️ server 拒请求时回 ProtoOAErrorRes, 提取 errorCode/description 后 raise

        ⚠️ 'wrong random id' 修复: clientMsgId 必须 UUID 格式字符串.
        SDK 0.9.2 默认 str(id(deferred)) 跟 server 期望的格式不匹配.
        官方 sample 显式传 str(uuid.uuid4()).
        """
        if not self._client:
            raise RuntimeError("Not connected")
        timeout = timeout or self.request_timeout_sec
        from twisted.internet import defer
        result_holder: dict = {}
        effective_client_msg_id = str(client_msg_id or uuid.uuid4())

        def _do_send():
            d = self._client.send(
                msg,
                clientMsgId=effective_client_msg_id,  # UUID 格式, 可用于 intent 对账
                responseTimeoutInSeconds=timeout,
            )
            d.addCallback(lambda wrapper: result_holder.update({
                "ok": True,
                "resp": Protobuf.extract(wrapper),
            }))
            d.addErrback(lambda f: result_holder.update({"ok": False, "err": f}))
            return d

        # 如果在 reactor 线程内 (如 _on_conn 回调), callFromThread + busy-wait 会死锁
        # → 直接调用 _do_send, 然后用 doIteration 给 reactor 时间处理响应。
        # ★ 持久 reactor 修复: 用线程 ID 判断 (不是 reactor.running, 因为 reactor 在后台线程跑)
        in_reactor_thread = (
            self._reactor_thread is not None
            and threading.current_thread() == self._reactor_thread
        )
        if not in_reactor_thread:
            self._reactor.callFromThread(_do_send)
            deadline = time.time() + timeout + 1.0
            while not result_holder and time.time() < deadline:
                time.sleep(0.05)
        else:
            _do_send()
            deadline = time.time() + timeout + 1.0
            while not result_holder and time.time() < deadline:
                try: self._reactor.doIteration(0.05)
                except: time.sleep(0.05)
        if not result_holder:
            raise TimeoutError(f"cTrader send timeout: {type(msg).__name__}")
        if not result_holder.get("ok"):
            err = result_holder.get("err")
            raise RuntimeError(f"cTrader send error: {err}")

        resp = result_holder["resp"]
        if type(resp).__name__ == "ProtoOAErrorRes":
            err_code = getattr(resp, "errorCode", "?")
            desc = getattr(resp, "description", "")
            raise RuntimeError(
                f"cTrader server rejected {type(msg).__name__}: "
                f"errorCode={err_code} description={desc!r}"
            )
        return resp

    def _app_auth(self) -> bool:
        req = TradeMsg.ProtoOAApplicationAuthReq()
        req.clientId = self.client_id
        req.clientSecret = self.client_secret
        try:
            self._send(req)
            self._app_authed = True
            logger.info(f"App auth OK (clientId={self.client_id})")
            return True
        except Exception as e:
            logger.error(f"App auth failed: {e}")
            return False

    def _account_auth(self) -> bool:
        """
        Account auth 流程 (按 cTrader 文档 step 8-9):
          step 8: ProtoOAGetAccountListByAccessTokenReq(accessToken)
                   → 拿 ctidTraderAccountId + permissionScope 列表
          step 9: ProtoOAAccountAuthReq(ctidTraderAccountId, accessToken)
                   → 实际认证选定账户

        ⚠️ 文档原话: "After receiving a response, the app sends the
        ProtoOAAccountAuthReq message ... the user should be authenticated
        under the account the ctidTraderAccountId of which matches the
        ctidTraderAccountId provided during step 8."
        跳过 step 8 → server 不知道 accessToken 跟哪个账户配对 → 立即 close
        """
        # step 8: 用 accessToken 查账户列表
        try:
            req_list = TradeMsg.ProtoOAGetAccountListByAccessTokenReq()
            req_list.accessToken = self.access_token
            resp_list = self._send(req_list)
            accounts = [a.ctidTraderAccountId for a in resp_list.ctidTraderAccount]
            logger.info(f"accessToken 绑定账户: ctidTraderAccountId list = {accounts}")
            if not accounts:
                logger.error("accessToken 没绑任何 ctid 账户 (broker 没批?)")
                return False
            # 看 account_id 是否在列表里
            if self.account_id not in accounts:
                logger.error(
                    f"请求的 account_id={self.account_id} 不在 accessToken 绑定列表 {accounts}。"
                    f"  → 多半 broker 没把这个 app 关联到对应 demo 账户,"
                    f"  → 或者 accessToken 是别 cTID 用户的 (不是注册 app 那个)"
                )
                return False
            # 顺便打印 permissionScope 验证 trading scope 在
            # ⚠️ permissionScope 在父 Res ProtoOAGetAccountListByAccessTokenRes 上,
            # 不在每个 ProtoOACtidTraderAccount 上(2025+ SDK 字段位置变了)
            logger.info(f"  accessToken permissionScope = {resp_list.permissionScope}")
        except Exception as e:
            logger.error(f"GetAccountListByAccessToken failed: {e}")
            return False

        # step 9: 真 AccountAuth
        req = TradeMsg.ProtoOAAccountAuthReq()
        req.ctidTraderAccountId = self.account_id
        req.accessToken = self.access_token
        try:
            self._send(req)
            self._account_authed = True
            logger.info(f"Account auth OK (account={self.account_id})")
            return True
        except Exception as e:
            logger.error(f"Account auth failed: {e}")
            return False

    def _resolve_symbol_id(self):
        """
        查 SymbolId 缓存, 给 NewOrderReq 用.

        ⚠️ ProtoOASymbolsListRes 给的是 ProtoOALightSymbol (只有 symbolId, symbolName 等基础字段,
           没有 digits/lotSize/minVolume 这些 metadata).
        拿 digits/lotSize 必须再发 ProtoOASymbolByIdReq 拿完整 ProtoOASymbol.

        流程:
          1) SymbolsListReq → 找 self.symbol 名字匹配的 symbolId (XAUUSD)
          2) SymbolByIdReq 拿完整 ProtoOASymbol → digits / lotSize / minVolume / stepVolume
          3) 缓存到 self._symbol_id + self._symbol_meta 供 NewOrderReq 用
        """
        try:
            # 1) 列 symbol 找 ID
            req = TradeMsg.ProtoOASymbolsListReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=15.0)
            target_id = None
            target_name = None
            for sym in resp.symbol:
                if sym.symbolName.upper() == self.symbol.upper():
                    target_id = sym.symbolId
                    target_name = sym.symbolName
                    break
            if target_id is None:
                # 退路: 用前 5 个候选名 + 第一个作为 forced
                names = [s.symbolName for s in resp.symbol[:5]]
                logger.warning(
                    f"symbol {self.symbol!r} not in list (first 5: {names}). "
                    f"Use --symbol to override."
                )
                if self._forced_symbol_id is None and resp.symbol:
                    target_id = resp.symbol[0].symbolId
                    target_name = resp.symbol[0].symbolName
                    logger.info(f"Fallback to first symbol: {target_name} (id={target_id})")
                else:
                    return []
            logger.info(f"Symbol resolved: {target_name} → id={target_id}")

            # 2) 拿完整 metadata
            req2 = TradeMsg.ProtoOASymbolByIdReq()
            req2.ctidTraderAccountId = self.account_id
            req2.symbolId.append(target_id)
            resp2 = self._send(req2, timeout=15.0)
            full = resp2.symbol[0]
            meta = _build_symbol_meta(full, target_name)
            self._symbol_id = full.symbolId
            self._symbol_meta = meta
            logger.info(f"Symbol meta: {meta}")
            return [meta]
        except Exception as e:
            logger.warning(f"resolve_symbol_id failed: {e}")
            return []

    # ── 行情 ──

    def get_symbols_list(self) -> list[dict]:
        """返回 broker 上前 10 个 symbol (调试用, 因为 ProtoOASymbol 没 name 字段)"""
        req = TradeMsg.ProtoOASymbolsListReq()
        req.ctidTraderAccountId = self.account_id
        try:
            resp = self._send(req, timeout=15.0)
            return [{
                "symbol_id": s.symbolId,
                "digits": s.digits,
                "lot_size": s.lotSize,
                "api_min_volume": s.minVolume,
                "api_step_volume": s.stepVolume,
                "api_max_volume": s.maxVolume,
                "volume_unit": "api",
            } for s in resp.symbol[:10]]
        except Exception as e:
            logger.error(f"get_symbols_list failed: {e}")
            return []

    # ── 订单 ──

    def market_buy(self, symbol: str = "", volume: float = 0.0,
                   sl: float = 0.0, tp: float = 0.0,
                   comment: str = "", *, decision_id: str = "",
                   trade_id: str = "", risk_verdict: dict | None = None) -> OrderResult:
        _sym = symbol or self.symbol
        r = self._send_market_order(
            TRADE_SIDE["BUY"], volume, sl, tp, comment,
            decision_id=decision_id, trade_id=trade_id,
            risk_verdict=risk_verdict,
        )
        return _to_order_result(r)

    def market_sell(self, symbol: str = "", volume: float = 0.0,
                    sl: float = 0.0, tp: float = 0.0,
                    comment: str = "", *, decision_id: str = "",
                    trade_id: str = "", risk_verdict: dict | None = None) -> OrderResult:
        _sym = symbol or self.symbol
        r = self._send_market_order(
            TRADE_SIDE["SELL"], volume, sl, tp, comment,
            decision_id=decision_id, trade_id=trade_id,
            risk_verdict=risk_verdict,
        )
        return _to_order_result(r)

    def amend_position_sltp(self, position_id: int,
                            sl: float = 0.0, tp: float = 0.0,
                            trailing: bool = False,
                            guaranteed: bool = False) -> CTraderOrderResult:
        with self._broker_mutation_lock:
            return self._amend_position_sltp_serial(
                position_id,
                sl=sl,
                tp=tp,
                trailing=trailing,
                guaranteed=guaranteed,
            )

    def _amend_position_sltp_serial(self, position_id: int,
                                    sl: float = 0.0, tp: float = 0.0,
                                    trailing: bool = False,
                                    guaranteed: bool = False) -> CTraderOrderResult:
        """
        REFACTOR-8 (audit 2026-06-06): 把 SL/TP 推到 server 端 (消除 1 bar 延迟)

        调 ProtoOAAmendPositionSLTPReq, 改指定 position 的 stopLoss/takeProfit.
        cTrader 限制: MARKET 单不能下单时带 SL/TP (per OpenAPI 文档),
        必须成交后 amend. 本方法专治这个.

        Args:
            position_id: ProtoOAPosition.positionId (从 get_positions() 拿)
            sl: 绝对 SL 价格 (e.g. 2034.50). 0 表示不修改当前 SL
            tp: 绝对 TP 价格 (e.g. 2050.00). 0 表示不修改当前 TP
            trailing: 是否启用 trailing stop loss (默认 False)
            guaranteed: 是否启用 guaranteed stop loss (默认 False, French Risk 账户才支持)

        Returns:
            CTraderOrderResult(success, position_id, comment, error_code)

        ⚠️ 风险:
            1. amend 失败不应重试, 因为 broker 那边 SL/TP 可能已经被 modify 过,
               重试会出现 stale price 错误. caller 负责 log + alarm.
            2. 缩放: cTrader server 接受 absolute price, 但 ProtoOAPosition 里
               stopLoss 是 moneyDigits-scaled. amend 时 caller 传真实价格 (e.g. 2034.5),
               server 自己 scale. 不像 ClosePositionReq 的 volume * 100.
        """
        # 按照 symbol digits 舍入, 避免 cTrader 拒绝 (如 4329.751677557901)
        digits = getattr(self, '_symbol_meta', {}).get('digits', 2)
        if sl > 0:
            sl = round(float(sl), digits)
        if tp > 0:
            tp = round(float(tp), digits)

        if not self._connected or not self._account_authed:
            return CTraderOrderResult(
                success=False, position_id=position_id,
                comment="Not connected/authed"
            )
        if position_id is None or position_id <= 0:
            return CTraderOrderResult(
                success=False, position_id=position_id or 0,
                comment="position_id required"
            )
        # DRY-RUN 安全闸 (跟 market/close 一致)
        if not self.send_orders:
            logger.warning(
                f"[DRY-RUN] amend_position_sltp pos={position_id} "
                f"sl={sl} tp={tp} trailing={trailing} (send_orders=False)"
            )
            return CTraderOrderResult(
                success=True, outcome="simulated", position_id=position_id,
                comment=f"DRY-RUN amend sl={sl} tp={tp} (send_orders=False)",
            )

        pre_result = None
        if self._execution_outcome_v2_enabled:
            pre_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        if pre_result is not None and pre_result.fresh and not any(
            int(item.position_id) == int(position_id)
            for item in pre_result.positions
        ):
            self._local_unknown_risk_mutations.pop(
                ("amend_position_sltp", int(position_id)),
                None,
            )
            try:
                from backend.services.live_safety_state import (
                    resolve_broker_outcome_mutation,
                )

                resolve_broker_outcome_mutation(
                    action="amend_position_sltp",
                    position_id=int(position_id),
                    outcome="confirmed",
                    evidence={
                        "source": "fresh_pre_reconcile",
                        "reconcile_id": str(pre_result.reconcile_id or ""),
                        "position_present": False,
                    },
                )
            except Exception as exc:
                logger.error("local amend unknown resolution append failed: %s", exc)
            return CTraderOrderResult(
                success=True,
                outcome="confirmed",
                position_id=int(position_id),
                comment=f"Position {position_id} already absent in fresh broker reconcile",
            )
        store, intent_id, client_msg_id, blocked_intent = self._prepare_risk_reduction_intent(
            action="amend_position_sltp",
            position_id=int(position_id),
            target_stop_loss=float(sl or 0.0),
            target_take_profit=float(tp or 0.0),
            request={
                "trailing": bool(trailing),
                "guaranteed": bool(guaranteed),
                "pre_reconcile_id": getattr(pre_result, "reconcile_id", ""),
                "pre_reconcile_status": getattr(pre_result, "status", "not_requested"),
            },
        )
        if blocked_intent is not None:
            self._latch_unknown_broker_outcome(
                action="amend_position_sltp",
                position_id=int(position_id),
                intent_id=intent_id,
                evidence={
                    "reason": "unresolved_risk_reduction_intent",
                    "existing_status": str(getattr(blocked_intent, "status", "unknown") or "unknown"),
                },
            )
            return CTraderOrderResult(
                success=False,
                outcome="unknown",
                position_id=int(position_id),
                error_code="DUPLICATE_MUTATION_BLOCKED",
                comment="unresolved amend intent must be recovered before resubmission",
                intent_id=intent_id,
                execution_intent_status=(
                    "persisted" if store is not None else "compat_missing_intent"
                ),
            )
        req = TradeMsg.ProtoOAAmendPositionSLTPReq()
        req.ctidTraderAccountId = self.account_id
        req.positionId = int(position_id)
        if sl > 0:
            req.stopLoss = float(sl)
        if tp > 0:
            req.takeProfit = float(tp)
        if trailing:
            req.trailingStopLoss = True
        if guaranteed:
            req.guaranteedStopLoss = True

        resp = None
        send_error: Exception | None = None
        try:
            resp = self._send(req, timeout=10.0, client_msg_id=client_msg_id)
        except Exception as exc:
            send_error = exc
            logger.error(
                "amend_position_sltp RPC outcome uncertain pos=%s sl=%s tp=%s: %s",
                position_id, sl, tp, exc,
            )
        response = self._response_evidence(resp)
        response.update({
            "client_msg_id": client_msg_id,
            "target_stop_loss": float(sl or 0.0),
            "target_take_profit": float(tp or 0.0),
        })
        rejected = self._response_is_explicit_rejection(response)
        if rejected:
            err_code = str(response.get("error_code") or "broker_rejected")
            self._finalize_risk_reduction_intent(
                store,
                intent_id,
                outcome="rejected",
                position_id=int(position_id),
                broker_response=response,
                error={"error_code": err_code, "description": response.get("description")},
            )
            return CTraderOrderResult(
                success=False,
                outcome="rejected",
                position_id=position_id,
                error_code=err_code,
                comment=f"amend rejected: {err_code} — {response.get('description') or ''}",
                intent_id=intent_id if store is not None else "",
                execution_intent_status=(
                    "persisted" if store is not None else "compat_missing_intent"
                ),
                client_msg_id=client_msg_id,
            )

        if not self._execution_outcome_v2_enabled:
            confirmed = (
                response.get("response_type") == "ProtoOAExecutionEvent"
                and send_error is None
            )
            outcome = "confirmed" if confirmed else "unknown"
            if not confirmed:
                self._latch_unknown_broker_outcome(
                    action="amend_position_sltp",
                    position_id=int(position_id),
                    intent_id=intent_id,
                    evidence={**response, "rpc_error": str(send_error or "")},
                )
            return CTraderOrderResult(
                success=confirmed,
                outcome=outcome,
                position_id=position_id,
                error_code="" if confirmed else "AMEND_OUTCOME_UNKNOWN",
                comment=(
                    "amend accepted by ExecutionEvent (compat mode)"
                    if confirmed
                    else str(send_error or f"unexpected response: {response.get('response_type') or 'none'}")
                ),
                execution_intent_status="compat_missing_intent",
                client_msg_id=client_msg_id,
            )

        post_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        current = next(
            (item for item in post_result.positions if int(item.position_id) == int(position_id)),
            None,
        ) if post_result.fresh else None
        tolerance = max(1e-9, 0.5 * (10.0 ** (-int(digits))))
        sl_matches = sl <= 0 or (current is not None and abs(float(current.sl or 0.0) - float(sl)) <= tolerance)
        tp_matches = tp <= 0 or (current is not None and abs(float(current.tp or 0.0) - float(tp)) <= tolerance)
        verifiable_fields = sl > 0 or tp > 0
        confirmed = bool(post_result.fresh and current is not None and verifiable_fields and sl_matches and tp_matches)
        resolution = {
            "post_reconcile_id": post_result.reconcile_id,
            "post_reconcile_status": post_result.status,
            "position_present": current is not None,
            "actual_stop_loss": float(current.sl or 0.0) if current is not None else None,
            "actual_take_profit": float(current.tp or 0.0) if current is not None else None,
            "sl_matches": bool(sl_matches),
            "tp_matches": bool(tp_matches),
            "rpc_error": str(send_error or ""),
        }
        outcome = "confirmed" if confirmed else "unknown"
        self._finalize_risk_reduction_intent(
            store,
            intent_id,
            outcome=outcome,
            position_id=int(position_id),
            broker_response={**response, "resolution": resolution},
            error={} if confirmed else {"reason": "amend_not_freshly_verified"},
        )
        if not confirmed:
            self._latch_unknown_broker_outcome(
                action="amend_position_sltp",
                position_id=int(position_id),
                intent_id=intent_id,
                evidence={**response, "resolution": resolution},
            )
        return CTraderOrderResult(
            success=confirmed,
            outcome=outcome,
            position_id=position_id,
            error_code="" if confirmed else "AMEND_OUTCOME_UNKNOWN",
            comment="amend verified by fresh broker reconcile" if confirmed else "amend not freshly verified",
            intent_id=intent_id if store is not None else "",
            execution_intent_status=(
                "persisted" if store is not None else "compat_missing_intent"
            ),
            client_msg_id=client_msg_id,
        )

    # Phase 4: 统一抽象接口方法
    def amend_sl_tp(self, position_id: int, sl: float, tp: float) -> bool:
        r = self.amend_position_sltp(position_id, sl=sl, tp=tp)
        return r.success

    def _intent_store(self):
        if self._execution_intent_store is None:
            from backend.services.broker_execution_intent import BrokerExecutionIntentStore

            self._execution_intent_store = BrokerExecutionIntentStore()
        return self._execution_intent_store

    def _prepare_risk_reduction_intent(
        self,
        *,
        action: str,
        position_id: int,
        requested_volume: float = 0.0,
        target_stop_loss: float = 0.0,
        target_take_profit: float = 0.0,
        request: dict[str, Any] | None = None,
    ) -> tuple[Any | None, str, str, Any | None]:
        """Best-effort intent ledger for close/reduce/tighten mutations.

        New risk requires PostgreSQL intent persistence before its RPC.  Risk
        reduction has the opposite availability rule: a state-store failure
        is written to the local safety outbox but must never suppress the
        broker mutation.  The returned client message id is always a UUID so
        broker transport evidence remains correlatable even without PG.
        """

        intent_id = str(uuid.uuid4())
        client_msg_id = str(uuid.uuid4())
        action_key = str(action or "").strip().lower()
        mutation_key = (action_key, int(position_id))
        try:
            from backend.services.live_safety_state import (
                no_new_risk_latch_status,
                unresolved_broker_outcome_mutations,
            )

            if not bool(no_new_risk_latch_status(fail_closed=True).get("active")):
                self._local_unknown_risk_mutations.clear()
            blocked_local = self._local_unknown_risk_mutations.get(mutation_key)
            if blocked_local is not None:
                return None, blocked_local.intent_id or intent_id, client_msg_id, blocked_local
            for durable in unresolved_broker_outcome_mutations():
                if (
                    str(durable.get("action") or "").strip().lower() == action_key
                    and int(durable.get("position_id") or 0) == int(position_id)
                ):
                    blocked = _BlockedRiskReductionIntent(
                        intent_id=str(durable.get("intent_id") or intent_id),
                    )
                    self._local_unknown_risk_mutations[mutation_key] = blocked
                    return None, blocked.intent_id, client_msg_id, blocked
        except Exception as exc:
            # A failed durable lookup cannot suppress a first risk reduction.
            # If that RPC becomes unknown, _latch_unknown_broker_outcome keeps
            # a process-local identity before attempting durable persistence.
            logger.error("unknown risk-reduction mutation lookup unavailable: %s", exc)
        if not self._execution_outcome_v2_enabled:
            return None, intent_id, client_msg_id, None
        try:
            store = self._intent_store()
            for existing in store.unresolved(
                account_id=str(self.account_id),
                symbol=str(self.symbol),
            ):
                existing_request = dict(getattr(existing, "request", None) or {})
                if (
                    str(getattr(existing, "action", "") or "").strip().lower()
                    == str(action or "").strip().lower()
                    and int(existing_request.get("position_id") or 0) == int(position_id)
                ):
                    # Never replay an unresolved mutation.  Recovery uses fresh
                    # broker facts and is the only path allowed to resolve it.
                    return (
                        store,
                        str(getattr(existing, "intent_id", "") or intent_id),
                        client_msg_id,
                        existing,
                    )
            prepared = store.prepare(
                intent_id=intent_id,
                idempotency_key=intent_id,
                broker="ctrader",
                account_id=str(self.account_id),
                symbol=str(self.symbol),
                action=str(action),
                side="",
                requested_volume=float(requested_volume or 0.0),
                target_stop_loss=float(target_stop_loss or 0.0),
                target_take_profit=float(target_take_profit or 0.0),
                request={
                    "schema": "broker_risk_reduction_request.v2",
                    "position_id": int(position_id),
                    "client_msg_id": client_msg_id,
                    "requested_volume": float(requested_volume or 0.0),
                    "target_stop_loss": float(target_stop_loss or 0.0),
                    "target_take_profit": float(target_take_profit or 0.0),
                    **dict(request or {}),
                },
            )
            intent_id = str(prepared.intent_id)
            store.mark_submitting(
                intent_id,
                request={**dict(prepared.request or {}), "submitting_at": time.time()},
            )
            return store, intent_id, client_msg_id, None
        except Exception as exc:
            logger.error("risk-reduction intent persistence unavailable; broker action continues: %s", exc)
            try:
                from backend.services.live_safety_state import append_safety_outbox

                append_safety_outbox(
                    event_type="broker_risk_reduction_intent_persist_failed",
                    correlation_id=intent_id,
                    payload={
                        "action": str(action),
                        "position_id": int(position_id),
                        "requested_volume": float(requested_volume or 0.0),
                        "target_stop_loss": float(target_stop_loss or 0.0),
                        "target_take_profit": float(target_take_profit or 0.0),
                        "client_msg_id": client_msg_id,
                    },
                    error=f"{type(exc).__name__}:{exc}",
                )
            except Exception as outbox_exc:
                logger.error("risk-reduction safety outbox also failed: %s", outbox_exc)
            return None, intent_id, client_msg_id, None

    @staticmethod
    def _finalize_risk_reduction_intent(
        store: Any | None,
        intent_id: str,
        *,
        outcome: str,
        position_id: int,
        broker_response: dict[str, Any],
        error: dict[str, Any] | None = None,
    ) -> None:
        """Finalize best-effort bookkeeping without rewriting broker truth."""

        if store is None:
            return
        try:
            store.complete(
                intent_id,
                outcome=outcome,
                position_id=position_id,
                broker_order_id=int(broker_response.get("order_id") or 0),
                broker_response=broker_response,
                error=dict(error or {}),
            )
        except Exception as exc:
            logger.error("risk-reduction intent finalize failed: %s", exc)
            try:
                from backend.services.live_safety_state import append_safety_outbox

                append_safety_outbox(
                    event_type="broker_risk_reduction_intent_finalize_failed",
                    correlation_id=intent_id,
                    payload={
                        "outcome": outcome,
                        "position_id": int(position_id),
                        "broker_response": broker_response,
                    },
                    error=f"{type(exc).__name__}:{exc}",
                )
            except Exception as outbox_exc:
                logger.error("risk-reduction finalize outbox also failed: %s", outbox_exc)

    def _latch_unknown_broker_outcome(
        self,
        *,
        action: str,
        position_id: int,
        intent_id: str,
        evidence: dict[str, Any],
    ) -> None:
        """Unknown broker mutation outcomes prohibit further new risk."""

        action_key = str(action or "").strip().lower()
        if action_key in _RISK_REDUCTION_ACTIONS and int(position_id or 0) > 0:
            self._local_unknown_risk_mutations[(action_key, int(position_id))] = (
                _BlockedRiskReductionIntent(intent_id=str(intent_id or ""))
            )

        try:
            from backend.services.live_safety_state import activate_no_new_risk_latch

            activate_no_new_risk_latch(
                reason="broker_execution_outcome_unknown",
                actor="execution:ctrader_bridge",
                correlation_id=intent_id,
                metadata={
                    "action": str(action),
                    "position_id": int(position_id),
                    "evidence": dict(evidence),
                },
            )
        except Exception as exc:
            # activate_no_new_risk_latch itself installs a process-local
            # fail-closed latch before raising on durable I/O failure.
            logger.critical("failed to durably latch unknown broker outcome: %s", exc)

    def unresolved_execution_intent_count(self) -> int:
        status = self.execution_intent_recovery_status()
        raw_count = status.get("unresolved_count")
        if raw_count is None:
            raise RuntimeError(str(status.get("error") or "execution intent status unavailable"))
        return int(raw_count or 0)

    def execution_intent_recovery_status(self) -> dict[str, Any]:
        try:
            from backend.services.live_safety_state import unresolved_broker_outcome_mutations

            durable_unknown = list(unresolved_broker_outcome_mutations())
        except Exception as exc:
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": False,
                "enabled": bool(self._execution_outcome_v2_enabled),
                "unresolved_count": None,
                "unresolved": [],
                "local_safety_latch_status": "unavailable",
                "error": (
                    "local_unknown_ledger_unavailable:"
                    f"{type(exc).__name__}:{exc}"
                )[:500],
            }
        if not self._execution_outcome_v2_enabled:
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": not durable_unknown,
                "enabled": False,
                "unresolved_count": len(durable_unknown),
                "unresolved": [
                    {**item, "source": "local_safety_latch"}
                    for item in durable_unknown
                ],
            }
        try:
            from backend.services.broker_execution_intent import execution_intent_recovery_status

            status = execution_intent_recovery_status(
                self._intent_store(), account_id=str(self.account_id), symbol=str(self.symbol)
            )
            unresolved = list(status.get("unresolved") or [])
            existing_ids = {
                str(item.get("intent_id") or "")
                for item in unresolved
                if str(item.get("intent_id") or "")
            }
            unresolved.extend(
                {**item, "source": "local_safety_latch"}
                for item in durable_unknown
                if not str(item.get("intent_id") or "")
                or str(item.get("intent_id") or "") not in existing_ids
            )
            return {
                **status,
                "enabled": True,
                "ready": not unresolved,
                "unresolved_count": len(unresolved),
                "unresolved": unresolved,
            }
        except Exception as exc:
            return {
                "schema": "broker_execution_intent_recovery.v1",
                "ready": False,
                "enabled": True,
                "unresolved_count": None,
                "unresolved": [],
                "error": f"{type(exc).__name__}:{exc}",
            }

    @staticmethod
    def _position_snapshot_payload(
        positions: list[PositionInfo] | tuple[PositionInfo, ...],
    ) -> dict[str, dict[str, Any]]:
        return {
            str(int(pos.position_id)): {
                "position_id": int(pos.position_id),
                "symbol_id": int(pos.symbol_id or 0),
                "direction": int(pos.direction or 0),
                "volume": float(pos.volume or 0.0),
                "open_timestamp": float(pos.open_timestamp or 0.0),
                "stop_loss": float(pos.sl or 0.0),
                "take_profit": float(pos.tp or 0.0),
            }
            for pos in positions
            if int(pos.position_id or 0) > 0
        }

    @staticmethod
    def _deal_snapshot_payload(deals: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {
            str(int(item.get("deal_id") or 0)): dict(item)
            for item in deals
            if int(item.get("deal_id") or 0) > 0
        }

    def _order_history_for_recovery(
        self,
        *,
        from_ts: float,
        to_ts: float | None = None,
    ) -> tuple[dict[str, dict[str, Any]], bool]:
        """Fetch immutable broker order evidence used only for intent recovery."""

        if not self.is_connected:
            return {}, False
        try:
            req = TradeMsg.ProtoOAOrderListReq()
            req.ctidTraderAccountId = self.account_id
            req.fromTimestamp = int(max(0.0, float(from_ts or 0.0)) * 1000)
            req.toTimestamp = int(float(to_ts or time.time()) * 1000)
            resp = self._send(req, timeout=15.0)
            payload: dict[str, dict[str, Any]] = {}
            for order in list(getattr(resp, "order", ()) or ()):
                order_id = int(getattr(order, "orderId", 0) or 0)
                if order_id <= 0:
                    continue
                trade_data = getattr(order, "tradeData", None)
                side_value = int(getattr(trade_data, "tradeSide", 0) or 0)
                payload[str(order_id)] = {
                    "order_id": order_id,
                    "position_id": int(getattr(order, "positionId", 0) or 0),
                    "symbol_id": int(getattr(trade_data, "symbolId", 0) or 0),
                    "trade_side": (
                        "buy" if side_value == TRADE_SIDE["BUY"]
                        else "sell" if side_value == TRADE_SIDE["SELL"]
                        else ""
                    ),
                    "client_order_id": str(getattr(order, "clientOrderId", "") or ""),
                    "comment": str(getattr(trade_data, "comment", "") or ""),
                    "label": str(getattr(trade_data, "label", "") or ""),
                    "volume": float(getattr(trade_data, "volume", 0.0) or 0.0),
                    "executed_volume": float(getattr(order, "executedVolume", 0.0) or 0.0),
                    "order_status": int(getattr(order, "orderStatus", 0) or 0),
                    "closing_order": bool(getattr(order, "closingOrder", False)),
                }
            return payload, True
        except Exception as exc:
            logger.warning("broker order history unavailable for intent recovery: %s", exc)
            return {}, False

    @staticmethod
    def _response_evidence(resp: Any) -> dict[str, Any]:
        if resp is None:
            return {}

        parse_errors: list[str] = []

        def safe_attr(value: Any, field: str, default: Any = None) -> Any:
            try:
                return getattr(value, field, default) if value is not None else default
            except Exception as exc:
                parse_errors.append(f"{field}:{type(exc).__name__}")
                return default

        def safe_int(*values: Any) -> int:
            for value in values:
                if value is None:
                    continue
                try:
                    if value == "" or value == 0:
                        continue
                    return int(value)
                except Exception as exc:
                    parse_errors.append(f"integer:{type(exc).__name__}")
            return 0

        def safe_text(value: Any) -> str:
            try:
                return str(value or "")
            except Exception as exc:
                parse_errors.append(f"text:{type(exc).__name__}")
                return ""

        position = safe_attr(resp, "position")
        order = safe_attr(resp, "order")
        deal = safe_attr(resp, "deal")
        evidence = {
            "response_type": type(resp).__name__,
            "error_code": safe_text(safe_attr(resp, "errorCode", "")),
            "description": safe_text(safe_attr(resp, "description", "")),
            "position_id": safe_int(
                safe_attr(position, "positionId", 0),
                safe_attr(deal, "positionId", 0),
            ),
            "order_id": safe_int(
                safe_attr(order, "orderId", 0),
                safe_attr(deal, "orderId", 0),
                safe_attr(resp, "orderId", 0),
            ),
            "deal_id": safe_int(safe_attr(deal, "dealId", 0)),
            "client_order_id": safe_text(safe_attr(order, "clientOrderId", "")),
        }
        if parse_errors:
            evidence["parse_errors"] = sorted(set(parse_errors))
        return evidence

    @staticmethod
    def _response_is_explicit_rejection(response: dict[str, Any]) -> bool:
        """Accept rejection only from a documented broker response type.

        A protobuf that is unknown to this client may still expose an
        ``errorCode`` field.  The field name alone is not enough to prove that
        an order was rejected; classifying it as terminal would permit a retry
        after an outcome that is actually unknown.
        """

        return str(response.get("response_type") or "") in {
            "ProtoOAOrderErrorEvent",
            "ProtoOAErrorRes",
        }

    @staticmethod
    def _resolve_open_differential(
        *,
        side: int,
        symbol_id: int,
        pre_positions: dict[str, dict[str, Any]],
        pre_deals: dict[str, dict[str, Any]],
        post_positions: dict[str, dict[str, Any]],
        post_deals: dict[str, dict[str, Any]],
        response: dict[str, Any],
        deals_differential_available: bool = True,
        post_orders: dict[str, dict[str, Any]] | None = None,
        orders_available: bool = False,
        client_order_id: str = "",
        comment_token: str = "",
    ) -> dict[str, Any]:
        """Resolve one open mutation only when all broker evidence is unique."""
        expected_direction = 1 if side == TRADE_SIDE["BUY"] else -1
        expected_side = "buy" if expected_direction == 1 else "sell"
        position_candidates: dict[int, set[str]] = {}
        correlated_position_ids: set[int] = set()
        order_candidates: set[int] = set()
        correlated_order_ids: set[int] = set()

        def add_position(raw: Any, source: str, *, correlated: bool = False) -> None:
            try:
                pid = int(raw or 0)
            except (TypeError, ValueError):
                pid = 0
            if pid > 0:
                position_candidates.setdefault(pid, set()).add(source)
                if correlated:
                    correlated_position_ids.add(pid)

        response_type = str(response.get("response_type") or "")
        # Only the documented execution event is an immutable broker receipt.
        # Unknown protobufs can expose similarly named ``position``/``order``
        # fields; treating those structural lookalikes as correlated evidence
        # would turn an unrecognised response into a false confirmation.  Such
        # responses may still be resolved by an independently unique fresh
        # position/deal/order differential below.
        response_is_execution = response_type == "ProtoOAExecutionEvent"
        response_pid = int(response.get("position_id") or 0) if response_is_execution else 0
        response_order_id = int(response.get("order_id") or 0) if response_is_execution else 0
        if response_pid > 0:
            add_position(response_pid, "execution_response", correlated=True)
        if response_order_id > 0:
            order_candidates.add(response_order_id)
            correlated_order_ids.add(response_order_id)

        normalized_client_order_id = str(client_order_id or "").strip()
        normalized_comment_token = str(comment_token or "").strip()
        for order in ((post_orders or {}).values() if orders_available else ()):
            order_client_id = str(order.get("client_order_id") or "").strip()
            order_comment = str(order.get("comment") or "")
            identity_match = bool(
                (normalized_client_order_id and order_client_id == normalized_client_order_id)
                or (normalized_comment_token and normalized_comment_token in order_comment)
            )
            if not identity_match:
                continue
            if symbol_id > 0 and int(order.get("symbol_id") or 0) != int(symbol_id):
                continue
            order_side = str(order.get("trade_side") or "").lower()
            if order_side and order_side != expected_side:
                continue
            order_id = int(order.get("order_id") or 0)
            if order_id > 0:
                order_candidates.add(order_id)
                correlated_order_ids.add(order_id)
            add_position(order.get("position_id"), "client_order_identity", correlated=True)

        for key, current in post_positions.items():
            if int(current.get("direction") or 0) != expected_direction:
                continue
            if symbol_id > 0 and int(current.get("symbol_id") or 0) != int(symbol_id):
                continue
            previous = pre_positions.get(str(key))
            if previous is None:
                add_position(current.get("position_id"), "new_position")
            elif float(current.get("volume") or 0.0) > float(previous.get("volume") or 0.0):
                add_position(current.get("position_id"), "position_volume_increase")

        for deal_id, deal in (post_deals.items() if deals_differential_available else ()):
            if deal_id in pre_deals:
                continue
            if str(deal.get("trade_side") or "").lower() != expected_side:
                continue
            if symbol_id > 0 and int(deal.get("symbol_id") or 0) != int(symbol_id):
                continue
            if dict(deal.get("close_detail") or {}):
                continue
            deal_order_id = int(deal.get("order_id") or 0)
            if correlated_order_ids and deal_order_id not in correlated_order_ids:
                continue
            if response_order_id > 0 and deal_order_id > 0 and deal_order_id != response_order_id:
                continue
            add_position(
                deal.get("position_id"),
                "correlated_new_deal" if deal_order_id in correlated_order_ids else "new_deal",
                correlated=deal_order_id in correlated_order_ids,
            )
            if deal_order_id > 0:
                order_candidates.add(deal_order_id)

        candidate_ids = sorted(correlated_position_ids or position_candidates)
        evidence = {str(pid): sorted(sources) for pid, sources in position_candidates.items()}
        if len(candidate_ids) != 1:
            return {
                "outcome": "unknown",
                "position_id": 0,
                "order_id": response_order_id if len(order_candidates) <= 1 else 0,
                "candidate_position_ids": candidate_ids,
                "candidate_order_ids": sorted(order_candidates),
                "correlated_position_ids": sorted(correlated_position_ids),
                "correlated_order_ids": sorted(correlated_order_ids),
                "evidence": evidence,
                "reason": "no_unique_position_match" if not candidate_ids else "multiple_position_matches",
            }
        return {
            "outcome": "confirmed",
            "position_id": candidate_ids[0],
            "order_id": response_order_id or (next(iter(order_candidates)) if len(order_candidates) == 1 else 0),
            "candidate_position_ids": candidate_ids,
            "candidate_order_ids": sorted(order_candidates),
            "correlated_position_ids": sorted(correlated_position_ids),
            "correlated_order_ids": sorted(correlated_order_ids),
            "evidence": evidence,
            "reason": (
                "unique_correlated_broker_match"
                if correlated_position_ids
                else "unique_broker_match"
            ),
        }

    @staticmethod
    def _resolve_close_intent_recovery(
        intent: Any,
        *,
        post_positions: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        request = dict(getattr(intent, "request", None) or {})
        position_id = int(request.get("position_id") or getattr(intent, "position_id", 0) or 0)
        if position_id <= 0:
            return {
                "outcome": "unknown",
                "position_id": 0,
                "order_id": 0,
                "reason": "close_position_identity_missing",
            }
        current = post_positions.get(str(position_id))
        if current is None:
            return {
                "outcome": "confirmed",
                "position_id": position_id,
                "order_id": 0,
                "reason": "close_target_absent_in_fresh_reconcile",
                "position_present": False,
            }
        before_volume = float(request.get("position_volume_before") or 0.0)
        requested_volume = float(
            getattr(intent, "requested_volume", 0.0)
            or request.get("requested_volume")
            or 0.0
        )
        current_volume = float(current.get("volume") or 0.0)
        expected_max_remaining = (
            max(0.0, before_volume - requested_volume)
            if before_volume > 0 and requested_volume > 0
            else None
        )
        confirmed_reduced = bool(
            expected_max_remaining is not None
            and current_volume < before_volume
            and current_volume <= expected_max_remaining + 1e-9
        )
        return {
            "outcome": "confirmed" if confirmed_reduced else "unknown",
            "position_id": position_id,
            "order_id": 0,
            "reason": (
                "close_volume_reduction_freshly_verified"
                if confirmed_reduced
                else "close_not_freshly_verified"
            ),
            "position_present": True,
            "position_volume_before": before_volume or None,
            "position_volume_after": current_volume,
            "expected_max_remaining": expected_max_remaining,
        }

    @staticmethod
    def _resolve_amend_intent_recovery(
        intent: Any,
        *,
        post_positions: dict[str, dict[str, Any]],
        digits: int,
    ) -> dict[str, Any]:
        request = dict(getattr(intent, "request", None) or {})
        position_id = int(request.get("position_id") or getattr(intent, "position_id", 0) or 0)
        if position_id <= 0:
            return {
                "outcome": "unknown",
                "position_id": 0,
                "order_id": 0,
                "reason": "amend_position_identity_missing",
            }
        current = post_positions.get(str(position_id))
        if current is None:
            # The position risk no longer exists.  Treat the intended
            # risk-reducing effect as terminal without claiming an amend fill.
            return {
                "outcome": "confirmed",
                "position_id": position_id,
                "order_id": 0,
                "reason": "amend_target_position_absent",
                "position_present": False,
            }
        target_sl = float(
            getattr(intent, "target_stop_loss", 0.0)
            or request.get("target_stop_loss")
            or 0.0
        )
        target_tp = float(
            getattr(intent, "target_take_profit", 0.0)
            or request.get("target_take_profit")
            or 0.0
        )
        if target_sl <= 0 and target_tp <= 0:
            return {
                "outcome": "unknown",
                "position_id": position_id,
                "order_id": 0,
                "reason": "amend_target_fields_missing",
                "position_present": True,
            }
        tolerance = max(1e-9, 0.5 * (10.0 ** (-max(0, int(digits)))))
        actual_sl = float(current.get("stop_loss") or 0.0)
        actual_tp = float(current.get("take_profit") or 0.0)
        sl_matches = target_sl <= 0 or abs(actual_sl - target_sl) <= tolerance
        tp_matches = target_tp <= 0 or abs(actual_tp - target_tp) <= tolerance
        confirmed = bool(sl_matches and tp_matches)
        return {
            "outcome": "confirmed" if confirmed else "unknown",
            "position_id": position_id,
            "order_id": 0,
            "reason": (
                "amend_targets_freshly_verified"
                if confirmed
                else "amend_not_freshly_verified"
            ),
            "position_present": True,
            "target_stop_loss": target_sl or None,
            "target_take_profit": target_tp or None,
            "actual_stop_loss": actual_sl,
            "actual_take_profit": actual_tp,
            "sl_matches": bool(sl_matches),
            "tp_matches": bool(tp_matches),
        }

    def recover_execution_intents(self) -> dict[str, Any]:
        """Resolve persisted broker intents by action without resubmitting."""
        if not self._execution_outcome_v2_enabled:
            return self._recover_local_execution_outcomes()
        store = self._intent_store()
        unresolved = store.unresolved(account_id=str(self.account_id), symbol=str(self.symbol))
        if not unresolved:
            return self._recover_local_execution_outcomes()
        positions_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        if not positions_result.fresh:
            return {
                **self.execution_intent_recovery_status(),
                "ready": False,
                "recovery_error": positions_result.error_code or "position_reconcile_failed",
            }
        earliest_prepared = min(
            (float(intent.prepared_at or 0.0) for intent in unresolved if intent.prepared_at),
            default=time.time() - 86400.0,
        )
        evidence_from_ts = max(0.0, earliest_prepared - 300.0)
        deals = self.get_deals(from_ts=int(evidence_from_ts), max_rows=1000)
        post_deals_available = bool(self._last_deals_fetch_ok)
        post_positions = self._position_snapshot_payload(positions_result.positions)
        post_deals = self._deal_snapshot_payload(deals)
        post_orders, post_orders_available = self._order_history_for_recovery(
            from_ts=evidence_from_ts,
        )
        recovered: list[dict[str, Any]] = []
        for intent in unresolved:
            request = dict(intent.request or {})
            action = str(intent.action or "").strip().lower()
            if (
                str(intent.status or "") == "prepared"
                and int(intent.attempt_count or 0) <= 0
                and action == "market_open"
            ):
                # New-risk submission fails closed when the durable
                # ``submitting`` transition is unavailable, so this state
                # proves that no market-open RPC was sent.  Risk-reducing
                # actions deliberately continue when that PG transition
                # fails; they must therefore be resolved from fresh broker
                # facts below instead of being mislabelled rejected.
                resolution = {
                    "outcome": "rejected",
                    "position_id": 0,
                    "order_id": 0,
                    "reason": "intent_never_reached_submitting",
                }
            elif action == "market_open" and intent.side.lower() in {"buy", "sell"}:
                resolution = self._resolve_open_differential(
                    side=(
                        TRADE_SIDE["BUY"]
                        if intent.side.lower() == "buy"
                        else TRADE_SIDE["SELL"]
                    ),
                    symbol_id=int(request.get("symbol_id") or self._symbol_id or 0),
                    pre_positions=dict(request.get("positions_before") or {}),
                    pre_deals=dict(request.get("deals_before") or {}),
                    post_positions=post_positions,
                    post_deals=post_deals,
                    response=dict(intent.broker_response or {}),
                    deals_differential_available=bool(
                        request.get("deals_before_available") and post_deals_available
                    ),
                    post_orders=post_orders,
                    orders_available=post_orders_available,
                    client_order_id=str(request.get("client_order_id") or ""),
                    comment_token=str(request.get("comment_token") or ""),
                )
            elif action in {"close_position", "reduce_position"}:
                resolution = self._resolve_close_intent_recovery(
                    intent,
                    post_positions=post_positions,
                )
            elif action == "amend_position_sltp":
                resolution = self._resolve_amend_intent_recovery(
                    intent,
                    post_positions=post_positions,
                    digits=int((getattr(self, "_symbol_meta", None) or {}).get("digits", 2)),
                )
            else:
                resolution = {
                    "outcome": "unknown",
                    "position_id": 0,
                    "order_id": 0,
                    "reason": f"unsupported_intent_action:{action or 'missing'}",
                }
            outcome = str(resolution.get("outcome") or "unknown")
            store.complete(
                intent.intent_id,
                outcome=outcome,
                position_id=int(resolution.get("position_id") or 0),
                broker_order_id=int(resolution.get("order_id") or 0),
                broker_response={**dict(intent.broker_response or {}), "recovery": resolution},
                error={} if outcome == "confirmed" else {"reason": resolution.get("reason")},
            )
            local_resolution: dict[str, Any] = {}
            if outcome in {"confirmed", "rejected"}:
                try:
                    from backend.services.live_safety_state import (
                        resolve_broker_outcome_mutation,
                    )

                    local_resolution = resolve_broker_outcome_mutation(
                        intent_id=str(intent.intent_id),
                        action=action,
                        position_id=int(resolution.get("position_id") or request.get("position_id") or 0),
                        outcome=outcome,
                        evidence={
                            "source": "fresh_broker_intent_recovery",
                            "reconcile_id": str(positions_result.reconcile_id or ""),
                            "resolution": resolution,
                        },
                    )
                    self._local_unknown_risk_mutations.pop(
                        (action, int(request.get("position_id") or 0)),
                        None,
                    )
                except Exception as exc:
                    # PostgreSQL recovery remains broker truth, but a failed
                    # local resolution append must keep admission fail closed.
                    logger.error("local unknown-outcome resolution append failed: %s", exc)
                    local_resolution = {
                        "status": "resolution_append_failed",
                        "error": f"{type(exc).__name__}:{exc}",
                    }
            recovered.append(
                {
                    "intent_id": intent.intent_id,
                    **resolution,
                    "local_resolution": local_resolution,
                }
            )
        return {**self.execution_intent_recovery_status(), "recovered": recovered}

    def _recover_local_execution_outcomes(self) -> dict[str, Any]:
        """Resolve compat-mode risk reductions from a fresh broker snapshot."""

        try:
            from backend.services.live_safety_state import (
                resolve_broker_outcome_mutation,
                unresolved_broker_outcome_mutations,
            )

            unresolved = list(unresolved_broker_outcome_mutations())
        except Exception as exc:
            return {
                **self.execution_intent_recovery_status(),
                "ready": False,
                "recovery_error": f"local_unknown_ledger_unavailable:{type(exc).__name__}:{exc}",
            }
        if not unresolved:
            return self.execution_intent_recovery_status()
        positions_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        if not positions_result.fresh:
            return {
                **self.execution_intent_recovery_status(),
                "ready": False,
                "recovery_error": positions_result.error_code or "position_reconcile_failed",
            }
        post_positions = self._position_snapshot_payload(positions_result.positions)
        digits = int((getattr(self, "_symbol_meta", None) or {}).get("digits", 2))
        recovered: list[dict[str, Any]] = []
        for item in unresolved:
            action = str(item.get("action") or "").strip().lower()
            position_id = int(item.get("position_id") or 0)
            evidence = dict(item.get("evidence") or {})
            nested_resolution = dict(evidence.get("resolution") or {})
            request = {
                "position_id": position_id,
                "position_volume_before": (
                    evidence.get("position_volume_before")
                    or nested_resolution.get("position_volume_before")
                ),
                "requested_volume": evidence.get("requested_volume"),
                "target_stop_loss": (
                    evidence.get("target_stop_loss")
                    or nested_resolution.get("target_stop_loss")
                ),
                "target_take_profit": (
                    evidence.get("target_take_profit")
                    or nested_resolution.get("target_take_profit")
                ),
            }
            local_intent = SimpleNamespace(
                request=request,
                position_id=position_id,
                requested_volume=float(request.get("requested_volume") or 0.0),
                target_stop_loss=float(request.get("target_stop_loss") or 0.0),
                target_take_profit=float(request.get("target_take_profit") or 0.0),
            )
            if action in {"close_position", "reduce_position"}:
                resolution = self._resolve_close_intent_recovery(
                    local_intent,
                    post_positions=post_positions,
                )
            elif action == "amend_position_sltp":
                resolution = self._resolve_amend_intent_recovery(
                    local_intent,
                    post_positions=post_positions,
                    digits=digits,
                )
            else:
                resolution = {
                    "outcome": "unknown",
                    "position_id": position_id,
                    "order_id": 0,
                    "reason": f"local_recovery_requires_persisted_identity:{action or 'missing'}",
                }
            outcome = str(resolution.get("outcome") or "unknown")
            local_resolution: dict[str, Any] = {}
            if outcome in {"confirmed", "rejected"}:
                try:
                    local_resolution = resolve_broker_outcome_mutation(
                        intent_id=str(item.get("intent_id") or ""),
                        action=action,
                        position_id=position_id,
                        outcome=outcome,
                        evidence={
                            "source": "fresh_local_ledger_recovery",
                            "reconcile_id": str(positions_result.reconcile_id or ""),
                            "resolution": resolution,
                        },
                    )
                    self._local_unknown_risk_mutations.pop((action, position_id), None)
                except Exception as exc:
                    logger.error("local unknown-outcome resolution append failed: %s", exc)
                    local_resolution = {
                        "status": "resolution_append_failed",
                        "error": f"{type(exc).__name__}:{exc}",
                    }
            recovered.append(
                {
                    "intent_id": str(item.get("intent_id") or ""),
                    **resolution,
                    "local_resolution": local_resolution,
                }
            )
        return {**self.execution_intent_recovery_status(), "recovered": recovered}

    def _send_market_order(self, side: int, volume: float, sl: float, tp: float,
                           comment: str, *, decision_id: str = "",
                           trade_id: str = "",
                           risk_verdict: dict | None = None) -> CTraderOrderResult:
        with self._broker_mutation_lock:
            return self._send_market_order_serial(
                side, volume, sl, tp, comment,
                decision_id=decision_id, trade_id=trade_id,
                risk_verdict=risk_verdict,
            )

    def _send_market_order_serial(self, side: int, volume: float, sl: float, tp: float,
                                  comment: str, *, decision_id: str = "",
                                  trade_id: str = "",
                                  risk_verdict: dict | None = None) -> CTraderOrderResult:
        if not self._connected or not self._account_authed:
            return CTraderOrderResult(
                success=False, outcome="rejected", error_code="not_connected",
                comment="Not connected/authed",
            )
        if self._symbol_id is None:
            return CTraderOrderResult(
                success=False, outcome="rejected", error_code="symbol_unresolved",
                comment="Symbol ID not resolved",
            )
        if not self.send_orders:
            logger.warning(
                "[DRY-RUN] cTrader market %s vol=%s sl=%s tp=%s (send_orders=False)",
                side, volume, sl, tp,
            )
            return CTraderOrderResult(
                success=True, outcome="simulated", comment="DRY-RUN (send_orders=False)",
                volume=volume, price=sl or tp,
            )

        try:
            from backend.services.live_safety_state import no_new_risk_latched

            new_risk_latched = no_new_risk_latched(fail_closed=True)
        except Exception as exc:
            logger.error("new-risk latch check failed closed before market RPC: %s", exc)
            return CTraderOrderResult(
                success=False,
                outcome="rejected",
                error_code="new_risk_latch_unavailable",
                comment=f"new-risk latch check failed: {type(exc).__name__}: {exc}",
            )
        if new_risk_latched:
            return CTraderOrderResult(
                success=False,
                outcome="rejected",
                error_code="no_new_risk_latched",
                comment="market open blocked by durable no_new_risk latch",
            )

        req = TradeMsg.ProtoOANewOrderReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.orderType = ORDER_TYPE["MARKET"]
        req.tradeSide = side
        meta = getattr(self, "_symbol_meta", None) or {}
        min_volume = int(round(float(meta.get("api_min_volume") or 0)))
        if min_volume <= 0 and self._symbol_id is not None:
            try:
                self._resolve_symbol_id()
                meta = getattr(self, "_symbol_meta", None) or {}
                min_volume = int(round(float(meta.get("api_min_volume") or 0)))
            except Exception:
                pass
        if min_volume <= 0:
            min_volume = max(1, int(round(volume)))
        req.volume = int(round(max(volume, min_volume)))

        client_order_id = str(uuid.uuid4())
        client_msg_id = str(uuid.uuid4())
        intent_id = str(uuid.uuid4())
        comment_token = f"qid:{intent_id.replace('-', '')[:16]}"
        base_comment = str(comment or "quant-live").strip()
        req.comment = f"{base_comment[:72]} {comment_token}".strip()
        req.clientOrderId = client_order_id
        logger.info(
            "market_order: account=%s symbolId=%s side=%s volume=%s "
            "clientOrderId=%s intent=%s",
            self.account_id, self._symbol_id, side, req.volume,
            client_order_id, intent_id,
        )

        pre_positions: dict[str, dict[str, Any]] = {}
        pre_deals: dict[str, dict[str, Any]] = {}
        pre_deals_available = False
        store = None
        if hasattr(risk_verdict, "to_dict"):
            try:
                risk_payload = dict(risk_verdict.to_dict())
            except Exception:
                risk_payload = dict(getattr(risk_verdict, "__dict__", {}) or {})
        elif isinstance(risk_verdict, dict):
            risk_payload = dict(risk_verdict)
        else:
            risk_payload = dict(getattr(risk_verdict, "__dict__", {}) or {})
        if self._execution_outcome_v2_enabled:
            try:
                store = self._intent_store()
                unresolved = int(
                    store.unresolved_count(
                        account_id=str(self.account_id), symbol=str(self.symbol)
                    )
                )
                if unresolved > 0:
                    self._latch_unknown_broker_outcome(
                        action="market_open",
                        position_id=0,
                        intent_id=intent_id,
                        evidence={
                            "reason": "unresolved_execution_intent",
                            "unresolved_count": unresolved,
                        },
                    )
                    return CTraderOrderResult(
                        success=False, outcome="rejected",
                        error_code="unresolved_execution_intent",
                        comment=f"blocked: {unresolved} unresolved broker execution intent(s)",
                        execution_intent_status="persisted",
                        client_order_id=client_order_id, client_msg_id=client_msg_id,
                    )
                pre_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
                if not pre_result.fresh:
                    return CTraderOrderResult(
                        success=False, outcome="rejected", error_code="pre_reconcile_failed",
                        comment=pre_result.error_message or pre_result.error_code,
                        client_order_id=client_order_id, client_msg_id=client_msg_id,
                    )
                pre_positions = self._position_snapshot_payload(pre_result.positions)
                pre_deal_rows = self.get_deals(from_ts=int(time.time() - 86400), max_rows=1000)
                pre_deals_available = bool(self._last_deals_fetch_ok)
                pre_deals = self._deal_snapshot_payload(pre_deal_rows) if pre_deals_available else {}
                prepared = store.prepare(
                    intent_id=intent_id, idempotency_key=intent_id,
                    broker="ctrader", account_id=str(self.account_id), symbol=str(self.symbol),
                    action="market_open",
                    side="buy" if side == TRADE_SIDE["BUY"] else "sell",
                    requested_volume=float(req.volume),
                    target_stop_loss=float(sl or 0.0),
                    target_take_profit=float(tp or 0.0),
                    decision_id=str(decision_id or ""),
                    trade_id=str(trade_id or ""),
                    request={
                        "schema": "broker_execution_request.v2",
                        "symbol_id": int(self._symbol_id or 0),
                        "client_order_id": client_order_id,
                        "client_msg_id": client_msg_id,
                        "comment_token": comment_token,
                        "comment": req.comment,
                        "positions_before": pre_positions,
                        "deals_before": pre_deals,
                        "deals_before_available": pre_deals_available,
                        "pre_reconcile_id": pre_result.reconcile_id,
                        "decision_id": str(decision_id or ""),
                        "trade_id": str(trade_id or ""),
                        "risk_verdict": risk_payload,
                    },
                )
                intent_id = prepared.intent_id
                store.mark_submitting(
                    intent_id,
                    request={**dict(prepared.request or {}), "submitting_at": time.time()},
                )
            except Exception as exc:
                logger.error("broker execution intent gate failed before RPC: %s", exc)
                return CTraderOrderResult(
                    success=False, outcome="rejected",
                    error_code="execution_intent_persist_failed", comment=str(exc),
                    intent_id=intent_id,
                    execution_intent_status="persist_failed",
                    client_order_id=client_order_id, client_msg_id=client_msg_id,
                )

        resp = None
        send_error: Exception | None = None
        try:
            resp = self._send(req, timeout=15.0, client_msg_id=client_msg_id)
        except Exception as exc:
            send_error = exc
            logger.warning("market_order RPC result uncertain: %s", exc)

        response = self._response_evidence(resp)
        error_code = str(response.get("error_code") or "")
        rejected = self._response_is_explicit_rejection(response)
        if rejected:
            broker_code = error_code or "broker_rejected"
            if store is not None:
                try:
                    store.complete(
                        intent_id, outcome="rejected", broker_response=response,
                        trade_id=str(
                            trade_id
                            or response.get("trade_id")
                            or response.get("position_id")
                            or ""
                        ),
                        error={"error_code": broker_code, "description": response.get("description")},
                    )
                except Exception as exc:
                    logger.error("failed to finalize rejected execution intent: %s", exc)
                    self._latch_unknown_broker_outcome(
                        action="market_open",
                        position_id=int(response.get("position_id") or 0),
                        intent_id=intent_id,
                        evidence={
                            **response,
                            "reason": "intent_finalize_failed_after_rejection",
                            "finalize_error": f"{type(exc).__name__}: {exc}",
                        },
                    )
                    return CTraderOrderResult(
                        success=False, outcome="unknown", error_code="INTENT_FINALIZE_FAILED",
                        comment=str(exc), intent_id=intent_id,
                        execution_intent_status="persisted",
                        client_order_id=client_order_id, client_msg_id=client_msg_id,
                    )
            return CTraderOrderResult(
                success=False, outcome="rejected", error_code=broker_code,
                comment=f"broker rejected: {broker_code} — {response.get('description') or ''}",
                intent_id=intent_id if store is not None else "",
                execution_intent_status=(
                    "persisted" if store is not None else "compat_missing_intent"
                ),
                client_order_id=client_order_id, client_msg_id=client_msg_id,
            )

        if not self._execution_outcome_v2_enabled:
            response_pid = int(response.get("position_id") or 0)
            if (
                response.get("response_type") == "ProtoOAExecutionEvent"
                and response_pid > 0
                and send_error is None
            ):
                return CTraderOrderResult(
                    success=True, outcome="confirmed",
                    order_id=int(response.get("order_id") or 0), position_id=response_pid,
                    comment=f"filled: orderId={int(response.get('order_id') or 0)} posId={response_pid}",
                    execution_intent_status="compat_missing_intent",
                    client_order_id=client_order_id, client_msg_id=client_msg_id,
                )
            self._latch_unknown_broker_outcome(
                action="market_open",
                position_id=int(response.get("position_id") or 0),
                intent_id=intent_id,
                evidence={
                    **response,
                    "reason": "compat_market_open_outcome_unknown",
                    "rpc_error": str(send_error or ""),
                },
            )
            return CTraderOrderResult(
                success=False, outcome="unknown",
                order_id=int(response.get("order_id") or 0),
                error_code="SEND_OUTCOME_UNKNOWN",
                comment=str(send_error or f"unexpected response: {response.get('response_type') or 'none'}"),
                execution_intent_status="compat_missing_intent",
                client_order_id=client_order_id, client_msg_id=client_msg_id,
            )

        post_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        post_positions = (
            self._position_snapshot_payload(post_result.positions) if post_result.fresh else {}
        )
        post_deal_rows = self.get_deals(from_ts=int(time.time() - 86400), max_rows=1000)
        post_deals_available = bool(self._last_deals_fetch_ok)
        post_deals = self._deal_snapshot_payload(post_deal_rows) if post_deals_available else {}
        post_orders: dict[str, dict[str, Any]] = {}
        post_orders_available = False
        if int(response.get("position_id") or 0) <= 0:
            post_orders, post_orders_available = self._order_history_for_recovery(
                from_ts=time.time() - 300.0,
            )
        resolution = self._resolve_open_differential(
            side=side, symbol_id=int(self._symbol_id or 0),
            pre_positions=pre_positions, pre_deals=pre_deals,
            post_positions=post_positions, post_deals=post_deals,
            response=response,
            deals_differential_available=pre_deals_available and post_deals_available,
            post_orders=post_orders,
            orders_available=post_orders_available,
            client_order_id=client_order_id,
            comment_token=comment_token,
        )
        if not post_result.fresh and int(response.get("position_id") or 0) <= 0:
            resolution = {
                **resolution, "outcome": "unknown", "position_id": 0,
                "reason": "post_reconcile_failed",
                "post_reconcile_error": post_result.error_code,
            }
        outcome = str(resolution.get("outcome") or "unknown")
        try:
            assert store is not None
            store.complete(
                intent_id, outcome=outcome,
                position_id=int(resolution.get("position_id") or 0),
                trade_id=str(
                    trade_id
                    or resolution.get("trade_id")
                    or response.get("trade_id")
                    or resolution.get("position_id")
                    or response.get("position_id")
                    or ""
                ),
                broker_order_id=int(resolution.get("order_id") or 0),
                broker_response={
                    **response, "resolution": resolution,
                    "post_reconcile_id": post_result.reconcile_id,
                    "post_reconcile_status": post_result.status,
                },
                error={} if outcome == "confirmed" else {
                    "reason": resolution.get("reason"), "rpc_error": str(send_error or "")
                },
            )
        except Exception as exc:
            logger.error("broker execution intent finalize failed; outcome held unknown: %s", exc)
            self._latch_unknown_broker_outcome(
                action="market_open",
                position_id=int(resolution.get("position_id") or 0),
                intent_id=intent_id,
                evidence={
                    **response,
                    "resolution": resolution,
                    "reason": "intent_finalize_failed",
                    "finalize_error": f"{type(exc).__name__}: {exc}",
                },
            )
            return CTraderOrderResult(
                success=False, outcome="unknown",
                position_id=int(resolution.get("position_id") or 0),
                order_id=int(resolution.get("order_id") or 0),
                error_code="INTENT_FINALIZE_FAILED", comment=str(exc),
                intent_id=intent_id,
                execution_intent_status="persisted",
                client_order_id=client_order_id, client_msg_id=client_msg_id,
            )
        if outcome == "unknown":
            self._latch_unknown_broker_outcome(
                action="market_open",
                position_id=int(resolution.get("position_id") or 0),
                intent_id=intent_id,
                evidence={**response, "resolution": resolution},
            )
        return CTraderOrderResult(
            success=outcome == "confirmed", outcome=outcome,
            position_id=int(resolution.get("position_id") or 0),
            order_id=int(resolution.get("order_id") or 0),
            error_code="" if outcome == "confirmed" else "SEND_OUTCOME_UNKNOWN",
            comment=str(resolution.get("reason") or outcome),
            intent_id=intent_id,
            execution_intent_status="persisted",
            client_order_id=client_order_id, client_msg_id=client_msg_id,
        )

    def _normalize_close_volume(self, volume: float) -> tuple[int, str, str]:
        raw_volume = int(round(float(volume or 0.0)))
        if raw_volume <= 0:
            return (
                0,
                "invalid_close_volume",
                f"close volume must be > 0, got {volume}",
            )

        meta = getattr(self, "_symbol_meta", None) or {}
        if not meta:
            try:
                self._resolve_symbol_id()
                meta = getattr(self, "_symbol_meta", None) or {}
            except Exception as exc:
                logger.warning("close volume metadata resolve failed: %s", exc)
        if not meta and str(self.symbol or "").upper().startswith("XAUUSD"):
            meta = {"api_min_volume": 100, "api_step_volume": 100}

        min_volume = max(1, int(round(float(meta.get("api_min_volume") or 1))))
        step_volume = max(1, int(round(float(meta.get("api_step_volume") or 1))))
        if raw_volume < min_volume:
            return (
                0,
                "invalid_close_volume_step",
                f"close volume {raw_volume} is below minVolume={min_volume}",
            )
        if raw_volume % step_volume != 0:
            return (
                0,
                "invalid_close_volume_step",
                f"close volume {raw_volume} is not a multiple of stepVolume={step_volume}",
            )
        return raw_volume, "", ""

    def close_position(self, position_id: int,
                       volume: float = 0.0) -> OrderResult:
        with self._broker_mutation_lock:
            return self._close_position_serial(position_id, volume=volume)

    def _close_position_serial(self, position_id: int,
                               volume: float = 0.0) -> OrderResult:
        """平仓 (走 ProtoOAClosePositionReq, DRY-RUN 时仅打印).

        Args:
            position_id: 要平的仓位 ID; None 时 broker 默认平当前账户所有仓位
            volume: 部分平仓量 (API volume); None 时自动查当前仓位 volume 全平

        ⚠️ audit 2026-06-11: ProtoOAClosePositionReq 4 个字段全部 required
        (payloadType/ctidTraderAccountId/positionId/volume). volume 不能省略.
        """
        if not self.is_connected:
            return OrderResult(success=False, outcome="rejected", comment="Not connected")
        if not self._account_authed:
            return OrderResult(success=False, outcome="rejected", comment="Account not authed")
        if position_id <= 0:
            return OrderResult(success=False, outcome="rejected", comment="position_id required")
        # DRY-RUN 安全闸
        if not self.send_orders:
            logger.warning(f"[DRY-RUN] close_position pos={position_id} vol={volume} (send_orders=False)")
            return OrderResult(
                success=True, position_id=position_id,
                outcome="simulated",
                comment="DRY-RUN close (send_orders=False)",
            )
        close_volume = 0
        if volume > 0.0:
            close_volume, volume_error_code, volume_error = self._normalize_close_volume(volume)
            if volume_error_code:
                logger.warning(
                    "close_position rejected locally pos=%s volume=%s: %s",
                    position_id, volume, volume_error,
                )
                return OrderResult(
                    success=False,
                    outcome="rejected",
                    position_id=position_id,
                    volume=float(volume or 0.0),
                    error_code=volume_error_code,
                    comment=volume_error,
                )

        pre_result = None
        pre_position = None
        # Always attempt a fresh pre-reconcile while the broker mutation lock
        # is held.  A fresh absence proves a concurrent/previous close already
        # removed the risk and prevents a duplicate RPC.  Reconcile failure
        # does not suppress a caller-supplied risk-reducing volume.
        pre_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        pre_position = next(
            (item for item in pre_result.positions if int(item.position_id) == int(position_id)),
            None,
        ) if pre_result.fresh else None
        if pre_result is not None and pre_result.fresh and pre_position is None:
            self._local_unknown_risk_mutations.pop(
                ("close_position", int(position_id)),
                None,
            )
            self._local_unknown_risk_mutations.pop(
                ("reduce_position", int(position_id)),
                None,
            )
            try:
                from backend.services.live_safety_state import (
                    resolve_broker_outcome_mutation,
                )

                resolve_broker_outcome_mutation(
                    action="close_position",
                    position_id=int(position_id),
                    outcome="confirmed",
                    evidence={
                        "source": "fresh_pre_reconcile",
                        "reconcile_id": str(pre_result.reconcile_id or ""),
                        "position_present": False,
                    },
                )
            except Exception as exc:
                logger.error("local close unknown resolution append failed: %s", exc)
            return OrderResult(
                success=True,
                outcome="confirmed",
                position_id=position_id,
                volume=float(volume or 0.0),
                comment=f"Position {position_id} already absent in fresh broker reconcile",
            )
        if volume <= 0.0:
            if pre_result is None or not pre_result.fresh:
                return OrderResult(
                    success=False,
                    outcome="rejected",
                    position_id=position_id,
                    error_code="pre_reconcile_failed",
                    comment=(
                        pre_result.error_message or pre_result.error_code
                        if pre_result is not None
                        else "fresh position reconcile required"
                    ),
                )
            assert pre_position is not None
            volume = float(pre_position.volume or 0.0)
            logger.info("auto-resolved fresh broker volume=%s for full close", volume)
            close_volume, volume_error_code, volume_error = self._normalize_close_volume(volume)
            if volume_error_code:
                return OrderResult(
                    success=False,
                    outcome="rejected",
                    position_id=position_id,
                    volume=float(volume or 0.0),
                    error_code=volume_error_code,
                    comment=volume_error,
                )

        store, intent_id, client_msg_id, blocked_intent = self._prepare_risk_reduction_intent(
            action="close_position",
            position_id=int(position_id),
            requested_volume=float(close_volume),
            request={
                "pre_reconcile_id": getattr(pre_result, "reconcile_id", ""),
                "pre_reconcile_status": getattr(pre_result, "status", "not_requested"),
                "position_volume_before": (
                    float(pre_position.volume or 0.0) if pre_position is not None else None
                ),
            },
        )
        if blocked_intent is not None:
            self._latch_unknown_broker_outcome(
                action="close_position",
                position_id=int(position_id),
                intent_id=intent_id,
                evidence={
                    "reason": "unresolved_risk_reduction_intent",
                    "existing_status": str(getattr(blocked_intent, "status", "unknown") or "unknown"),
                },
            )
            return OrderResult(
                success=False,
                outcome="unknown",
                position_id=int(position_id),
                volume=float(close_volume),
                error_code="DUPLICATE_MUTATION_BLOCKED",
                comment="unresolved close intent must be recovered before resubmission",
                intent_id=intent_id,
                execution_intent_status=(
                    "persisted" if store is not None else "compat_missing_intent"
                ),
            )
        req = TradeMsg.ProtoOAClosePositionReq()
        req.ctidTraderAccountId = self.account_id
        req.positionId = int(position_id)
        req.volume = close_volume
        resp = None
        send_error: Exception | None = None
        try:
            resp = self._send(req, timeout=10.0, client_msg_id=client_msg_id)
        except Exception as exc:
            send_error = exc
            logger.error("close_position RPC outcome uncertain pos=%s: %s", position_id, exc)
        response = self._response_evidence(resp)
        response.update({"client_msg_id": client_msg_id, "requested_volume": close_volume})
        rejected = self._response_is_explicit_rejection(response)
        if rejected:
            err_code = str(response.get("error_code") or "broker_rejected")
            self._finalize_risk_reduction_intent(
                store,
                intent_id,
                outcome="rejected",
                position_id=int(position_id),
                broker_response=response,
                error={"error_code": err_code, "description": response.get("description")},
            )
            return OrderResult(
                success=False,
                outcome="rejected",
                position_id=position_id,
                volume=float(close_volume),
                error_code=err_code,
                comment=f"close rejected: {err_code} — {response.get('description') or ''}",
                intent_id=intent_id if store is not None else "",
                execution_intent_status=(
                    "persisted" if store is not None else "compat_missing_intent"
                ),
                client_msg_id=client_msg_id,
            )

        if not self._execution_outcome_v2_enabled:
            confirmed = (
                response.get("response_type") == "ProtoOAExecutionEvent"
                and send_error is None
            )
            outcome = "confirmed" if confirmed else "unknown"
            evidence = {
                **response,
                "rpc_error": str(send_error or ""),
                "requested_volume": float(close_volume),
                "position_volume_before": (
                    float(pre_position.volume or 0.0) if pre_position is not None else None
                ),
            }
            if not confirmed:
                self._latch_unknown_broker_outcome(
                    action="close_position",
                    position_id=int(position_id),
                    intent_id=intent_id,
                    evidence=evidence,
                )
            return OrderResult(
                success=confirmed,
                outcome=outcome,
                position_id=position_id,
                volume=float(close_volume),
                order_id=int(response.get("order_id") or 0),
                error_code="" if confirmed else "CLOSE_OUTCOME_UNKNOWN",
                comment=(
                    "close accepted by ExecutionEvent (compat mode)"
                    if confirmed
                    else str(send_error or f"unexpected response: {response.get('response_type') or 'none'}")
                ),
                execution_intent_status="compat_missing_intent",
                client_msg_id=client_msg_id,
            )

        post_result = self.reconcile_positions(force=True, allow_cache_fallback=False)
        current = next(
            (item for item in post_result.positions if int(item.position_id) == int(position_id)),
            None,
        ) if post_result.fresh else None
        expected_remaining = None
        if pre_position is not None:
            expected_remaining = max(0.0, float(pre_position.volume or 0.0) - float(close_volume))
        confirmed_absent = bool(post_result.fresh and current is None)
        confirmed_reduced = bool(
            post_result.fresh
            and current is not None
            and expected_remaining is not None
            and float(current.volume or 0.0) <= expected_remaining + 1e-9
        )
        confirmed = confirmed_absent or confirmed_reduced
        resolution = {
            "post_reconcile_id": post_result.reconcile_id,
            "post_reconcile_status": post_result.status,
            "position_present": current is not None,
            "position_volume_before": (
                float(pre_position.volume or 0.0) if pre_position is not None else None
            ),
            "position_volume_after": float(current.volume or 0.0) if current is not None else 0.0,
            "expected_max_remaining": expected_remaining,
            "rpc_error": str(send_error or ""),
        }
        outcome = "confirmed" if confirmed else "unknown"
        self._finalize_risk_reduction_intent(
            store,
            intent_id,
            outcome=outcome,
            position_id=int(position_id),
            broker_response={**response, "resolution": resolution},
            error={} if confirmed else {"reason": "close_not_freshly_verified"},
        )
        if not confirmed:
            self._latch_unknown_broker_outcome(
                action="close_position",
                position_id=int(position_id),
                intent_id=intent_id,
                evidence={**response, "resolution": resolution},
            )
        return OrderResult(
            success=confirmed,
            outcome=outcome,
            position_id=position_id,
            volume=float(close_volume),
            order_id=int(response.get("order_id") or 0),
            error_code="" if confirmed else "CLOSE_OUTCOME_UNKNOWN",
            comment="close verified by fresh broker reconcile" if confirmed else "close not freshly verified",
            intent_id=intent_id if store is not None else "",
            execution_intent_status=(
                "persisted" if store is not None else "compat_missing_intent"
            ),
            client_msg_id=client_msg_id,
        )

    # ── 账户 ──

    def ping(self) -> bool:
        """轻量探针: 发一个 ProtoOATraderReq 检查连接是否还活着.
        成功返回 True; 超时/异常返回 False 并标记 _connected=False."""
        if not self._connected:
            return False
        try:
            req = TradeMsg.ProtoOATraderReq()
            req.ctidTraderAccountId = self.account_id
            self._send(req, timeout=5.0)
            return True
        except Exception:
            self._mark_disconnected()
            return False

    def account_info(self) -> AccountInfo:
        cached = self._copy_account_cache()
        if cached.account_id or cached.balance or cached.equity:
            return cached
        return self.refresh_account_info()

    def refresh_account_info(self) -> AccountInfo:
        """Legacy value-only wrapper around :meth:`reconcile_account`."""
        result = self.reconcile_account(force=True, allow_cache_fallback=True)
        return result.account or AccountInfo()

    def reconcile_account(
        self,
        *,
        force: bool = True,
        allow_cache_fallback: bool = False,
        confirmed_empty_positions: PositionReconcileResult | None = None,
    ) -> AccountReconcileResult:
        """
        Return an explicit broker account result.

        A zero-valued ``AccountInfo`` is never used to represent a failed
        reconciliation.  Callers can distinguish a fresh response from a
        cache/event projection through ``status``.
        """
        reconcile_id = str(uuid.uuid4())
        generated_at = time.time()

        def fallback(error_code: str, error_message: str) -> AccountReconcileResult:
            cached = self._copy_account_cache()
            with self._account_cache_lock:
                observed_at = float(self._account_cache_observed_at or 0.0)
                source = str(self._account_cache_source or "cache")
            if allow_cache_fallback and observed_at > 0:
                return AccountReconcileResult(
                    reconcile_id=reconcile_id,
                    status="event" if source == "event" else "cache",
                    account=cached,
                    observed_at=observed_at,
                    generated_at=time.time(),
                    error_code=error_code,
                    error_message=error_message,
                )
            return AccountReconcileResult(
                reconcile_id=reconcile_id,
                status="failed",
                account=None,
                observed_at=0.0,
                generated_at=time.time(),
                error_code=error_code,
                error_message=error_message,
            )

        if not self.is_connected:
            return fallback("not_connected", "cTrader is not connected/authenticated")
        if not force and self._should_backoff():
            return fallback("broker_backoff", "cTrader request backoff is active")
        try:
            req = TradeMsg.ProtoOATraderReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
            t = resp.trader
            # Equity is not fresh unless both the trader balance and the
            # broker position/PnL projection succeeded.  A fresh immutable
            # broker reconcile that confirms zero positions is authoritative
            # proof that unrealized PnL was zero at its observation time, so
            # avoid a redundant PnL RPC in that narrow case.  This reduces
            # cTrader timeout pressure without ever inferring zero from a
            # cache, failed reconcile, stale timestamp, or non-empty account.
            empty_positions_observed_at = 0.0
            if (
                isinstance(confirmed_empty_positions, PositionReconcileResult)
                and confirmed_empty_positions.fresh
                and not confirmed_empty_positions.positions
            ):
                candidate_observed_at = float(
                    confirmed_empty_positions.observed_at or 0.0
                )
                candidate_age = generated_at - candidate_observed_at
                if -1.0 <= candidate_age <= 15.0:
                    empty_positions_observed_at = candidate_observed_at
            unrealized = (
                0.0
                if empty_positions_observed_at > 0.0
                else self._unrealized_pnl()
            )
            account = self._account_from_trader(t, unrealized=unrealized)
            balance = account.balance
            equity = account.equity
            logger.debug(
                f"Trader info: login={account.account_id} balance={balance:.2f} "
                f"depositAssetId={getattr(t, 'depositAssetId', 0)} currency={account.currency} "
                f"leverage={account.leverage:.2f} (leverageInCents={float(getattr(t, 'leverageInCents', 0) or 0):.0f}, maxLeverage={getattr(t, 'maxLeverage', 0)})"
            )
            self._record_success()
            account = self._set_account_cache(account, emit=True, reason="account_info")
            observed_at = (
                empty_positions_observed_at
                if empty_positions_observed_at > 0.0
                else time.time()
            )
            with self._account_cache_lock:
                self._account_cache_observed_at = observed_at
                self._account_cache_source = "cache"
            return AccountReconcileResult(
                reconcile_id=reconcile_id,
                status="fresh",
                account=self._copy_account_cache(),
                observed_at=observed_at,
                generated_at=time.time(),
            )
        except Exception as e:
            if not self._is_soft_timeout_error(e):
                self._mark_disconnected()
            self._record_failure()
            if self._should_log_error(f"account_info failed: {e}"):
                logger.error(f"account_info failed: {e}")
            return fallback("account_reconcile_failed", str(e))

    def _unrealized_pnl(self) -> float:
        """查所有持仓的浮动盈亏总和 (美元, 非 centi-unit).
        0.0 = 无持仓或 broker 不支持.

        ProtoOAGetPositionUnrealizedPnLRes 返 repeated {positionId,
        grossUnrealizedPnL, netUnrealizedPnL}, 单位 centi-unit.
        """
        req = TradeMsg.ProtoOAGetPositionUnrealizedPnLReq()
        req.ctidTraderAccountId = self.account_id
        resp = self._send(req, timeout=8.0)
        money_digits = getattr(resp, "moneyDigits", 2) or 2
        divisor = 10 ** money_digits
        total = 0.0
        for entry in resp.positionUnrealizedPnL:
            # netUnrealizedPnL: 扣除佣金后的浮动盈亏
            pnl = getattr(entry, "netUnrealizedPnL", 0)
            if pnl:
                total += pnl / divisor
        return total

    def get_positions(self, symbol: str = "") -> list[PositionInfo]:
        cached_positions = self._positions_snapshot()
        if cached_positions:
            if symbol and self._symbol_id is not None:
                return [pos for pos in cached_positions if int(pos.symbol_id or 0) == int(self._symbol_id)]
            return cached_positions
        return self.refresh_positions(symbol)

    def refresh_positions(
        self,
        symbol: str = "",
        *,
        force: bool = False,
        allow_cache_fallback: bool = True,
    ) -> list[PositionInfo]:
        """Legacy value-only wrapper around :meth:`reconcile_positions`."""
        result = self.reconcile_positions(
            symbol,
            force=force,
            allow_cache_fallback=allow_cache_fallback,
        )
        return [self._copy_position(pos) for pos in result.positions]

    def reconcile_positions(
        self,
        symbol: str = "",
        *,
        force: bool = True,
        allow_cache_fallback: bool = False,
    ) -> PositionReconcileResult:
        """
        Return an explicit full position reconciliation result.

        A fresh empty tuple means the broker confirmed no positions.  A
        failed call is never encoded as an empty list, which is the key safety
        distinction missing from the legacy ``refresh_positions`` API.
        """
        reconcile_id = str(uuid.uuid4())

        def filtered_snapshot() -> list[PositionInfo]:
            cached_positions = self._positions_snapshot()
            if symbol and self._symbol_id is not None:
                return [pos for pos in cached_positions if int(pos.symbol_id or 0) == int(self._symbol_id)]
            return cached_positions

        def fallback(error_code: str, error_message: str) -> PositionReconcileResult:
            cached_positions = filtered_snapshot()
            with self._positions_cache_lock:
                observed_at = float(self._positions_cache_observed_at or 0.0)
                source = str(self._positions_cache_source or "cache")
            if allow_cache_fallback and observed_at > 0:
                return PositionReconcileResult(
                    reconcile_id=reconcile_id,
                    status="event" if source == "event" else "cache",
                    positions=tuple(self._copy_position(pos) for pos in cached_positions),
                    observed_at=observed_at,
                    generated_at=time.time(),
                    error_code=error_code,
                    error_message=error_message,
                )
            return PositionReconcileResult(
                reconcile_id=reconcile_id,
                status="failed",
                positions=(),
                observed_at=0.0,
                generated_at=time.time(),
                error_code=error_code,
                error_message=error_message,
            )

        if not self.is_connected:
            return fallback("not_connected", "cTrader is not connected/authenticated")
        if not force and self._should_backoff():
            return fallback("broker_backoff", "cTrader request backoff is active")
        try:
            req = TradeMsg.ProtoOAReconcileReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
            snapshot_observed_at = time.time()
            result = []
            for p in resp.position:
                td = p.tradeData  # symbolId 在 tradeData 里, 不在顶层
                # ⚠️ audit 2026-06-11: ProtoOAPosition 顶层没有 symbolId;
                #    symbolId 在嵌套 tradeData (ProtoOATradeData) 里.
                if symbol and td.symbolId != self._symbol_id:
                    continue
                # ⚠️ audit 2026-06-11: ProtoOAPosition.price 是 float (type=1=double),
                #    already real price, 不能除 moneyDigits. spot event 才需要除.
                #    SL/TP 同理是 float.
                direction = 1 if td.tradeSide == TRADE_SIDE["BUY"] else -1
                result.append(PositionInfo(
                    position_id=p.positionId,
                    symbol_id=td.symbolId,
                    symbol=self.symbol,
                    direction=direction,
                    volume=td.volume,
                    entry_price=p.price,
                    # ProtoOAPosition.price is the immutable entry price, not
                    # a current mark.  A fresh spot event may fill this below.
                    current_price=0.0,
                    sl=p.stopLoss or 0,
                    tp=p.takeProfit or 0,
                    commission=p.commission / 100.0,
                    swap=p.swap / 100.0,
                    open_timestamp=(td.openTimestamp / 1000.0) if td.openTimestamp else 0,
                    current_price_state="unknown",
                    current_price_source="ctrader_reconcile",
                    current_price_reason_code="fresh_spot_unavailable",
                    pnl_state="unknown",
                    pnl_source="ctrader_unrealized_pnl",
                    pnl_reason_code="pnl_projection_pending",
                ))

            position_ids = tuple(sorted(int(pos.position_id) for pos in result))
            quote = self.get_spot_quote()
            quote_ts = float(quote.get("ts") or 0.0)
            quote_mid = float(quote.get("mid") or 0.0)
            quote_age = (
                max(0.0, snapshot_observed_at - quote_ts)
                if quote_ts > 0
                else None
            )
            price_known_ids: list[int] = []
            price_unknown_ids: list[int] = []
            for pos in result:
                quote_applies = (
                    self._symbol_id is not None
                    and int(pos.symbol_id or 0) == int(self._symbol_id)
                )
                if (
                    quote_applies
                    and quote_mid > 0
                    and quote_age is not None
                    and quote_age <= _POSITION_SPOT_COMPONENT_MAX_AGE_SECONDS
                ):
                    pos.current_price = quote_mid
                    pos.current_price_state = "known"
                    pos.current_price_source = "ctrader_spot"
                    pos.current_price_observed_at = quote_ts
                    pos.current_price_reason_code = ""
                    price_known_ids.append(int(pos.position_id))
                else:
                    pos.current_price = 0.0
                    pos.current_price_state = "stale" if quote_applies and quote_ts > 0 else "unknown"
                    pos.current_price_source = "ctrader_spot"
                    pos.current_price_observed_at = quote_ts
                    pos.current_price_reason_code = (
                        "spot_quote_stale"
                        if quote_applies and quote_ts > 0
                        else "spot_quote_wrong_symbol"
                        if not quote_applies
                        else "spot_quote_unavailable"
                    )
                    price_unknown_ids.append(int(pos.position_id))

            pnl_known_ids: list[int] = []
            pnl_unknown_ids: list[int] = []
            pnl_error = ""
            pnl_observed_at = 0.0
            if result:
                try:
                    pnl_map = self._query_unrealized_pnl()
                    pnl_observed_at = time.time()
                    for pos in result:
                        pid = int(pos.position_id)
                        if pid in pnl_map:
                            pos.pnl = float(pnl_map[pid])
                            pos.pnl_state = "known"
                            pos.pnl_source = "ctrader_unrealized_pnl"
                            pos.pnl_observed_at = pnl_observed_at
                            pos.pnl_reason_code = ""
                            pnl_known_ids.append(pid)
                        else:
                            pos.pnl = 0.0
                            pos.pnl_state = "unknown"
                            pos.pnl_source = "ctrader_unrealized_pnl"
                            pos.pnl_observed_at = pnl_observed_at
                            pos.pnl_reason_code = "position_pnl_entry_missing"
                            pnl_unknown_ids.append(pid)
                except Exception as exc:
                    pnl_error = f"{type(exc).__name__}: {exc}"
                    for pos in result:
                        pos.pnl = 0.0
                        pos.pnl_state = "error"
                        pos.pnl_source = "ctrader_unrealized_pnl"
                        pos.pnl_observed_at = 0.0
                        pos.pnl_reason_code = "unrealized_pnl_rpc_failed"
                        pnl_unknown_ids.append(int(pos.position_id))
                    if self._should_log_error(f"position PnL component failed: {exc}"):
                        logger.error("position PnL component failed: %s", exc)
            else:
                # Empty is authoritative without a second RPC: there is no
                # per-position price or unrealized-PnL component to resolve.
                pnl_observed_at = snapshot_observed_at

            # Identity/volume/SL/TP were observed when ProtoOAReconcileRes
            # arrived, not after the independent PnL request completed.
            self._last_reconcile_at = snapshot_observed_at
            self._set_positions_cache(result, emit=True, reason="reconcile")
            observed_at = float(snapshot_observed_at)
            with self._positions_cache_lock:
                self._positions_cache_observed_at = observed_at
                self._positions_cache_source = "cache"
            if not pnl_unknown_ids:
                self._recompute_account_equity_from_cache(emit=True, reason="reconcile")
            self._record_success()
            if symbol and self._symbol_id is not None:
                result = [pos for pos in result if int(pos.symbol_id or 0) == int(self._symbol_id)]
            return PositionReconcileResult(
                reconcile_id=reconcile_id,
                status="fresh",
                positions=tuple(self._copy_position(pos) for pos in result),
                observed_at=observed_at,
                generated_at=time.time(),
                identity_component=ReconcileComponentFact(
                    state="known",
                    source="ctrader_reconcile",
                    observed_at=snapshot_observed_at,
                    known_position_ids=position_ids,
                ),
                protection_component=ReconcileComponentFact(
                    state="known",
                    source="ctrader_reconcile",
                    observed_at=snapshot_observed_at,
                    known_position_ids=position_ids,
                ),
                price_component=ReconcileComponentFact(
                    state=("known" if not price_unknown_ids else "unknown"),
                    source=("ctrader_spot" if result else "ctrader_reconcile_empty"),
                    observed_at=(quote_ts if result and price_known_ids else snapshot_observed_at if not result else 0.0),
                    reason_code=("" if not price_unknown_ids else "position_price_component_incomplete"),
                    known_position_ids=tuple(sorted(price_known_ids)),
                    unknown_position_ids=tuple(sorted(price_unknown_ids)),
                ),
                pnl_component=ReconcileComponentFact(
                    state=(
                        "known"
                        if not pnl_unknown_ids
                        else "error"
                        if pnl_error
                        else "unknown"
                    ),
                    source=("ctrader_unrealized_pnl" if result else "ctrader_reconcile_empty"),
                    observed_at=pnl_observed_at,
                    reason_code=(
                        "unrealized_pnl_rpc_failed"
                        if pnl_error
                        else "position_pnl_component_incomplete"
                        if pnl_unknown_ids
                        else ""
                    ),
                    known_position_ids=tuple(sorted(pnl_known_ids)),
                    unknown_position_ids=tuple(sorted(pnl_unknown_ids)),
                ),
            )
        except Exception as e:
            if not self._is_soft_timeout_error(e):
                self._mark_disconnected()
            self._record_failure()
            if self._should_log_error(f"get_positions failed: {e}"):
                logger.error(f"get_positions failed: {e}")
            return fallback("position_reconcile_failed", str(e))

    def _query_unrealized_pnl(self) -> dict[int, float]:
        """Strict broker PnL query used by explicit reconciliation.

        Unlike the compatibility wrapper, failures propagate so the caller
        can publish an ``error`` component instead of a fabricated empty map.
        """
        if not self.is_connected:
            raise ConnectionError("cTrader is not connected/authenticated")
        req = TradeMsg.ProtoOAGetPositionUnrealizedPnLReq()
        req.ctidTraderAccountId = self.account_id
        resp = self._send(req, timeout=10.0)
        if not hasattr(resp, "positionUnrealizedPnL"):
            raise TypeError("unrealized PnL response missing positionUnrealizedPnL")
        result: dict[int, float] = {}
        md = getattr(resp, "moneyDigits", 2) or 2
        divisor = 10.0 ** md
        for upnl in resp.positionUnrealizedPnL:
            position_id = int(getattr(upnl, "positionId", 0) or 0)
            if position_id <= 0:
                continue
            net_pnl = float(getattr(upnl, "netUnrealizedPnL", 0.0) or 0.0) / divisor
            if not math.isfinite(net_pnl):
                raise ValueError(f"non-finite unrealized PnL for position {position_id}")
            result[position_id] = net_pnl
        self._record_success()
        return result

    def get_unrealized_pnl(self) -> dict[int, float]:
        """Query unrealized PnL for all open positions via ProtoOAGetPositionUnrealizedPnLReq.

        Returns dict mapping position_id → netUnrealizedPnL (in real USD, after dividing by moneyDigits).
        Empty dict on error or no positions.
        """
        try:
            return self._query_unrealized_pnl()
        except Exception as e:
            self._record_failure()
            if self._should_log_error(f"get_unrealized_pnl failed: {e}"):
                logger.error(f"get_unrealized_pnl failed: {e}")
            return {}

    # ── 历史成交 (ProtoOADealListReq) ─────────────────────────

    def get_deals(self, *, from_ts: int = 0, to_ts: int = 0,
                  max_rows: int = 100) -> list[dict]:
        """查历史成交记录 (已平仓交易).

        cTrader ProtoOADealListReq → ProtoOADealListRes.

        Args:
            from_ts: 起始时间戳 (秒, 0=不限).
            to_ts:   结束时间戳 (秒, 0=不限).
            max_rows: 最大返回条数 (默认 100).

        Returns:
            [{dealId, positionId, symbolId, volume, filledVolume,
              executionPrice, tradeSide, dealStatus,
              executionTimestamp, commission, ...}]
        """
        if not self.is_connected:
            return []
        try:
            req = TradeMsg.ProtoOADealListReq()
            req.ctidTraderAccountId = self.account_id
            if from_ts > 0:
                req.fromTimestamp = int(from_ts * 1000)  # ms
            if to_ts > 0:
                req.toTimestamp = int(to_ts * 1000)
            if max_rows > 0:
                req.maxRows = max_rows
            resp = self._send(req, timeout=15.0)
            results = []
            for d in resp.deal:
                money_digits = getattr(d, 'moneyDigits', None)
                # ── Close detail (平仓盈亏) ──
                cpd = d.closePositionDetail
                close_detail = {}
                # cpd.Descriptor 存在且至少有一个非默认字段 → 有真实平仓数据
                if cpd and (cpd.balance != 0 or cpd.grossProfit != 0):
                    close_detail = {
                        "entry_price": _parse_broker_raw_price(cpd.entryPrice),
                        "gross_profit": _parse_broker_money_amount(
                            cpd.grossProfit, getattr(cpd, 'moneyDigits', None)
                        ),
                        "swap": _parse_broker_money_amount(
                            cpd.swap, getattr(cpd, 'moneyDigits', None)
                        ),
                        "commission": _parse_broker_money_amount(
                            cpd.commission, getattr(cpd, 'moneyDigits', None)
                        ),
                        "balance": _parse_broker_money_amount(
                            cpd.balance, getattr(cpd, 'moneyDigits', None)
                        ),
                        "closed_volume": cpd.closedVolume,
                    }
                raw_execution_price, price_quality = (
                    _classify_broker_deal_price(
                        d.executionPrice,
                        entry_price=close_detail.get("entry_price"),
                    )
                )
                execution_price = (
                    raw_execution_price
                    if price_quality == "broker_reported"
                    else 0.0
                )
                results.append({
                    "deal_id": d.dealId,
                    "order_id": d.orderId,
                    "position_id": d.positionId,
                    "symbol_id": d.symbolId,
                    "volume": d.volume,
                    "filled_volume": d.filledVolume,
                    "execution_price": execution_price,
                    "raw_execution_price": raw_execution_price,
                    "price_contract": (
                        "ctrader.deal.execution_price.raw.v1"
                        if execution_price > 0.0
                        else "legacy_unknown"
                    ),
                    "price_quality": price_quality,
                    "trade_side": "buy" if d.tradeSide == TRADE_SIDE["BUY"] else "sell",
                    "deal_status": d.dealStatus,
                    "execution_timestamp": d.executionTimestamp / 1000.0,
                    "commission": _parse_broker_money_amount(d.commission, money_digits),
                    "close_detail": close_detail,
                })
            logger.info("[cTrader] get_deals: %d deals returned (from=%s to=%s max=%d)",
                        len(results),
                        time.strftime("%Y-%m-%d", time.gmtime(from_ts)) if from_ts else "∞",
                        time.strftime("%Y-%m-%d", time.gmtime(to_ts)) if to_ts else "∞",
                        max_rows)
            self._last_deals_fetch_ok = True
            return results
        except Exception as e:
            self._last_deals_fetch_ok = False
            logger.error(f"get_deals failed: {e}")
            return []

    # ── 历史 (trendbar) ──

    def fetch_bars(self, timeframe: str = "M15", n_bars: int = 5000) -> "pd.DataFrame | None":
        """
        拉最近 n_bars K 线.

        ⚠️ ProtoOATrendbar 字段是:
          utcTimestampInMinutes: int (unix minutes, NOT ms)
          deltaOpen/deltaClose/deltaHigh: int64 (相对 prev close 的偏移, pips)
          low: int64 (绝对 low)
          volume: int64
          period: enum
        解码: close_prev 未知时不能用 delta, 只用 low (绝对值), 其他估 0
        (要拿真实 OHLC 需订阅 spot, 见阶段 2)
        """
        if not self.is_connected:
            return None
        if self._symbol_id is None:
            logger.error("Symbol ID not resolved")
            return None
        period_map = _CTRADER_PERIOD_MAP
        # protobuf enum ≠ minutes; need actual minutes for fromTimestamp calc
        period_minutes = {
            "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5,
            "M10": 10, "M15": 15, "M30": 30, "H1": 60, "H4": 240,
            "H12": 720, "D1": 1440, "W1": 10080, "MN1": 43200,
        }
        period = period_map.get(timeframe)
        if period is None:
            logger.error(f"Unknown timeframe {timeframe}")
            return None

        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas not installed")
            return None

        # cTrader 用 range 协议: [fromTimestamp, toTimestamp] 内返所有 bar.
        # 不传 req.count — 同时设 count + range 会被 server 拒 (audit 2026-06-08).
        # n_bars 仅用于 fromTimestamp 计算 (保证不超最大范围).
        # cTrader range 上限 ~5000 根; 超出时 server 仍会返, 但速度慢.
        req = TradeMsg.ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.period = period
        # ⚠️ 2026-06-18: 官方文档要求毫秒时间戳 (Unix ms), 之前用了分钟导致 bar 始终为空
        mins = period_minutes.get(timeframe, 15)
        now_ms = int(time.time() * 1000)
        mins_ms = mins * 60 * 1000
        lookback_bars = min(n_bars, 5000)
        req.fromTimestamp = now_ms - lookback_bars * mins_ms
        req.toTimestamp = now_ms
        # count 可同时传, 官方文档示例同时使用 count + 时间范围
        # 按官方示例设 count = lookback_bars (服务端最多返这个数)
        try:
            req.count = lookback_bars
        except Exception:
            pass
        try:
            resp = self._send(req, timeout=20.0)
        except Exception as e:
            logger.error(f"fetch_bars failed: {e}")
            return None
        if not resp.trendbar:
            return None
        # 按官方文档: GetPriceFromRelative = round(value / 100000, symbol.digits)
        # digits 从 _symbol_meta 获取, 保底 2 (XAUUSD 常见值)
        digits = getattr(self, '_symbol_meta', {}).get('digits', 2)
        rows = []
        for bar in resp.trendbar:
            row = _trendbar_to_row(bar, digits)
            if row is not None:
                rows.append(row)
        if not rows:
            return None
        df = pd.DataFrame(rows)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time").sort_index()
        return df[["open", "high", "low", "close", "volume"]]


def _period_to_sec(period: int) -> int:
    """cTrader period (分钟) → 秒"""
    return period * 60


# ── Dry-run 入口 ──────────────────────────────────────────

def _dry_run():
    """无 broker 时: 验证 import / 配置 / 离线接口"""
    print("=" * 70)
    print("  cTrader Bridge dry-run (无 broker)")
    print("=" * 70)
    print(f"  ctrader-open-api installed: {HAS_CTRADER}")
    if HAS_CTRADER:
        print(f"  Client: {Client}")
        print(f"  TcpProtocol: {TcpProtocol}")
        print(f"  Protobuf: {Protobuf}")
    print()
    print("  Order type map:")
    for k, v in ORDER_TYPE.items():
        print(f"    {k}: {v}")
    print()
    print("  Required env vars (从 .env / shell 注入, 不进 git):")
    print("    CTRADER_CLIENT_ID, CTRADER_CLIENT_SECRET,")
    print("    CTRADER_ACCESS_TOKEN, CTRADER_ACCOUNT_ID")
    print()
    print("  用法:")
    print("    bridge = CTraderBridge(client_id=..., access_token=..., account_id=...)")
    print("    if bridge.connect():")
    print("        print(bridge.account_info())")
    print("        print(bridge.get_positions())")
    print("        df = bridge.fetch_bars('M15', 1000)")
    print("        bridge.disconnect()")
    print()
    print("  PoC 入口: scripts/ctrader_poc.py")
    print("=" * 70)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    _dry_run()
