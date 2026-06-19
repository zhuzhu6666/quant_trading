import { useAppStore } from "@/lib/store";

// Debug: confirm this module version is loaded
if (import.meta.env.DEV) {
  console.log("[ws] module loaded, WS_BASE=",
    import.meta.env.VITE_WS_URL || (import.meta.env.DEV ? "ws://localhost:8000" : `ws://${location.host}`));
}

// WebSocket base URL.
// Dev mode: connect directly to backend :8000 (Vite WS proxy may drop subprotocols).
// Prod (single-port): same-origin is correct.
// VITE_WS_URL allows LAN/QA override: VITE_WS_URL=ws://192.168.1.5:8000
const WS_BASE: string =
  import.meta.env.VITE_WS_URL ||
  (import.meta.env.DEV ? "ws://localhost:8000" : `ws://${location.host}`);
const MIN_INTERVAL_MS = 200;

class WSClient {
  private ws: WebSocket | null = null;
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private pendingTimer: ReturnType<typeof setTimeout> | null = null;
  private lastFlush = 0;
  private pendingSnapshot: any = null;
  private path: string = "/ws/state";

  start(path = "/ws/state") {
    this.stopped = false;
    this.path = path;
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
    // ★ close old connection first (prevent connection leak)
    if (this.ws) {
        try { this.ws.onclose = null; this.ws.close(); } catch {}
    }
    // 从 localStorage 取 JWT token 传给后端 WS 鉴权
    const token = localStorage.getItem("quant_token") || "";
    // 同时用 subprotocol + query string 双通道传 token (subprotocol 在 Vite proxy 可能丢失)
    const sep = path.includes("?") ? "&" : "?";
    const url = `${WS_BASE}${path}${token ? `${sep}token=${encodeURIComponent(token)}` : ""}`;
    if (import.meta.env.DEV) console.log("[ws] connecting: token=" + !!token + " url=" + url.slice(0, 80) + "...");
    try { this.ws = token ? new WebSocket(url, [token]) : new WebSocket(url); } catch { this.scheduleReconnect(); return; }
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
    // Don't force-close on transient errors — let onclose handle cleanup naturally.
    // force-close creates a WinError 10054 reset loop on Windows.
    this.ws.onerror = () => {
      // no-op: the browser will fire onclose after onerror
    };
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
    this.reconnectTimer = setTimeout(() => this.connect(this.path), delay);
  }
}

let instance: WSClient | null = null;
export function getWSClient(): WSClient {
  if (!instance) instance = new WSClient();
  return instance;
}
