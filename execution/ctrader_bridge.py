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
import time
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

try:
    from ctrader_open_api import Client, Protobuf, TcpProtocol
    from ctrader_open_api.messages import (
        OpenApiCommonMessages_pb2 as CommonMsg,
        OpenApiMessages_pb2 as TradeMsg,
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


# ── 主类 ────────────────────────────────────────────────

class CTraderBridge:
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
        self._app_authed = False
        self._account_authed = False
        self._symbol_id: int | None = None
        self._forced_symbol_id = forced_symbol_id  # ProtoOASymbol 无 name, 需外部指定 ID
        self._server_version: str = "v0"  # ★ VersionReq 拿, 给后续 Req clientMsgId 用

    # ── 连接管理 ──

    def connect(self) -> bool:
        """连 broker + App auth + Account auth; 同步阻塞到全部完成或失败"""
        if self._connected:
            logger.info("Already connected")
            return True
        try:
            from twisted.internet import reactor
            from twisted.internet import defer
            self._reactor = reactor
        except Exception as e:
            logger.error(f"Twisted reactor import failed: {e}")
            return False

        # Twisted 关键: reactor 必须在 daemon 线程上跑 .run() 阻塞事件循环
        # 一次只能一个 reactor run, 重入会 ReactorAlreadyRunning
        # installSignalHandlers=False: 防 signal.signal() 跨线程错
        from twisted.internet import reactor as default_reactor
        from threading import Thread
        self._reactor = default_reactor
        if not self._reactor.running:
            self._reactor_thread = Thread(
                target=lambda: self._reactor.run(installSignalHandlers=False),
                daemon=True,
            )
            self._reactor_thread.start()
            # 等 reactor 真正跑起来
            for _ in range(50):  # 5s max
                if self._reactor.running:
                    break
                time.sleep(0.1)
            if not self._reactor.running:
                logger.error("Reactor failed to start in 5s")
                return False

        # 官方 sample 写法: client = Client(host, port, TcpProtocol) — 3 参
        # 之前加 numberOfMessagesToSendPerSecond=5 可能是引发 'wrong random id' 的元凶
        # 退回 3 参构造
        self._client = Client(
            self.host, self.port,
            TcpProtocol,
        )

        # 用 callback 推事件 (Twisted reactor 推 model)
        self._conn_deferred: defer.Deferred = defer.Deferred()
        self._client.setConnectedCallback(lambda c: self._on_connected())
        self._client.setDisconnectedCallback(lambda c, r: self._on_disconnected(r))
        # Client service 也需 startService 触发连接尝试
        try:
            self._client.startService()
        except Exception as e:
            logger.warning(f"startService: {e}")

        # 主线程等连接完成
        deadline = time.time() + self.request_timeout_sec
        while not self._client.isConnected and time.time() < deadline:
            time.sleep(0.1)
        if not self._client.isConnected:
            logger.error(f"Connect timeout: {self.host}:{self.port}")
            self.disconnect()
            return False
        self._connected = True
        logger.info(f"cTrader TCP connected: {self.host}:{self.port}")

        # 官方 sample 顺序: connected callback 第一件事就是 App auth
        # 不要发 ProtoOAVersionReq 在前 — 实测会引发 "wrong random id" (协议时序错)
        # 1) App auth
        if not self._app_auth():
            self.disconnect()
            return False
        # 2) Account auth: 先 GetAccountListByAccessTokenReq 拿绑定账户列表
        #    再 ProtoOAAccountAuthReq 真认证 (cTrader 文档 step 8-9)
        if not self._account_auth():
            self.disconnect()
            return False
        # 3) 查 symbol_id 缓存
        self._resolve_symbol_id()
        return True

    def _version_handshake(self) -> bool:
        """保留方法, 当前未使用 (官方 sample 不发 VersionReq)"""
        return True  # skip

    def _on_connected(self):
        logger.debug("cTrader Twisted: connected callback fired")

    def _on_disconnected(self, reason):
        logger.warning(f"cTrader Twisted: disconnected ({reason})")
        self._connected = False

    def disconnect(self):
        """停 client service + 关连接; reactor 留着(全局 reactor 不能跨进程 stop)"""
        if self._client:
            try:
                self._client.stopService()
            except Exception:
                pass
        # 不调 reactor.stop() — 那是全局 reactor, 关了影响别的
        self._connected = False
        self._app_authed = False
        self._account_authed = False
        logger.info("cTrader disconnected (reactor 留着, 给下次 connect 复用)")

    @property
    def is_connected(self) -> bool:
        return self._connected and self._app_authed and self._account_authed

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

        self._reactor.callFromThread(_do_send)

        deadline = time.time() + timeout + 1.0
        while not result_holder and time.time() < deadline:
            time.sleep(0.05)
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

    def market_buy(self, volume: float, sl: float = 0.0, tp: float = 0.0,
                   comment: str = "") -> CTraderOrderResult:
        return self._send_market_order(TRADE_SIDE["BUY"], volume, sl, tp, comment)

    def market_sell(self, volume: float, sl: float = 0.0, tp: float = 0.0,
                    comment: str = "") -> CTraderOrderResult:
        return self._send_market_order(TRADE_SIDE["SELL"], volume, sl, tp, comment)

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

        跟 ProtoOA 协议字段对照:
            stopLoss, takeProfit, trailingStopLoss, guaranteedStopLoss
            都是 Optional 字段. 0 / False 表示不修改, server 保留旧值.

        Returns:
            CTraderOrderResult(success, position_id, comment, error_code)

        ⚠️ 风险:
            1. amend 失败不应重试, 因为 broker 那边 SL/TP 可能已经被 modify 过,
               重试会出现 stale price 错误. caller 负责 log + alarm.
            2. 缩放: cTrader server 接受 absolute price, 但 ProtoOAPosition 里
               stopLoss 是 moneyDigits-scaled. amend 时 caller 传真实价格 (e.g. 2034.5),
               server 自己 scale. 不像 ClosePositionReq 的 volume * 100.
        """
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
            req.ctidTraderAccountId = self.account_id
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
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.orderType = ORDER_TYPE["MARKET"]
        req.tradeSide = side
        # cTrader volume 字段单位: 1 lot = 100 (centi-lot) per OpenApiPy docs
        # "volume int64 Required Volume, represented in 0.01 of a unit (e.g. 1000 in protocol means 10.00 units)"
        req.volume = int(volume * 100)
        req.comment = comment or "quant-live"
        # ⚠️ 阶段 2 MVP: SL/TP 不上 server (cTrader MARKET 单不支持 SL/TP 字段,
        # 需用 MARKET_RANGE 或 AmendOrder 后置). runner 在本地 Python 层做 SL/TP 检查
        # + close_position(). 阶段 3 补 ProtoOAAmendPositionSLTPReq 把 SL/TP 推 server
        try:
            resp = self._send(req, timeout=15.0)
            order_id = getattr(resp, "orderId", 0)
            # 注: ProtoOANewOrderRes 返回的是 orderId, 真实成交价要从 ProtoOAExecutionEvent push 拿
            # MVP 阶段我们后续调 get_positions() 拉真实 entry_price
            return CTraderOrderResult(
                success=True, order_id=order_id,
                comment=f"orderId={order_id}, awaiting get_positions() for entry_price",
            )
        except Exception as e:
            return CTraderOrderResult(success=False, error_code="SEND_ERR", comment=str(e))

    def close_position(self, position_id: int | None = None,
                       volume: float | None = None) -> CTraderOrderResult:
        """平仓 (走 ProtoOAClosePositionReq, DRY-RUN 时仅打印).

        Args:
            position_id: 要平的仓位 ID; None 时 broker 默认平当前账户所有仓位
            volume: 部分平仓量 (lots); None 时全平
        """
        if not self.is_connected:
            return CTraderOrderResult(success=False, comment="Not connected")
        if not self._account_authed:
            return CTraderOrderResult(success=False, comment="Account not authed")
        # DRY-RUN 安全闸
        if not self.send_orders:
            logger.warning(f"[DRY-RUN] close_position pos={position_id} vol={volume} (send_orders=False)")
            return CTraderOrderResult(
                success=True, position_id=position_id or 0,
                comment="DRY-RUN close (send_orders=False)",
            )
        try:
            req = TradeMsg.ProtoOAClosePositionReq()
            req.ctidTraderAccountId = self.account_id
            if position_id is not None:
                req.positionId = int(position_id)
            if volume is not None:
                # cTrader volume 字段: 1 lot = 100 (centi-lot) per doc
                # "volume int64 Required Volume, represented in 0.01 of a unit (e.g. 1000 in protocol means 10.00 units)"
                req.volume = int(volume * 100)
            resp = self._send(req, timeout=10.0)
            logger.info(f"close_position OK pos={position_id} vol={volume} resp={type(resp).__name__}")
            return CTraderOrderResult(
                success=True,
                position_id=position_id or 0,
                volume=volume or 0.0,
                comment=f"close accepted, awaiting ProtoOAExecutionEvent",
            )
        except Exception as e:
            logger.error(f"close_position failed pos={position_id}: {e}")
            return CTraderOrderResult(
                success=False, position_id=position_id or 0,
                error_code="close_failed", comment=str(e),
            )

    # ── 账户 ──

    def account_info(self) -> dict:
        """
        查账户余额/净值.

        ⚠️ ProtoOATrader 没有 equity/freeMargin 字段; 净值要等
        ProtoOAGetPositionUnrealizedPnLReq 算 (本次 PoC 先回 balance + leverage).
        """
        if not self.is_connected:
            return {}
        try:
            req = TradeMsg.ProtoOATraderReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
            t = resp.trader
            return {
                "balance": t.balance / 100.0,  # cTrader balance 存 centi-unit
                "equity": None,                  # 需另查
                "margin": None,
                "margin_free": None,
                "leverage": t.maxLeverage,        # 字段是 maxLeverage 不是 leverage
                "leverage_in_cents": t.leverageInCents,  # 1 = 100x
                "currency_asset_id": t.depositAssetId,   # 是 assetId int 不是 currency str
                "swap_free": t.swapFree,
                "trader_login": t.traderLogin,
            }
        except Exception as e:
            logger.error(f"account_info failed: {e}")
            return {}

    def get_positions(self, symbol: str | None = None) -> list[dict]:
        """
        查当前持仓. 用 ProtoOAReconcileReq (增量大).

        ⚠️ ProtoOAPosition 没有 tradeSide/volume 顶层字段 — 这些都在
        position.tradeData (ProtoOATradeData) 里.
        """
        if not self.is_connected:
            return []
        try:
            req = TradeMsg.ProtoOAReconcileReq()
            req.ctidTraderAccountId = self.account_id
            resp = self._send(req, timeout=10.0)
            result = []
            for p in resp.position:
                if symbol and p.symbolId != self._symbol_id:
                    continue
                # tradeData 是嵌套消息, 取 tradeSide/volume
                td = p.tradeData
                result.append({
                    "position_id": p.positionId,
                    "symbol_id": p.symbolId,
                    "type": "buy" if td.tradeSide == TRADE_SIDE["BUY"] else "sell",
                    "volume": td.volume / 100.0,         # centi-lot → lot
                    "price_open": p.price / (10 ** p.moneyDigits),  # moneyDigits 不是 symbol.digits
                    "sl": p.stopLoss / (10 ** p.moneyDigits) if p.stopLoss else 0,
                    "tp": p.takeProfit / (10 ** p.moneyDigits) if p.takeProfit else 0,
                    "profit": None,  # 需 ProtoOAGetPositionUnrealizedPnLReq
                    "swap": p.swap / 100.0,
                    "commission": p.commission / 100.0,
                })
            return result
        except Exception as e:
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
        period_map = {"M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M10": 10,
                      "M15": 15, "M30": 30, "H1": 60, "H4": 240,
                      "D1": 1440, "W1": 10080, "MN1": 43200}
        period = period_map.get(timeframe)
        if period is None:
            logger.error(f"Unknown timeframe {timeframe}")
            return None

        try:
            import pandas as pd
        except ImportError:
            logger.error("pandas not installed")
            return None

        # 单次拉一批, cTrader count 上限 ~5000
        req = TradeMsg.ProtoOAGetTrendbarsReq()
        req.ctidTraderAccountId = self.account_id
        req.symbolId = self._symbol_id
        req.period = period
        req.count = min(n_bars, 5000)
        now_min = int(time.time() // 60)
        req.fromTimestamp = now_min - n_bars * period
        req.toTimestamp = now_min
        try:
            resp = self._send(req, timeout=20.0)
        except Exception as e:
            logger.error(f"fetch_bars failed: {e}")
            return None
        if not resp.trendbar:
            return None
        # ⚠️ moneyDigits 需 SymbolByIdReq 拿, 先固定 2 (XAUUSD = 4512.34 2 digits)
        digits = 2
        rows = []
        for bar in resp.trendbar:
            # 低点有绝对值; 高/开/收是 delta 相对前 close — 没前 close 时算不出
            # 先用 low 估计 (lossy 凑合看, 真 OHLC 走阶段 2 spot 订阅)
            low = bar.low / (10 ** digits)
            # delta 是 pips (10^digits 倍数 = 1 USD)
            # close = low + deltaClose / 10^digits; 但没有 prev close
            # PoC 阶段: 用 low 兜底全部 4 个, 标注 lossy
            close = low + bar.deltaClose / (10 ** digits)
            open_ = close - bar.deltaOpen / (10 ** digits)  # 估算
            high = max(open_, close, low) + bar.deltaHigh / (10 ** digits)
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
        logger.warning("fetch_bars LOSSY: only low is absolute; O/H/C estimated from delta")
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
