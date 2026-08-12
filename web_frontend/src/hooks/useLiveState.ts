import {
  createContext,
  createElement,
  ReactNode,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { getWsTicket, getWsUrl, SessionStats, StateSnapshot } from "@/api/client";
import { factHasDisplayValue, readFactComponent } from "@/api/fact";

type SourceType = "websocket" | "offline";

type LiveStateHookOptions = {
  enabled: boolean;
};

type LiveStateValue = {
  snapshot: StateSnapshot | null;
  error: string | null;
  snapshotRequestFailed: boolean;
  source: SourceType;
  connected: boolean;
  refresh: () => Promise<boolean>;
};

const LiveStateContext = createContext<LiveStateValue | null>(null);

export const LIVE_WS_RECONNECT_BASE_DELAY_MS = 3_000;

function epochSeconds(value: unknown): number {
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value / 1000 : value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric > 1e12 ? numeric / 1000 : numeric;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed / 1000 : 0;
  }
  return 0;
}

function snapshotSequence(snapshot: StateSnapshot | null): number {
  if (!snapshot) return 0;
  const fact = (snapshot as { _fact?: Record<string, unknown> })._fact;
  return epochSeconds(fact?.generated_at) || epochSeconds(snapshot.server_time);
}

export function shouldApplyLiveSnapshot(
  current: StateSnapshot | null,
  incoming: StateSnapshot,
): boolean {
  const currentSequence = snapshotSequence(current);
  const incomingSequence = snapshotSequence(incoming);
  return currentSequence <= 0 || incomingSequence <= 0 || incomingSequence >= currentSequence;
}

function useLiveStateConnection({ enabled }: LiveStateHookOptions): LiveStateValue {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [snapshotRequestFailed, setSnapshotRequestFailed] = useState(false);
  const [source, setSource] = useState<SourceType>("offline");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const retryRef = useRef<number>(0);
  const socketGenerationRef = useRef(0);
  const lifecycleGenerationRef = useRef(0);
  const connectedRef = useRef(false);
  const hasSnapshotRef = useRef(false);
  const snapshotRef = useRef<StateSnapshot | null>(null);
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;

  const wsBase = useMemo(getWsUrl, []);

  const normalizeSnapshot = (incoming: StateSnapshot) => {
    const merged = { ...incoming };
    const asRecord = incoming as Record<string, unknown>;
    const accountFact = readFactComponent(incoming, "account", "live.account.v2");
    const loopFact = readFactComponent(incoming, "loop", "live.loop.v2");
    const daily = asRecord.daily as Record<string, unknown> | undefined;
    if (asRecord.daily && typeof daily === "object" && !asRecord.session_stats) {
      merged.session_stats = {
        pnl_today: Number(daily.pnl ?? daily.pnl_today ?? 0),
        trades: Number(daily.trades ?? 0),
        wins: Number((daily.win ?? 0)),
        losses: Number((daily.loss ?? 0)),
        drawdown_pct: Number(daily.drawdown_pct ?? 0),
        consecutive_loss: Number(daily.consecutive_loss ?? 0),
      } as SessionStats;
    }

    if (
      factHasDisplayValue(accountFact)
      && !asRecord.account
      && (asRecord.balance !== undefined || asRecord.equity !== undefined)
    ) {
      merged.account = {
        ok: true,
        balance: asRecord.balance,
        equity: asRecord.equity,
        margin: asRecord.margin,
        margin_free: asRecord.margin_free,
        leverage: asRecord.leverage,
        currency: asRecord.currency,
      };
    }

    const closedLoop = asRecord.closed_loop as Record<string, unknown> | undefined;
    if (factHasDisplayValue(loopFact) && !asRecord.loop_status && closedLoop && typeof closedLoop === "object") {
      const execution = (closedLoop.execution as Record<string, unknown>) || {};
      merged.loop_status = {
        running: Boolean(closedLoop.pipeline_active),
        broker: String(asRecord.broker || "ctrader"),
        strategy: asRecord.active_strategy && typeof asRecord.active_strategy === "object"
          ? String((asRecord.active_strategy as Record<string, unknown>).source || "factor_v4")
          : "factor_v4",
        reason: String(execution.status || ""),
      };
    }

    return merged;
  };

  const stopSocket = () => {
    // Invalidate every pending ticket request and every old socket callback.
    // WebSocket close events are asynchronous and may arrive after a new
    // socket has already been created.
    socketGenerationRef.current += 1;
    connectedRef.current = false;
    retryRef.current = 0;
    if (retryTimerRef.current !== null) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (wsRef.current !== null) {
      wsRef.current.close();
      wsRef.current = null;
    }
  };

  const clearLiveSnapshot = () => {
    snapshotRef.current = null;
    hasSnapshotRef.current = false;
    setSnapshot(null);
    setSnapshotRequestFailed(false);
  };

  const applySnapshot = (incoming: StateSnapshot, incomingSource: SourceType) => {
    const normalized = normalizeSnapshot(incoming || {});
    const current = snapshotRef.current;
    if (!shouldApplyLiveSnapshot(current, normalized)) return false;
    // /ws/state is the sole live authority. Each message is a complete
    // snapshot; absent fields must not be resurrected from an older message.
    const next = normalized;
    snapshotRef.current = next;
    hasSnapshotRef.current = true;
    setSnapshot(next);
    setSnapshotRequestFailed(false);
    setSource(incomingSource);
    setError(null);
    return true;
  };

  const refreshSnapshot = async (): Promise<boolean> => {
    if (!enabledRef.current) return false;
    clearLiveSnapshot();
    stopSocket();
    void startSocket();
    return true;
  };

  function scheduleSocketRetry(generation: number): void {
    const token = localStorage.getItem("quant.auth.token");
    if (!enabledRef.current || !token || retryTimerRef.current !== null) return;

    retryRef.current = Math.min(retryRef.current + 1, 5);
    const baseDelayMs = LIVE_WS_RECONNECT_BASE_DELAY_MS;
    const backoffMs = baseDelayMs * (2 ** Math.max(retryRef.current - 1, 0));
    const jitterMs = Math.floor(Math.random() * (baseDelayMs / 3));
    retryTimerRef.current = window.setTimeout(() => {
      retryTimerRef.current = null;
      if (
        !enabledRef.current
        || !localStorage.getItem("quant.auth.token")
        || generation !== socketGenerationRef.current
        || wsRef.current !== null
      ) {
        return;
      }
      void startSocket();
    }, backoffMs + jitterMs);
  }

  async function startSocket(): Promise<void> {
    if (!enabledRef.current || !localStorage.getItem("quant.auth.token") || wsRef.current !== null) {
      return;
    }

    const generation = socketGenerationRef.current + 1;
    socketGenerationRef.current = generation;
    try {
      const wsTicket = await getWsTicket();
      if (generation !== socketGenerationRef.current) return;
      const socket = new WebSocket(`${wsBase}?ticket=${encodeURIComponent(wsTicket.ticket)}`);
      wsRef.current = socket;
      const isCurrentSocket = () => (
        generation === socketGenerationRef.current && wsRef.current === socket
      );
      socket.onopen = () => {
        if (!isCurrentSocket()) {
          socket.close();
          return;
        }
        if (retryTimerRef.current !== null) {
          window.clearTimeout(retryTimerRef.current);
          retryTimerRef.current = null;
        }
        retryRef.current = 0;
        connectedRef.current = true;
        setSource("websocket");
        setConnected(true);
        setError(null);
      };
      socket.onmessage = (event) => {
        if (!isCurrentSocket()) return;
        try {
          const payload = JSON.parse(event.data);
          applySnapshot(payload as StateSnapshot, "websocket");
        } catch {
          setError("WS 消息解析失败");
        }
      };
      socket.onclose = (e) => {
        if (!isCurrentSocket()) return;
        connectedRef.current = false;
        setConnected(false);
        setSource("offline");
        wsRef.current = null;
        clearLiveSnapshot();
        if (e.code === 4001) {
          setError("WebSocket 认证失败，等待重连");
          scheduleSocketRetry(generation);
          return;
        }
        if (!hasSnapshotRef.current) {
          setError("WebSocket 已断开，等待重连");
        }
        scheduleSocketRetry(generation);
      };
      socket.onerror = () => {
        if (!isCurrentSocket()) return;
        connectedRef.current = false;
        setConnected(false);
        setSource("offline");
        clearLiveSnapshot();
        if (!hasSnapshotRef.current) {
          setError("WebSocket 连接异常，等待重连");
        }
        // Some runtimes emit error without a subsequent close. Invalidate
        // this socket before scheduling the one allowed retry so a late close
        // callback cannot tear down the replacement socket.
        socketGenerationRef.current += 1;
        wsRef.current = null;
        try {
          socket.close();
        } catch {
          // The socket may already be closing; the retry remains sufficient.
        }
        scheduleSocketRetry(socketGenerationRef.current);
      };
    } catch {
      if (generation !== socketGenerationRef.current) return;
      connectedRef.current = false;
      setConnected(false);
      setSource("offline");
      clearLiveSnapshot();
      if (!hasSnapshotRef.current) {
        setError("WebSocket 创建失败，等待重连");
      }
      scheduleSocketRetry(generation);
    }
  };

  useEffect(() => {
    lifecycleGenerationRef.current += 1;
    if (!enabled) {
      stopSocket();
      setSource("offline");
      setConnected(false);
      connectedRef.current = false;
      setSnapshotRequestFailed(false);
      clearLiveSnapshot();
      return;
    }
    setSource("offline");
    void startSocket();
    const onAuthInvalidated = () => {
      lifecycleGenerationRef.current += 1;
      stopSocket();
      clearLiveSnapshot();
      setSource("offline");
      setConnected(false);
    };
    window.addEventListener("quant-auth-invalidated", onAuthInvalidated);
    return () => {
      window.removeEventListener("quant-auth-invalidated", onAuthInvalidated);
      lifecycleGenerationRef.current += 1;
      stopSocket();
      clearLiveSnapshot();
    };
  // Access-token rotation refreshes the one-time ticket request but must not
  // tear down an already-authenticated WS session and start a second one.
  }, [enabled, wsBase]);

  return {
    snapshot,
    error,
    snapshotRequestFailed,
    source,
    connected,
    refresh: refreshSnapshot,
  };
}

export function LiveStateProvider({
  enabled,
  children,
}: LiveStateHookOptions & { children: ReactNode }) {
  const value = useLiveStateConnection({ enabled });
  return createElement(LiveStateContext.Provider, { value }, children);
}

export function useLiveState(_options?: LiveStateHookOptions): LiveStateValue {
  const value = useContext(LiveStateContext);
  if (!value) {
    throw new Error("useLiveState must be used within LiveStateProvider");
  }
  return value;
}
