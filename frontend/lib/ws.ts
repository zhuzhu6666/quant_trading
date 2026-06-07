"use client";
import { useAppStore } from "./store";

const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000, 30000];

class WSClient {
  private ws: WebSocket | null = null;
  private url = "";
  private attempt = 0;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;

  start(path: string = "/ws/state") {
    this.stopped = false;
    this.url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}${path}`;
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
