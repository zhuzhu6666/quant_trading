import { createContext, createElement, type ReactNode, useContext, useEffect, useSyncExternalStore } from "react";
import { getWsTicket, getWsUrl } from "@/api/client";
import { getAccessToken } from "@/auth/tokenStore";
import { syncServerClockFromPayload } from "@/api/time";
import { decodeLiveSnapshot } from "@/api/domains/live";
import { isAuthenticationClose, isCompleteLiveSnapshot, reconnectDelay, shouldAcceptSnapshot, snapshotTimestamp } from "@/hooks/liveStateLogic";
import type { LiveStateSnapshot } from "@/types/contracts";

export type LiveConnectionState = "connecting" | "connected" | "offline" | "auth-failed";

export type LiveStateStore = {
  snapshot: LiveStateSnapshot | null;
  error: string | null;
  connection: LiveConnectionState;
  reconnectCount: number;
  lastCompleteSnapshotAt: string | number | null;
  refresh: () => Promise<boolean>;
};

export const LIVE_WS_RECONNECT_BASE_DELAY_MS = 1_500;

const initialState: LiveStateStore = {
  snapshot: null,
  error: null,
  connection: "offline",
  reconnectCount: 0,
  lastCompleteSnapshotAt: null,
  refresh: async () => false,
};

let state: LiveStateStore = initialState;
const listeners = new Set<() => void>();
let socket: WebSocket | null = null;
let socketGeneration = 0;
let retryTimer: number | null = null;
let retryAttempt = 0;
let started = false;

function emit(next: Partial<LiveStateStore>): void {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

function clearSocket(): void {
  if (retryTimer !== null) {
    window.clearTimeout(retryTimer);
    retryTimer = null;
  }
  socketGeneration += 1;
  if (socket) {
    socket.close();
    socket = null;
  }
}

function scheduleReconnect(generation: number): void {
  if (retryTimer !== null || !started || !getAccessToken()) return;
  retryAttempt = Math.min(retryAttempt + 1, 6);
  const delay = reconnectDelay(retryAttempt, Math.floor(Math.random() * 250));
  retryTimer = window.setTimeout(() => {
    retryTimer = null;
    if (generation === socketGeneration && started && getAccessToken()) void connect();
  }, delay);
}

async function connect(): Promise<void> {
  if (!started || !getAccessToken() || socket) return;
  const generation = ++socketGeneration;
  emit({ connection: "connecting", error: null });
  try {
    const ticket = await getWsTicket();
    if (!started || generation !== socketGeneration || !getAccessToken()) return;
    const nextSocket = new WebSocket(`${getWsUrl()}?ticket=${encodeURIComponent(ticket.ticket)}`);
    socket = nextSocket;
    const current = () => socket === nextSocket && generation === socketGeneration;

    nextSocket.onopen = () => {
      if (!current()) return;
      retryAttempt = 0;
      emit({ connection: "connected", reconnectCount: state.reconnectCount });
    };

    nextSocket.onmessage = (event) => {
      if (!current()) return;
      let decoded: unknown;
      try {
        decoded = JSON.parse(event.data as string) as unknown;
      } catch {
        emit({ error: "live_snapshot_invalid_json" });
        return;
      }
      if (!isCompleteLiveSnapshot(decoded)) {
        emit({ error: "live_snapshot_contract_invalid" });
        return;
      }
      syncServerClockFromPayload(decoded);
      const next = decodeLiveSnapshot(decoded);
      const previousTs = snapshotTimestamp(state.snapshot?.fact ?? null);
      const nextTs = snapshotTimestamp(next.fact);
      if (!shouldAcceptSnapshot(previousTs, nextTs)) return;
      // Each WS message replaces the complete current snapshot.
      emit({
        snapshot: next,
        // The rail describes when the broker facts were observed.  Using
        // generated_at here made the header, fact badge, and source line show
        // three different clocks for the same snapshot.
        lastCompleteSnapshotAt: next.fact.observed_at,
        error: null,
        connection: "connected",
      });
    };

    nextSocket.onerror = () => {
      if (current()) emit({ error: "live_ws_error" });
    };

    nextSocket.onclose = (event) => {
      if (!current()) return;
      socket = null;
      const authFailure = isAuthenticationClose(event.code);
      emit({
        connection: authFailure ? "auth-failed" : "offline",
        snapshot: null,
        error: authFailure ? "live_ws_auth_failed" : "live_ws_disconnected",
        reconnectCount: state.reconnectCount + 1,
      });
      if (!authFailure) scheduleReconnect(generation);
    };
  } catch (error) {
    if (generation !== socketGeneration) return;
    emit({ connection: "offline", snapshot: null, error: error instanceof Error ? error.message : "live_ws_ticket_failed", reconnectCount: state.reconnectCount + 1 });
    scheduleReconnect(generation);
  }
}

async function refresh(): Promise<boolean> {
  if (!started || !getAccessToken()) return false;
  clearSocket();
  retryAttempt = 0;
  emit({ snapshot: null, connection: "connecting", error: null });
  await connect();
  return true;
}

state = { ...state, refresh };

function start(): void {
  if (started) return;
  started = true;
  if (getAccessToken()) void connect();
}

function stop(): void {
  started = false;
  clearSocket();
  retryAttempt = 0;
  emit({ snapshot: null, connection: "offline", error: null, reconnectCount: 0, lastCompleteSnapshotAt: null });
}

const LiveStateContext = createContext<LiveStateStore | null>(null);

export function LiveStateProvider({ children, enabled }: { children: ReactNode; enabled: boolean }) {
  useEffect(() => {
    if (enabled) start();
    else stop();
    return () => stop();
  }, [enabled]);

  useEffect(() => {
    const onToken = () => {
      if (started && !socket && getAccessToken()) void connect();
    };
    const onInvalidated = () => stop();
    window.addEventListener("quant-auth-token", onToken);
    window.addEventListener("quant-auth-invalidated", onInvalidated);
    return () => {
      window.removeEventListener("quant-auth-token", onToken);
      window.removeEventListener("quant-auth-invalidated", onInvalidated);
    };
  }, []);

  const snapshot = useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => state,
    () => state,
  );
  return createElement(LiveStateContext.Provider, { value: snapshot }, children);
}

export function useLiveState(): LiveStateStore {
  const context = useContext(LiveStateContext);
  if (!context) throw new Error("useLiveState must be used inside LiveStateProvider");
  return context;
}
