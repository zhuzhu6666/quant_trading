import { useAppStore } from "@/lib/store";

// WebSocket base URL.
// In dev mode Vite proxies /ws to the backend, so relative works.
// In prod (single-port), same-origin is correct.
// VITE_WS_URL allows LAN/QA override: VITE_WS_URL=ws://192.168.1.5:8000
const WS_BASE: string =
  import.meta.env.VITE_WS_URL || `ws://${location.host}`;
const MIN_INTERVAL_MS = 200;

class WSClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private pendingTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFlush = 0;
  private pendingSnapshot: any = null;

  start(path = "/ws/state") {
    this.stopped = false;
    this.connect(path);
  }

  stop() {
    this.stopped = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pendingTimer) clearTimeout(this.pendingTimer);
    this.ws?.close();
  }

  private connect(path: string) {
    if (this.stopped) return;
    try { this.ws = new WebSocket(`${WS_BASE}${path}`); } catch { this.scheduleReconnect(); return; }
    this.ws.onopen = () => {
      this.attempt = 0;
      useAppStore.getState().setWsConnected(true);
    };
    this.ws.onmessage = (e) => {
      try { this.pendingSnapshot = JSON.parse(e.data); this.scheduleFlush(); } catch {}
    };
    this.ws.onclose = () => {
      useAppStore.getState().setWsConnected(false);
      this.scheduleReconnect();
    };
    this.ws.onerror = () => this.ws?.close();
  }

  private scheduleFlush() {
    if (this.pendingTimer) return;
    const elapsed = performance.now() - this.lastFlush;
    const wait = Math.max(0, MIN_INTERVAL_MS - elapsed);
    this.pendingTimer = setTimeout(() => {
      this.pendingTimer = null;
      this.lastFlush = performance.now();
      const snap = this.pendingSnapshot;
      this.pendingSnapshot = null;
      if (snap) {
        useAppStore.getState().setSnapshot(snap);
        // audit 2026-06-10: append equity point for the MainDashboard chart
        if (snap && typeof snap.equity === "number" && snap.server_time) {
          const t = Math.floor(new Date(snap.server_time).getTime() / 1000);
          if (!isNaN(t)) {
            useAppStore.getState().pushEquityPoint(t, snap.equity);
          }
        }
      }
    }, wait);
  }

  private scheduleReconnect() {
    if (this.stopped) return;
    const delays = [1000, 2000, 4000, 8000, 15000, 30000];
    const delay = delays[Math.min(this.attempt, delays.length - 1)];
    this.attempt++;
    this.reconnectTimer = setTimeout(() => this.connect("/ws/state"), delay);
  }
}

let instance: WSClient | null = null;
export function getWSClient(): WSClient {
  if (!instance) instance = new WSClient();
  return instance;
}
