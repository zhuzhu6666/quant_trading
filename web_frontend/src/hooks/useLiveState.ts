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

type SourceType = "websocket" | "polling" | "offline";

type LiveStateHookOptions = {
  enabled: boolean;
  pollIntervalMs?: number;
};

type LiveStateValue = {
  snapshot: StateSnapshot | null;
  error: string | null;
  source: SourceType;
  connected: boolean;
  refresh: () => Promise<boolean>;
};

const LiveStateContext = createContext<LiveStateValue | null>(null);

export const LIVE_HTTP_POLL_INTERVAL_MS = {
  snapshotFallback: 4_000,
  endpointFallback: 3_000,
  endpointVerification: 10_000,
} as const;

export function liveEndpointPollInterval(connected: boolean): number {
  return connected
    ? LIVE_HTTP_POLL_INTERVAL_MS.endpointVerification
    : LIVE_HTTP_POLL_INTERVAL_MS.endpointFallback;
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

function useLiveStateConnection({
  enabled,
  pollIntervalMs = LIVE_HTTP_POLL_INTERVAL_MS.snapshotFallback,
}: LiveStateHookOptions): LiveStateValue {
  const [snapshot, setSnapshot] = useState<StateSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [source, setSource] = useState<SourceType>("offline");
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const pollRef = useRef<number | null>(null);
  const retryTimerRef = useRef<number | null>(null);
  const retryRef = useRef<number>(0);
  const closeRequestedRef = useRef(false);
  const connectedRef = useRef(false);
  const hasSnapshotRef = useRef(false);
  const token = localStorage.getItem("quant.auth.token");

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

  const stopPolling = () => {
    if (pollRef.current) {
      window.clearInterval(pollRef.current);
      pollRef.current = null;
    }
  };

  const stopSocket = () => {
    closeRequestedRef.current = true;
    connectedRef.current = false;
    if (retryTimerRef.current) {
      window.clearTimeout(retryTimerRef.current);
      retryTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
  };

  const applySnapshot = (incoming: StateSnapshot) => {
    const normalized = normalizeSnapshot(incoming || {});
    setSnapshot((prev) => {
      if (!shouldApplyLiveSnapshot(prev, normalized)) return prev;
      hasSnapshotRef.current = true;
      return { ...(prev || {}), ...(normalized || {}) };
    });
    setError(null);
  };

  const fetchSnapshot = async () => {
    try {
      const next = await getStateSnapshot();
      applySnapshot(next);
      if (!connectedRef.current) {
        setSource("polling");
        setConnected(false);
      }
      return true;
    } catch (exc: unknown) {
      const message = exc instanceof Error ? exc.message : String(exc);
      if (!hasSnapshotRef.current) {
        setError(message);
      }
      return false;
    }
  };

  const startPolling = () => {
    if (pollRef.current) {
      return;
    }
    void fetchSnapshot();
    pollRef.current = window.setInterval(() => {
      void fetchSnapshot();
    }, pollIntervalMs);
  };

  const startSocket = async () => {
    if (!token) {
      return;
    }

    closeRequestedRef.current = false;
    try {
      const wsTicket = await getWsTicket();
      if (closeRequestedRef.current) return;
      const socket = new WebSocket(`${wsBase}?ticket=${encodeURIComponent(wsTicket.ticket)}`);
      wsRef.current = socket;
      socket.onopen = () => {
        retryRef.current = 0;
        connectedRef.current = true;
        setSource("websocket");
        setConnected(true);
        setError(null);
        stopPolling();
      };
      socket.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          applySnapshot(payload as StateSnapshot);
        } catch {
          setError("WS 消息解析失败");
        }
      };
      socket.onclose = (e) => {
        connectedRef.current = false;
        setConnected(false);
        setSource("polling");
        wsRef.current = null;
        if (closeRequestedRef.current) {
          return;
        }
        if (e.code === 4001) {
          setError("WebSocket 认证失败，已回退轮询");
          startPolling();
          return;
        }
        startPolling();
        retryRef.current = Math.min(retryRef.current + 1, 5);
        if (retryTimerRef.current) {
          window.clearTimeout(retryTimerRef.current);
        }
        retryTimerRef.current = window.setTimeout(() => {
          if (!closeRequestedRef.current) {
            void startSocket();
          }
        }, 3000);
      };
      socket.onerror = () => {
        connectedRef.current = false;
        setConnected(false);
        if (!hasSnapshotRef.current) {
          setError("WebSocket 连接异常，尝试轮询");
        }
        startPolling();
      };
    } catch {
      if (!hasSnapshotRef.current) {
        setError("WebSocket 创建失败，回退到轮询");
      }
      startPolling();
    }
  };

  useEffect(() => {
    if (!enabled) {
      stopPolling();
      stopSocket();
      setSource("offline");
      setConnected(false);
      connectedRef.current = false;
      hasSnapshotRef.current = false;
      setSnapshot(null);
      return;
    }
    setSource("polling");
    startPolling();
    void startSocket();
    const onAuthInvalidated = () => {
      stopPolling();
      stopSocket();
      setSource("offline");
      setConnected(false);
    };
    window.addEventListener("quant-auth-invalidated", onAuthInvalidated);
    return () => {
      window.removeEventListener("quant-auth-invalidated", onAuthInvalidated);
      stopPolling();
      stopSocket();
    };
  }, [enabled, pollIntervalMs, token, wsBase]);

  return {
    snapshot,
    error,
    source,
    connected,
    refresh: fetchSnapshot,
  };
}

export function LiveStateProvider({
  enabled,
  pollIntervalMs = LIVE_HTTP_POLL_INTERVAL_MS.snapshotFallback,
  children,
}: LiveStateHookOptions & { children: ReactNode }) {
  const value = useLiveStateConnection({ enabled, pollIntervalMs });
  return createElement(LiveStateContext.Provider, { value }, children);
}

export function useLiveState(_options?: LiveStateHookOptions): LiveStateValue {
  const value = useContext(LiveStateContext);
  if (!value) {
    throw new Error("useLiveState must be used within LiveStateProvider");
  }
  return value;
}
