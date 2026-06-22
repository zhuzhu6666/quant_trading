"""
execution/ctrader_bridge.py — cTrader Open API 桥接 (并行 MT5, 2026-06-04)

设计目标:
  - 跟 MT5Bridge 同形态: connect/disconnect / market_buy / market_sell /
    close_position / get_positions / account_info / fetch_bars
  - 走 Twisted 异步 + Protobuf 消息 + 回调转 Deferred
  - Pepperstone demo 默认: host=demo.ctraderapi.com:5035, 无 password (走 access_token)
  - 安全闸: send_orders=False 时 market_buy/sell 仅打印不真发 (PoC 默认)

不依赖 MT5; 跟 MT5Bridge 并行, 不替换。
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Phase 4: 统一接口
from execution.base import BaseBrokerBridge, OrderResult, PositionInfo, AccountInfo
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

@dataclass
class CTraderOrderResult:
    """统一订单结果 (跟 MT5OrderResult 同形态, 便于后续抽象 BaseBridge)"""
    success: bool
    order_id: int = 0
    position_id: int = 0
    error_code: str = ""
    comment: str = ""
    price: float = 0.0
    volume: float = 0.0


# Phase 4: CTraderOrderResult -> OrderResult 转换
def _to_order_result(r: CTraderOrderResult) -> "OrderResult":
    return OrderResult(
        success=r.success, order_id=r.order_id, position_id=r.position_id,
        error_code=r.error_code, comment=r.comment, price=r.price, volume=r.volume,
    )


# ── 常量映射 (跟 MT5 FILLING_MODES 风格统一) ────────────

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
    cTrader Open API 桥接 (并行 MT5 接入).

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
                 forced_symbol_id: int | None = None):
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

        self._client: "Client | None" = None
        self._reactor = None
        self._connected = False
        self._connected_lock = threading.Lock()
        self._app_authed = False
        self._account_authed = False
        self._symbol_id: int | None = None
        self._forced_symbol_id = forced_symbol_id  # ProtoOASymbol 无 name, 需外部指定 ID
        self._server_version: str = "v0"  # ★ VersionReq 拿, 给后续 Req clientMsgId 用
        self._trader_login: int = 0  # account_info 返回的 traderLogin, 下单 fallback
        # audit 2026-06-08: 实时报价 (ProtoOASpotEvent 回调更新)
        self._spot_price: float | None = None
        self._spot_lock = threading.Lock()
        # ── 熔断 / 退避 ──
        self._fail_count: int = 0
        self._last_fail_time: float = 0.0
        self._backoff_lock = threading.Lock()
        self._last_error_time: float = 0.0
        self._last_error_msg: str = ""

        # ── 心跳 (每 10 秒 ProtoHeartbeatEvent, 防服务端空闲断开) ──
        self._heartbeat_stop = threading.Event()
        self._heartbeat_thread: threading.Thread | None = None

        # ── 持久 L2 DuckDB 连接 (避免每次 depth event 开/关导致锁冲突) ──
        self._l2_db = None
        self._l2_db_lock = threading.Lock()

        # ── 持久 reactor 线程 (auth 后不停, 保持连接活跃) ──
        self._reactor_started = False
        self._reactor_thread: threading.Thread | None = None

    def has_token(self) -> bool:
        """检查必要凭证是否已设置 (client_id + client_secret + access_token).
        用于 live_service 的 pre-flight 检查, 不做网络调用."""
        return bool(self.client_id and self.client_secret and self.access_token)

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

    # ── 连接管理 ──

    def connect(self) -> bool:
        """连 broker + App auth + Account auth + Symbol resolve。
        首次调用启动持久 reactor 线程 (auth 后不停, 保持 TCP 长连接);
        后续调用复用已运行的 reactor, 只做 auth 链。"""
        with self._connected_lock:
            if self._connected:
                logger.info("Already connected")
                return True

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
                    try: self._client.stopService()
                    except: pass
                self._client = Client(self.host, self.port, TcpProtocol)
            except Exception as e:
                logger.error(f"Client create failed: {e}")
                self._conn_ready.set()
                return

            def _on_conn(c):
                logger.info("cTrader TCP+TLS connected, starting auth")
                self._conn_ok = True
                from twisted.internet import defer
                import uuid
                from ctrader_open_api import Protobuf

                def _unwrap(resp):
                    return Protobuf.extract(resp)

                def _step_app(_dummy=None):
                    req = TradeMsg.ProtoOAApplicationAuthReq()
                    req.clientId = self.client_id
                    req.clientSecret = self.client_secret
                    d = self._client.send(req, clientMsgId=str(uuid.uuid4()),
                                          responseTimeoutInSeconds=self.request_timeout_sec)
                    d.addCallback(_unwrap)
                    def _check(resp):
                        if type(resp).__name__ == "ProtoOAErrorRes":
                            raise RuntimeError(f"App auth rejected: code={resp.errorCode} {resp.description!r}")
                        self._app_authed = True
                        logger.info(f"App auth OK (clientId={self.client_id})")
                    d.addCallback(_check)
                    return d

                def _step_account(_dummy):
                    rl = TradeMsg.ProtoOAGetAccountListByAccessTokenReq()
                    rl.accessToken = self.access_token
                    d = self._client.send(rl, clientMsgId=str(uuid.uuid4()),
                                          responseTimeoutInSeconds=self.request_timeout_sec)
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
                        d2 = self._client.send(r2, clientMsgId=str(uuid.uuid4()),
                                               responseTimeoutInSeconds=self.request_timeout_sec)
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
                    req = TradeMsg.ProtoOASymbolsListReq()
                    req.ctidTraderAccountId = self.account_id
                    d = self._client.send(req, clientMsgId=str(uuid.uuid4()),
                                          responseTimeoutInSeconds=self.request_timeout_sec)
                    d.addCallback(_unwrap)
                    def _check(resp):
                        if type(resp).__name__ == "ProtoOAErrorRes":
                            raise RuntimeError(f"Symbol list rejected: code={resp.errorCode}")
                        for s in resp.symbol:
                            if s.symbolName == self.symbol:
                                self._symbol_id = s.symbolId
                                logger.info(f"Symbol {self.symbol} id={self._symbol_id}")
                                return
                        raise RuntimeError(f"Symbol {self.symbol} not found in list")
                    d.addCallback(_check)
                    return d

                def _on_done(_):
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
                    logger.error(f"cTrader auth failed: {f.getErrorMessage()}")
                    self._conn_ready.set()
                    # ★ 不再 stop reactor — reactor 线程保持, 下次 connect() 重试

                chain = defer.succeed(None)
                chain.addCallback(_step_app)
                chain.addCallback(_step_account)
                chain.addCallback(_step_symbol)
                chain.addCallback(_on_done)
                chain.addErrback(_on_error)

            self._client.setConnectedCallback(_on_conn)
            self._client.setDisconnectedCallback(lambda c, r: self._on_disconnected(r))
            self._client.setMessageReceivedCallback(lambda c, m: self._on_message(c, m))
            try:
                self._client.startService()
            except Exception as e:
                logger.warning(f"startService: {e}")
                self._conn_ready.set()

        # 超时兜底
        timeout = self.request_timeout_sec + 10
        self._reactor.callLater(timeout,
            lambda: (self._conn_ready.set() if not self._conn_ready.is_set() else None))

        # 在已运行的 reactor 线程内执行 _do_connect
        self._reactor.callFromThread(_do_connect)

        # 等 auth 完成 (reactor 线程会 set _conn_ready)
        if not self._conn_ready.wait(timeout=timeout + 5):
            logger.error(f"Connect/auth timeout after {timeout}s")
            self._mark_disconnected()
            return False

        if not self._conn_ok or not self._auth_ok:
            logger.error(f"Connect/auth failed: conn={self._conn_ok} auth={self._auth_ok}")
            self._mark_disconnected()
            return False

        return True

    def _version_handshake(self) -> bool:
        """保留方法, 当前未使用 (官方 sample 不发 VersionReq)"""
        return True  # skip

    def _on_connected(self):
        logger.debug("cTrader Twisted: connected callback fired")

    def _on_disconnected(self, reason):
        logger.warning(f"cTrader Twisted: disconnected ({reason})")
        self._stop_heartbeat()
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

    def _should_log_error(self, msg: str) -> bool:
        """相同错误聚合: 同一消息 60 秒内只打一次,
        避免超时风暴灌满磁盘."""
        now = time.time()
        if msg == self._last_error_msg and now - self._last_error_time < 60:
            return False
        self._last_error_msg = msg
        self._last_error_time = now
        return True

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
                        hb = ProtoHeartbeatEvent()
                        # payloadType 默认为 51 (HEARTBEAT 枚举), 不需要改
                        self._reactor.callFromThread(
                            lambda: self._client.send(hb)
                        )
                except Exception:
                    logger.debug("heartbeat send failed (non-fatal)")
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
            self._handle_depth_event(payload)
        except Exception as e:
            logger.warning(f"_on_message parse failed: {e}")

    def _handle_spot_event(self, payload):
        """处理实时报价更新."""
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASpotEvent
            if not isinstance(payload, ProtoOASpotEvent):
                return
            raw_bid = payload.bid or 0
            raw_ask = payload.ask or 0
            meta = getattr(self, '_symbol_meta', None) or {}
            digits = meta.get('digits', 2)
            pip_pos = meta.get('pip_position', digits)
            price_digits = max(digits, pip_pos)
            divisor = 10 ** price_digits
            bid = raw_bid / divisor if raw_bid else 0
            ask = raw_ask / divisor if raw_ask else 0
            # 自动修正: cTrader demo 的 symbol meta 经常少报精度
            max_val = max(bid, ask)
            for _ in range(5):
                if max_val < 10000:
                    break
                price_digits += 1
                divisor = 10 ** price_digits
                bid = raw_bid / divisor if raw_bid else 0
                ask = raw_ask / divisor if raw_ask else 0
                max_val = max(bid, ask)
            logger.debug(f"spot raw: bid={raw_bid} ask={raw_ask} digits={price_digits} → bid={bid:.2f} ask={ask:.2f}")
            with self._spot_lock:
                if bid > 0 and ask > 0:
                    self._spot_price = (bid + ask) / 2.0
                elif bid > 0:
                    self._spot_price = bid
                elif ask > 0:
                    self._spot_price = ask
        except Exception as e:
            logger.warning(f"spot event parse failed: {e}")

    def _load_depth_counter(self) -> int:
        """从当前时间戳生成 INT32 安全 id, 避免 DB 锁冲突.""" 
        import time
        return (time.time_ns() // 1000) & 0x7FFFFFFF

    def _handle_depth_event(self, payload):
        """处理深度报价 (Level II) 事件."""
        if not hasattr(self, '_depth_quotes'):
            self._depth_quotes = []
            self._depth_lock = threading.Lock()
            # 从 DB 取最大 id 作为起始, 避免重启后计数器重置产生重复
            self._depth_counter = self._load_depth_counter()
        try:
            from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOADepthEvent
            if not isinstance(payload, ProtoOADepthEvent):
                return
            # 收集新报价
            new_quotes = []
            for q in payload.newQuotes:
                bid = q.bid / 100000.0
                ask = q.ask / 100000.0
                size = q.size / 100.0  # 官方: depth 量 ÷ 100
                new_quotes.append({
                    'id': q.id,
                    'bid': round(bid, 2),
                    'ask': round(ask, 2),
                    'size': size,
                })
            deleted_ids = list(payload.deletedQuotes)
            # 存入 bridge 状态
            if not hasattr(self, '_depth_quotes'):
                self._depth_quotes = []
                self._depth_lock = threading.Lock()
            # 更新本地 order book
            with self._depth_lock:
                # 删除旧报价
                if deleted_ids:
                    del_set = set(deleted_ids)
                    self._depth_quotes = [q for q in self._depth_quotes if q['id'] not in del_set]
                # 添加/更新新报价
                for q in new_quotes:
                    found = False
                    for existing in self._depth_quotes:
                        if existing['id'] == q['id']:
                            existing.update(q)
                            found = True
                            break
                    if not found:
                        self._depth_quotes.append(q)
            logger.info(f"depth event: {len(new_quotes)} new, {len(deleted_ids)} deleted, "
                         f"total={len(self._depth_quotes)}")

            # ── 持久化到 l2.duckdb ──
            try:
                import duckdb as _duckdb
                import pandas as _pd
                with self._l2_db_lock:
                    if self._l2_db is None:
                        import os
                        _db_path = os.path.join(
                            os.path.dirname(os.path.abspath(__file__)),
                            "..", "data", "l2.duckdb"
                        )
                        self._l2_db = _duckdb.connect(str(_db_path))
                    _tdb = self._l2_db
                now = time.time()
                # 记录每个变动
                for q in new_quotes:
                    self._depth_counter += 1
                    side = 'bid' if q.get('bid', 0) > 0 else 'ask'
                    price = q.get('bid', 0) or q.get('ask', 0)
                    _tdb.execute("""
                        INSERT INTO orderbook_changes
                        (id, symbol, ts, quote_id, side, price, size, change_type, created_at)
                        VALUES (?, 'XAUUSD+', ?, ?, ?, ?, ?, 'new', ?)
                    """, [self._depth_counter, now, q['id'], side, price, q['size'], now])
                for did in deleted_ids:
                    self._depth_counter += 1
                    _tdb.execute("""
                        INSERT INTO orderbook_changes
                        (id, symbol, ts, quote_id, side, price, size, change_type, created_at)
                        VALUES (?, 'XAUUSD+', ?, ?, '', 0, 0, 'delete', ?)
                    """, [self._depth_counter, now, did, now])
            except Exception as _l2e:
                logger.warning(f"l2 db write failed: {_l2e}")
        except Exception as e:
            logger.warning(f"depth event parse failed: {e}")

    def get_depth_quotes(self) -> list[dict]:
        """获取当前深度报价快照."""
        if not hasattr(self, '_depth_quotes'):
            return []
        with self._depth_lock:
            return list(self._depth_quotes)

    def subscribe_spots(self, symbol_id: int | None = None) -> bool:
        """订阅实时报价 (ProtoOASubscribeSpotsReq). 成功后 _on_message 会持续收到 spot event."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeSpotsReq
        sid = symbol_id or self._symbol_id
        if not sid:
            logger.error("subscribe_spots: no symbol_id")
            return False
        try:
            req = ProtoOASubscribeSpotsReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId.append(sid)
            self._send(req, timeout=5.0)
            logger.info(f"subscribe_spots OK for symbol_id={sid}")
            return True
        except Exception as e:
            logger.warning(f"subscribe_spots failed: {e}")
            return False

    def subscribe_depth(self, symbol_id: int | None = None) -> bool:
        """订阅 L2 深度报价 (ProtoOASubscribeDepthQuotesReq)."""
        from ctrader_open_api.messages.OpenApiMessages_pb2 import ProtoOASubscribeDepthQuotesReq
        sid = symbol_id or self._symbol_id
        if not sid:
            logger.error("subscribe_depth: no symbol_id")
            return False
        try:
            req = ProtoOASubscribeDepthQuotesReq()
            req.ctidTraderAccountId = self.account_id
            req.symbolId.append(sid)
            self._send(req, timeout=5.0)
            logger.info(f"subscribe_depth OK for symbol_id={sid}")
            return True
        except Exception as e:
            logger.warning(f"subscribe_depth failed: {e}")
            return False

    def get_spot_price(self) -> float | None:
        """线程安全读最新 spot 价."""
        with self._spot_lock:
            return self._spot_price

    def disconnect(self):
        """停 client service + 关连接; reactor 留着(全局 reactor 不能跨进程 stop)"""
        self._stop_heartbeat()
        # 关闭持久 L2 DuckDB 连接
        with self._l2_db_lock:
            if self._l2_db is not None:
                try:
                    self._l2_db.close()
                except Exception:
                    pass
                self._l2_db = None
        if self._client:
            try:
                self._client.stopService()
            except Exception:
                pass
        # 不调 reactor.stop() — 那是全局 reactor, 关了影响别的
        with self._connected_lock:
            self._connected = False
        self._app_authed = False
        self._account_authed = False
        logger.info("cTrader disconnected (reactor 留着, 给下次 connect 复用)")

    @property
    def is_connected(self) -> bool:
        with self._connected_lock:
            return (self._connected and self._app_authed
                    and self._account_authed and self._symbol_id is not None)

    # ── 内部: App auth / Account auth ──

    def _send(self, msg, timeout: float | None = None) -> Any:
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
        import uuid
        result_holder: dict = {}

        def _do_send():
            d = self._client.send(
                msg,
                clientMsgId=str(uuid.uuid4()),  # ★ UUID 格式, 跟 server 期望匹配
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
            meta = {
                "symbol_id": full.symbolId,
                "symbol_name": target_name,
                "digits": full.digits,        # 价格小数位 (XAUUSD=2)
                "lot_size": full.lotSize,     # 1 lot = X oz (XAUUSD=100)
                "min_volume": full.minVolume / 100.0,  # centi-lot → lot
                "step_volume": full.stepVolume / 100.0,
                "max_volume": full.maxVolume / 100.0,
                "pip_position": full.pipPosition,
            }
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
                "min_volume": s.minVolume / 100.0,
                "step_volume": s.stepVolume / 100.0,
                "max_volume": s.maxVolume / 100.0,
            } for s in resp.symbol[:10]]
        except Exception as e:
            logger.error(f"get_symbols_list failed: {e}")
            return []

    # ── 订单 ──

    def market_buy(self, symbol: str = "", volume: float = 0.0,
                   sl: float = 0.0, tp: float = 0.0,
                   comment: str = "") -> OrderResult:
        _sym = symbol or self.symbol
        r = self._send_market_order(TRADE_SIDE["BUY"], volume, sl, tp, comment)
        return _to_order_result(r)

    def market_sell(self, symbol: str = "", volume: float = 0.0,
                    sl: float = 0.0, tp: float = 0.0,
                    comment: str = "") -> OrderResult:
        _sym = symbol or self.symbol
        r = self._send_market_order(TRADE_SIDE["SELL"], volume, sl, tp, comment)
        return _to_order_result(r)

    def amend_position_sltp(self, position_id: int,
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
                success=True, position_id=position_id,
                comment=f"DRY-RUN amend sl={sl} tp={tp} (send_orders=False)",
            )

        try:
            req = TradeMsg.ProtoOAAmendPositionSLTPReq()
            order_acct = self._trader_login if self._trader_login > 0 else self.account_id
            req.ctidTraderAccountId = order_acct
            req.positionId = int(position_id)
            # Proto3 semantics: 0 = 不设, 非 0 = 设
            if sl > 0:
                req.stopLoss = float(sl)
            if tp > 0:
                req.takeProfit = float(tp)
            if trailing:
                req.trailingStopLoss = True
            if guaranteed:
                req.guaranteedStopLoss = True
            # 注: stopLossTriggerMethod 不传, 用 server 默认 (1=TRADE)

            resp = self._send(req, timeout=10.0)
            # ⚠️ audit 2026-06-11: amend 可能返回 ProtoOAOrderErrorEvent 而非 raise
            if type(resp).__name__ == "ProtoOAOrderErrorEvent":
                err_code = getattr(resp, "errorCode", "?")
                err_desc = getattr(resp, "description", "")
                logger.error(f"amend_position_sltp rejected: errorCode={err_code} desc={err_desc!r}")
                return CTraderOrderResult(
                    success=False, position_id=position_id,
                    error_code=err_code,
                    comment=f"amend rejected: {err_code} — {err_desc}",
                )
            # 跟 close_position 一致: amend 异步, 真确认靠 ProtoOAExecutionEvent
            return CTraderOrderResult(
                success=True, position_id=position_id,
                comment=f"amend accepted, awaiting ExecutionEvent; resp={type(resp).__name__}",
            )
        except Exception as e:
            err_code = "AMEND_FAILED"
            err_msg = str(e)
            # 协议错误细节
            if hasattr(e, 'description'):
                err_msg = f"{e} ({e.description})"
            logger.error(
                f"amend_position_sltp failed pos={position_id} sl={sl} tp={tp}: {err_msg}"
            )
            return CTraderOrderResult(
                success=False, position_id=position_id,
                error_code=err_code, comment=err_msg,
            )

    # Phase 4: 统一抽象接口方法
    def amend_sl_tp(self, position_id: int, sl: float, tp: float) -> bool:
        r = self.amend_position_sltp(position_id, sl=sl, tp=tp)
        return r.success

    def _send_market_order(self, side: int, volume: float, sl: float, tp: float,
                           comment: str) -> CTraderOrderResult:
        if not self._connected or not self._account_authed:
            return CTraderOrderResult(success=False, comment="Not connected/authed")
        if self._symbol_id is None:
            return CTraderOrderResult(success=False, comment="Symbol ID not resolved")
        # 安全闸: send_orders=False 时仅打印
        if not self.send_orders:
            logger.warning(f"[DRY-RUN] cTrader market {side} vol={volume} sl={sl} tp={tp} (send_orders=False)")
            return CTraderOrderResult(
                success=True, comment="DRY-RUN (send_orders=False)",
                volume=volume, price=sl or tp,  # 占位
            )

        req = TradeMsg.ProtoOANewOrderReq()
        # ★ 优先用 traderLogin (用户可见的账户ID), auth 用的 account_id 可能不同
        order_acct = self._trader_login if self._trader_login > 0 else self.account_id
        req.ctidTraderAccountId = order_acct
        req.symbolId = self._symbol_id
        req.orderType = ORDER_TYPE["MARKET"]
        req.tradeSide = side
        # cTrader volume 字段单位: 1 lot = 100 (centi-lot) per OpenApiPy docs
        # "volume int64 Required Volume, represented in 0.01 of a unit (e.g. 1000 in protocol means 10.00 units)"
        req.volume = int(round(volume * 100))
        req.comment = comment or "quant-live"
        logger.info(
            f"market_order: account={order_acct} (auth={self.account_id}) symbolId={self._symbol_id} "
            f"side={side} volume={req.volume} centilot (= {volume} lot) "
            f"sl={sl} tp={tp}"
        )
        # ⚠️ 阶段 2 MVP: SL/TP 不上 server (cTrader MARKET 单不支持 SL/TP 字段,
        # 需用 MARKET_RANGE 或 AmendOrder 后置). runner 在本地 Python 层做 SL/TP 检查
        # + close_position(). 阶段 3 补 ProtoOAAmendPositionSLTPReq 把 SL/TP 推 server
        try:
            resp = self._send(req, timeout=15.0)
            # ⚠️ audit 2026-06-11: 可能返回 ProtoOAOrderErrorEvent (风控/参数拒)
            if type(resp).__name__ == "ProtoOAOrderErrorEvent":
                err_code = getattr(resp, "errorCode", "?")
                err_desc = getattr(resp, "description", "")
                logger.error(f"market_order rejected: errorCode={err_code} desc={err_desc!r}")
                return CTraderOrderResult(
                    success=False, error_code=err_code,
                    comment=f"rejected: {err_code} — {err_desc}",
                )
            # ProtoOANewOrderRes 不存在! server 回 ProtoOAExecutionEvent (含 position/order/deal)
            # 或 ProtoOAExecutionEvent with errorCode.
            resp_name = type(resp).__name__
            if resp_name == "ProtoOAExecutionEvent":
                err_code = getattr(resp, "errorCode", "")
                if err_code:
                    logger.error(f"market_order execution error: {err_code}")
                    return CTraderOrderResult(
                        success=False, error_code=err_code,
                        comment=f"execution error: {err_code}",
                    )
                # 成功 — 取 positionId 和 orderId
                pos = getattr(resp, "position", None)
                order = getattr(resp, "order", None)
                position_id = pos.positionId if pos else 0
                order_id = order.orderId if order else 0
                return CTraderOrderResult(
                    success=True, order_id=order_id, position_id=position_id,
                    comment=f"filled: orderId={order_id} posId={position_id}",
                )
            # fallback: 未知响应类型 (兼容 old SDK)
            logger.warning(f"market_order unexpected resp type: {resp_name}")
            order_id = getattr(resp, "orderId", 0)
            # 注: ProtoOANewOrderRes 返回的是 orderId, 真实成交价要从 ProtoOAExecutionEvent push 拿
            # MVP 阶段我们后续调 get_positions() 拉真实 entry_price
            return CTraderOrderResult(
                success=True, order_id=order_id,
                comment=f"orderId={order_id}, awaiting get_positions() for entry_price",
            )
        except Exception as e:
            # P0-5: timeout/crash may mask a filled order.
            # Reconcile by fetching current positions before declaring failure.
            logger.warning(
                "market_order send error: %s. Reconciling positions...", e,
            )
            try:
                positions = self.get_positions(self.symbol)
                expected_side = 1 if side == TRADE_SIDE["BUY"] else -1
                matching = [
                    p for p in positions
                    if p.direction == expected_side
                ]
                if matching:
                    # Use the most recent match (last in list by position_id)
                    best = max(matching, key=lambda p: p.position_id)
                    logger.info(
                        "Reconciliation found position %s — order actually filled",
                        best.position_id,
                    )
                    return CTraderOrderResult(
                        success=True,
                        position_id=int(best.position_id),
                        comment=f"reconciled after send error: posId={best.position_id}",
                    )
                logger.info(
                    "Reconciliation: no matching %s position on %s",
                    "BUY" if side == TRADE_SIDE["BUY"] else "SELL",
                    self.symbol,
                )
            except Exception as reconcile_err:
                logger.error("Reconciliation failed: %s", reconcile_err)
            return CTraderOrderResult(success=False, error_code="SEND_ERR", comment=str(e))

    def close_position(self, position_id: int,
                       volume: float = 0.0) -> OrderResult:
        """平仓 (走 ProtoOAClosePositionReq, DRY-RUN 时仅打印).

        Args:
            position_id: 要平的仓位 ID; None 时 broker 默认平当前账户所有仓位
            volume: 部分平仓量 (lots); None 时自动查当前仓位 volume 全平

        ⚠️ audit 2026-06-11: ProtoOAClosePositionReq 4 个字段全部 required
        (payloadType/ctidTraderAccountId/positionId/volume). volume 不能省略.
        """
        if not self.is_connected:
            return OrderResult(success=False, comment="Not connected")
        if not self._account_authed:
            return OrderResult(success=False, comment="Account not authed")
        if position_id <= 0:
            return OrderResult(success=False, comment="position_id required")
        # DRY-RUN 安全闸
        if not self.send_orders:
            logger.warning(f"[DRY-RUN] close_position pos={position_id} vol={volume} (send_orders=False)")
            return OrderResult(
                success=True, position_id=position_id,
                comment="DRY-RUN close (send_orders=False)",
            )
        try:
            # 如果未传 volume, 自动查仓位 volume
            if volume <= 0.0:
                positions = self.get_positions()
                match = [p for p in positions if p.position_id == position_id]
                if not match:
                    return OrderResult(
                        success=False, position_id=position_id,
                        comment=f"Position {position_id} not found in open positions",
                    )
                volume = match[0].volume
                logger.info(f"  auto-resolved volume={volume} for full close")

            req = TradeMsg.ProtoOAClosePositionReq()
            order_acct = self._trader_login if self._trader_login > 0 else self.account_id
            req.ctidTraderAccountId = order_acct
            req.positionId = int(position_id)
            # cTrader volume 字段: 1 lot = 100 (centi-lot) per doc
            # "volume int64 Required Volume, represented in 0.01 of a unit (e.g. 1000 in protocol means 10.00 units)"
            req.volume = int(round(volume * 100))
            resp = self._send(req, timeout=10.0)
            logger.info(f"close_position OK pos={position_id} vol={volume} resp={type(resp).__name__}")
            return OrderResult(
                success=True,
                position_id=position_id,
                volume=volume,
                comment=f"close accepted, awaiting ProtoOAExecutionEvent",
            )
        except Exception as e:
            logger.error(f"close_position failed pos={position_id}: {e}")
            return OrderResult(
                success=False, position_id=position_id,
                error_code="close_failed", comment=str(e),
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
        """
        查账户余额/净值.

        ⚠️ ProtoOATrader 没有 equity/freeMargin 字段; 净值需要从持仓的
        unrealized PnL 累加算出 (另发 ProtoOAGetPositionUnrealizedPnLReq).

        v3 (2026-06-08): 加 _unrealized_pnl() 逐仓查浮动盈亏, 归入 equity.
        若查询失败 (无持仓时跳空, 或 broker 不支持), 回退 balance.

        Margin 同理: cTrader ProtoOAMarginReq 可查, v3 暂略, 返 0.0。
        """
        if not self.is_connected:
            return AccountInfo()
        if self._should_backoff():
            return AccountInfo()
        try:
            req = TradeMsg.ProtoOATraderReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
            t = resp.trader
            balance = t.balance / 100.0  # cTrader balance 存 centi-unit
            # equity = balance + 逐仓 unrealized PnL (centi-unit)
            try:
                unrealized = self._unrealized_pnl()
            except Exception:
                unrealized = 0.0
            equity = balance + unrealized
            login = t.traderLogin
            self._trader_login = login  # 保存 traderLogin, 下单时用作 fallback
            currency = _ASSET_ID_TO_CODE.get(t.depositAssetId, f"ASSET_{t.depositAssetId}")
            logger.info(
                f"Trader info: login={login} balance={balance:.2f} "
                f"depositAssetId={t.depositAssetId} currency={currency} "
                f"leverage={getattr(t, 'maxLeverage', 0)}"
            )
            self._record_success()
            return AccountInfo(
                balance=balance,
                equity=equity,
                margin=0.0,
                margin_free=0.0,
                leverage=float(getattr(t, 'maxLeverage', 0) or 0),
                currency=currency,
                account_id=login,
                name=f"cTrader-{login}",
            )
        except Exception as e:
            self._mark_disconnected()
            self._record_failure()
            if self._should_log_error(f"account_info failed: {e}"):
                logger.error(f"account_info failed: {e}")
            return AccountInfo()

    def _unrealized_pnl(self) -> float:
        """查所有持仓的浮动盈亏总和 (美元, 非 centi-unit).
        0.0 = 无持仓或 broker 不支持.

        ProtoOAGetPositionUnrealizedPnLRes 返 repeated {positionId,
        grossUnrealizedPnL, netUnrealizedPnL}, 单位 centi-unit.
        """
        try:
            req = TradeMsg.ProtoOAGetPositionUnrealizedPnLReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=8.0)
        except Exception:
            return 0.0
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
        """
        查当前持仓. 用 ProtoOAReconcileReq (增量大).

        ⚠️ ProtoOAPosition 没有 tradeSide/volume 顶层字段 — 这些都在
        position.tradeData (ProtoOATradeData) 里.
        """
        if not self.is_connected:
            return []
        if self._should_backoff():
            return []
        try:
            req = TradeMsg.ProtoOAReconcileReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
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
                    symbol=self.symbol,
                    direction=direction,
                    volume=td.volume / 100.0,
                    entry_price=p.price,
                    current_price=p.price,
                    sl=p.stopLoss or 0,
                    tp=p.takeProfit or 0,
                    commission=p.commission / 100.0,
                    swap=p.swap / 100.0,
                ))
            self._record_success()
            return result
        except Exception as e:
            self._mark_disconnected()
            self._record_failure()
            if self._should_log_error(f"get_positions failed: {e}"):
                logger.error(f"get_positions failed: {e}")
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
        period_map = {
            "M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5,
            "M10": 6, "M15": 7, "M30": 8, "H1": 9, "H4": 10,
            "H12": 11, "D1": 12, "W1": 13, "MN1": 14,
            # 一些 broker 用了非官方 enum (e.g. TICK=15, QUOTE=16) —
            # 这里只列主流 14 个, 避免给 server 报 Unknown enum value.
        }
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
            low = round(bar.low / 100000, digits)
            open_ = round((bar.low + bar.deltaOpen) / 100000, digits)
            high = round((bar.low + bar.deltaHigh) / 100000, digits)
            close = round((bar.low + bar.deltaClose) / 100000, digits)
            rows.append({
                "time": bar.utcTimestampInMinutes * 60,  # unix minute → unix second
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": bar.volume,
            })
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
