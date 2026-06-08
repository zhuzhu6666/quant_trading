import { create } from "zustand";

/** v8: account block (broker) and position block (broker) both flat for the
 * /总览 page. `source` tells the UI which mode the snapshot came from:
 *   - "live"   = real broker data (MT5 or cTrader), live loop is running
 *   - "paper"  = paper trader state from core.state
 *   - "none"   = neither running (all zeros)
 */
export interface StateSnapshot {
  source: "live" | "paper" | "none";
  broker: "mt5" | "ctrader" | null;
  equity: number;
  balance: number;
  pnl_today: number;
  position: { dir: "LONG" | "SHORT" | "FLAT"; entry: number; size: number; unrealized: number; ticket?: number; sl?: number; tp?: number };
  daily: { trades: number; win: number; loss: number; pnl: number; drawdown_pct: number };
  risk: { circuit_breaker: boolean; consecutive_loss: number };
  // live-only fields
  margin?: number;
  margin_free?: number;
  leverage?: number;
  currency?: string;
  n_positions?: number;
  // nested (optional, for code that wants to see paper vs live explicitly)
  live?: { broker: string; account: any; position: any; n_positions: number } | null;
  paper?: any;
  server_time: string;
}

interface AppState {
  snapshot: StateSnapshot | null;
  wsConnected: boolean;
  setSnapshot: (s: StateSnapshot) => void;
  setWsConnected: (v: boolean) => void;
}

export const useAppStore = create<AppState>((set) => ({
  snapshot: null,
  wsConnected: false,
  setSnapshot: (snapshot) => set({ snapshot }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
}));
