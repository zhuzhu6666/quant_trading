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
import { getStateSnapshot, getWsTicket, getWsUrl, SessionStats, StateSnapshot } from "@/api/client";
import { factHasDisplayValue, readFactComponent } from "@/api/fact";

type SourceType = "websocket" | "http-fallback" | "offline";

type LiveStateHookOptions = {
  enabled: boolean;
};

type LiveStateValue = {
  snapshot: StateSnapshot | null;
  error: string | null;
  source: SourceType;
  connected: boolean;
  refresh: () => Promise<boolean>;
};

const LiveStateContext = createContext<LiveStateValue | null>(null);

export const LIVE_HTTP_ENDPOINT_VERIFY_INTERVAL_MS = 10_000;
export const LIVE_HTTP_ENDPOINT_FALLBACK_INTERVAL_MS = 5_000;
export const LIVE_HTTP_SNAPSHOT_FALLBACK_INTERVAL_MS = 5_000;
export const LIVE_WS_RECONNECT_BASE_DELAY_MS = 3_000;
export const LIVE_WS_SILENCE_TIMEOUT_MS = 8_000;
const LIVE_WS_WATCHDOG_INTERVAL_MS = 1_000;

// HTTP endpoints verify the canonical fact while WS is healthy and provide an
// explicit read-only fallback while WS is reconnecting. The UI keeps the
// transport state visible, so the fallback cannot masquerade as a live socket.
export function liveEndpointRefetchInterval(connected: boolean): number {
  return connected
    ? LIVE_HTTP_ENDPOINT_VERIFY_INTERVAL_MS
    : LIVE_HTTP_ENDPOINT_FALLBACK_INTERVAL_MS;
}

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

type SnapshotFactRecord = Record<string, unknown>;

function isSnapshotFactRecord(value: unknown): value is SnapshotFactRecord {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function mergeSnapshotFactNode(
  current: SnapshotFactRecord | undefined,
  incoming: SnapshotFactRecord | undefined,
): SnapshotFactRecord {
  const merged = { ...(current || {}), ...(incoming || {}) };
  const currentComponents = current?.components;
  const incomingComponents = incoming?.components;
  if (isSnapshotFactRecord(currentComponents) || isSnapshotFactRecord(incomingComponents)) {
    const mergedComponents: SnapshotFactRecord = {
      ...(isSnapshotFactRecord(currentComponents) ? currentComponents : {}),
      ...(isSnapshotFactRecord(incomingComponents) ? incomingComponents : {}),
    };
    for (const key of Object.keys(mergedComponents)) {
      const currentNode = isSnapshotFactRecord(currentComponents)
        ? currentComponents[key]
        : undefined;
      const incomingNode = isSnapshotFactRecord(incomingComponents)
        ? incomingComponents[key]
        : undefined;
      if (isSnapshotFactRecord(currentNode) && isSnapshotFactRecord(incomingNode)) {
        mergedComponents[key] = mergeSnapshotFactNode(currentNode, incomingNode);
      }
    }
    merged.components = mergedComponents;
  }
  return merged;
}

/**
 * WS updates are normally full snapshots, but reconnects and older servers can
 * emit a partial payload. Preserve the last component fact when the new
 * payload does not carry that component; otherwise a shallow spread briefly
 * turns retained account/position data into an unknown value.
 */
export function mergeLiveSnapshot(
  current: StateSnapshot | null,
  incoming: StateSnapshot,
): StateSnapshot {
  const merged = { ...(current || {}), ...(incoming || {}) } as StateSnapshot & { _fact?: SnapshotFactRecord };
  const currentFact = isSnapshotFactRecord((current as { _fact?: unknown } | null)?._fact)
    ? (current as { _fact: SnapshotFactRecord })._fact
    : undefined;
  const incomingFact = isSnapshotFactRecord((incoming as { _fact?: unknown })._fact)
    ? (incoming as { _fact: SnapshotFactRecord })._fact
    : undefined;
  if (currentFact || incomingFact) {
    merged._fact = mergeSnapshotFactNode(currentFact, incomingFact);
  }
  return merged;
}

function useLiveStateConnection({ enabled }: LiveStateHookOptions): LiveStateValue {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<SourceType>("offline");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const refreshInFlightRef = useRef<Promise<boolean> | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const fallbackTimerRef = useRef<number | null>(null);
  const watchdogTimerRef = useRef<number | null>(null);
  const retryRef = useRef<number>(0);
  const socketGenerationRef = useRef(0);
  const lifecycleGenerationRef = useRef(0);
  const connectedRef = useRef(false);
  const lastMessageAtRef = useRef(0);
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

  const stopFallbackSnapshotPolling = () => {
    if (fallbackTimerRef.current !== null) {
      window.clearInterval(fallbackTimerRef.current);
      fallbackTimerRef.current = null;
    }
  };

  const stopSocketWatchdog = () => {
    if (watchdogTimerRef.current !== null) {
      window.clearTimeout(watchdogTimerRef.current);
      watchdogTimerRef.current = null;
    }
    lastMessageAtRef.current = 0;
  };

  const stopSocket = () => {
    // Invalidate every pending ticket request and every old socket callback.
    // WebSocket close events are asynchronous and may arrive after a new
    // socket has already been created.
    socketGenerationRef.current += 1;
    connectedRef.current = false;
    stopSocketWatchdog();
    stopFallbackSnapshotPolling();
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

  const applySnapshot = (incoming: StateSnapshot, incomingSource: SourceType) => {
    const normalized = normalizeSnapshot(incoming || {});
    const current = snapshotRef.current;
    if (!shouldApplyLiveSnapshot(current, normalized)) return false;
    const next = mergeLiveSnapshot(current, normalized);
    snapshotRef.current = next;
    hasSnapshotRef.current = true;
    setSnapshot(next);
    setSource(incomingSource);
    setError(null);
    return true;
  };

  const refreshSnapshot = (): Promise<boolean> => {
    if (!enabledRef.current) return Promise.resolve(false);
    if (refreshInFlightRef.current) return refreshInFlightRef.current;

    const lifecycleGeneration = lifecycleGenerationRef.current;
    const request = (async () => {
      try {
        const next = await getStateSnapshot();
        if (!enabledRef.current || lifecycleGeneration !== lifecycleGenerationRef.current) {
          return false;
        }
        return applySnapshot(next, connectedRef.current ? "websocket" : "http-fallback");
      } catch (exc: unknown) {
        if (!enabledRef.current || lifecycleGeneration !== lifecycleGenerationRef.current) {
          return false;
        }
        const message = exc instanceof Error ? exc.message : String(exc);
        if (!hasSnapshotRef.current) {
          setError(message);
        }
        return false;
      }
    })();
    refreshInFlightRef.current = request;
    void request.then(
      () => {
        if (refreshInFlightRef.current === request) refreshInFlightRef.current = null;
      },
      () => {
        if (refreshInFlightRef.current === request) refreshInFlightRef.current = null;
      },
    );
    return request;
  };

  const startFallbackSnapshotPolling = () => {
    if (
      !enabledRef.current
      || !localStorage.getItem("quant.auth.token")
      || fallbackTimerRef.current !== null
    ) return;
    void refreshSnapshot();
    fallbackTimerRef.current = window.setInterval(() => {
      if (
        !enabledRef.current
        || !localStorage.getItem("quant.auth.token")
        || connectedRef.current
      ) {
        stopFallbackSnapshotPolling();
        return;
      }
      void refreshSnapshot();
    }, LIVE_HTTP_SNAPSHOT_FALLBACK_INTERVAL_MS);
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
      const startSocketWatchdog = () => {
        stopSocketWatchdog();
        lastMessageAtRef.current = Date.now();
        const checkSocketSilence = () => {
          watchdogTimerRef.current = null;
          if (!isCurrentSocket()) return;
          const silenceMs = Date.now() - lastMessageAtRef.current;
          if (silenceMs >= LIVE_WS_SILENCE_TIMEOUT_MS) {
            connectedRef.current = false;
            setConnected(false);
            setSource("offline");
            setError("WebSocket 长时间无消息，已切换 HTTP 快照并重连");
            socketGenerationRef.current += 1;
            wsRef.current = null;
            stopSocketWatchdog();
            try {
              socket.close();
            } catch {
              // The socket may already be closing; the fallback remains active.
            }
            startFallbackSnapshotPolling();
            scheduleSocketRetry(socketGenerationRef.current);
            return;
          }
          watchdogTimerRef.current = window.setTimeout(
            checkSocketSilence,
            LIVE_WS_WATCHDOG_INTERVAL_MS,
          );
        };
        watchdogTimerRef.current = window.setTimeout(
          checkSocketSilence,
          LIVE_WS_WATCHDOG_INTERVAL_MS,
        );
      };
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
        stopFallbackSnapshotPolling();
        startSocketWatchdog();
      };
      socket.onmessage = (event) => {
        if (!isCurrentSocket()) return;
        lastMessageAtRef.current = Date.now();
        try {
          const payload = JSON.parse(event.data);
          applySnapshot(payload as StateSnapshot, "websocket");
        } catch {
          setError("WS 消息解析失败");
        }
      };
      socket.onclose = (e) => {
        if (!isCurrentSocket()) return;
        stopSocketWatchdog();
        connectedRef.current = false;
        setConnected(false);
        setSource("offline");
        wsRef.current = null;
        startFallbackSnapshotPolling();
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
        stopSocketWatchdog();
        connectedRef.current = false;
        setConnected(false);
        setSource("offline");
        if (!hasSnapshotRef.current) {
          setError("WebSocket 连接异常，等待重连");
        }
        // Some runtimes emit error without a subsequent close. Invalidate
        // this socket before scheduling the one allowed retry so a late close
        // callback cannot tear down the replacement socket.
        socketGenerationRef.current += 1;
        wsRef.current = null;
        startFallbackSnapshotPolling();
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
      startFallbackSnapshotPolling();
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
      hasSnapshotRef.current = false;
      snapshotRef.current = null;
      setSnapshot(null);
      return;
    }
    setSource("offline");
    startFallbackSnapshotPolling();
    void startSocket();
    const onAuthInvalidated = () => {
      lifecycleGenerationRef.current += 1;
      stopSocket();
      setSource("offline");
      setConnected(false);
    };
    window.addEventListener("quant-auth-invalidated", onAuthInvalidated);
    return () => {
      window.removeEventListener("quant-auth-invalidated", onAuthInvalidated);
      lifecycleGenerationRef.current += 1;
      stopSocket();
    };
  // Access-token rotation refreshes the one-time ticket request but must not
  // tear down an already-authenticated WS session and start a second one.
  }, [enabled, wsBase]);

  return {
    snapshot,
    error,
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
