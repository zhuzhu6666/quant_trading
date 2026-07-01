import { useEffect, useMemo, useRef, useState } from "react";
import { getStateSnapshot, getWsUrl, SessionStats, StateSnapshot } from "@/api/client";

type SourceType = "websocket" | "polling" | "offline";

type LiveStateHookOptions = {
  enabled: boolean;
  pollIntervalMs?: number;
};

export function useLiveState({ enabled, pollIntervalMs = 4000 }: LiveStateHookOptions) {
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
  const token = localStorage.getItem("quant.auth.token");

  const wsBase = useMemo(getWsUrl, []);

  const normalizeSnapshot = (incoming: StateSnapshot) => {
    const merged = { ...incoming };
    const asRecord = incoming as Record<string, unknown>;
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

    if (!asRecord.account && (asRecord.balance !== undefined || asRecord.equity !== undefined)) {
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
    if (!asRecord.loop_status && closedLoop && typeof closedLoop === "object") {
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
      const next = { ...(prev || {}), ...(normalized || {}) };
      return next;
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
      if (!snapshot) {
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

  const startSocket = () => {
    if (!token) {
      return;
    }
    const normalized = token.trim();
    const variants = [
      { url: wsBase, protocols: [normalized] as string[] },
      { url: `${wsBase}?token=${encodeURIComponent(normalized)}` },
    ];
    const candidate = variants[Math.min(retryRef.current, variants.length - 1)];

    closeRequestedRef.current = false;
    try {
      const socket = new WebSocket(candidate.url, (candidate.protocols as string[]) || []);
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
            startSocket();
          }
        }, 3000);
      };
      socket.onerror = () => {
        connectedRef.current = false;
        setConnected(false);
        setError("WebSocket 连接异常，尝试轮询");
        startPolling();
      };
    } catch {
      setError("WebSocket 创建失败，回退到轮询");
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
      setSnapshot(null);
      return;
    }
    setSource("polling");
    startPolling();
    startSocket();
    return () => {
      stopPolling();
      stopSocket();
    };
  }, [enabled, pollIntervalMs]);

  return {
    snapshot,
    error,
    source,
    connected,
    refresh: fetchSnapshot,
  };
}
