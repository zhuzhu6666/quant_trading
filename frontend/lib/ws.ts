"use client";
import { useAppStore } from "./store";

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000, 30000];

// Backend WebSocket base URL. In dev, frontend runs on :3000 but WS is on :8000,
// so we use an explicit env var rather than `location.host` (which would be :3000).
// Production (single-port via reverse proxy or static mount) sets this to same-origin.
const WS_BASE_URL: string =
  process.env.NEXT_PUBLIC_WS_URL ?? "ws://localhost:8000";

class WSClient {
  private ws: WebSocket | null = null;
  private url = "";
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  start(path: string = "/ws/state") {
    this.stopped = false;
    this.url = `${WS_BASE_URL}${path}`;
    this.connect();
  }

  stop() {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    this.ws?.close();
  }

  private connect() {
    if (this.stopped) return;
    try {
      this.ws = new WebSocket(this.url);
    } catch {
      this.scheduleReconnect();
      return;
    }
    this.ws.onopen = () => {
      this.attempt = 0;
      useAppStore.getState().setWsConnected(true);
    };
    this.ws.onmessage = (e) => {
      try {
        const data = JSON.parse(e.data);
        useAppStore.getState().setSnapshot(data);
      } catch {}
    };
    this.ws.onclose = () => {
      useAppStore.getState().setWsConnected(false);
      this.scheduleReconnect();
    };
    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    const delay = RECONNECT_DELAYS[Math.min(this.attempt, RECONNECT_DELAYS.length - 1)];
    this.attempt++;
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }
}

let instance: WSClient | null = null;

export function getWSClient(): WSClient {
  if (!instance) instance = new WSClient();
  return instance;
}
