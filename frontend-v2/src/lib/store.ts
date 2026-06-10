import { create } from "zustand";

export interface WSnapshot {
  source: "live" | "paper" | "none";
  broker: "mt5" | "ctrader" | null;
  equity: number;
  balance: number;
  pnl_today: number;
  currency: string;
  leverage: number;
  margin: number;
  margin_free: number;
  n_positions: number;
  current_price: number | null;
  active_strategy: { id: string | null; mode: string; source: string };
  position: { dir: string; entry: number; size: number; unrealized: number };
  daily: { trades: number; win: number; loss: number; pnl: number; drawdown_pct: number };
  risk: { circuit_breaker: boolean; consecutive_loss: number };
  live?: { broker: string; account: any; position: any; n_positions: number } | null;
  paper?: any;
  server_time: string;
}

const TOKEN_KEY = "quant_token";
const USER_KEY = "quant_user";

function getStoredToken(): string | null {
  try {
    return typeof window !== "undefined" ? localStorage.getItem(TOKEN_KEY) : null;
  } catch {
    return null;
  }
}

function getStoredUser(): string | null {
  try {
    return typeof window !== "undefined" ? localStorage.getItem(USER_KEY) : null;
  } catch {
    return null;
  }
}

interface State {
  snapshot: WSnapshot | null;
  equityHistory: { t: number; v: number }[];
  wsConnected: boolean;
  setSnapshot: (s: WSnapshot | null) => void;
  pushEquityPoint: (t: number, v: number) => void;
  setWsConnected: (v: boolean) => void;
  token: string | null;
  user: string | null;
  setAuth: (token: string, user: string) => void;
  clearAuth: () => void;
}

export const useAppStore = create<State>((set) => ({
  snapshot: null,
  equityHistory: [],
  wsConnected: false,
  setSnapshot: (snapshot) => set({ snapshot }),
  pushEquityPoint: (t, v) => set((s) => {
    const arr = s.equityHistory ?? [];
    // dedup: skip if value unchanged from last point
    if (arr.length > 0 && arr[arr.length - 1].v === v) return s;
    const next = [...arr, { t, v }];
    return { equityHistory: next.length > 200 ? next.slice(-200) : next };
  }),
  setWsConnected: (wsConnected) => set({ wsConnected }),
  token: getStoredToken(),
  user: getStoredUser(),
  setAuth: (token, user) => {
    try {
      localStorage.setItem(TOKEN_KEY, token);
      localStorage.setItem(USER_KEY, user);
    } catch {
      /* noop */
    }
    set({ token, user });
  },
  clearAuth: () => {
    try {
      localStorage.removeItem(TOKEN_KEY);
      localStorage.removeItem(USER_KEY);
    } catch {
      /* noop */
    }
    set({ token: null, user: null });
  },
}));
