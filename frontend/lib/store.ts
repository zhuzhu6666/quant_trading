import { create } from "zustand";

export interface StateSnapshot {
  equity: number;
  balance: number;
  pnl_today: number;
  position: { dir: "LONG" | "SHORT" | "FLAT"; entry: number; size: number; unrealized: number };
  daily: { trades: number; win: number; loss: number; pnl: number; drawdown_pct: number };
  risk: { circuit_breaker: boolean; consecutive_loss: number };
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
