import { createStore } from '../utils/store';

const store = createStore({
  wsConnected: false,
  lastUpdate: 0,
  trading: {
    source: 'none',
    equity: 0,
    balance: 0,
    pnl_today: 0,
    position: { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
    positions_list: [],
    daily: { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
    risk: { circuit_breaker: false, consecutive_loss: 0 },
    n_positions: 0,
    current_price: 0,
  },
  strategyStatus: null,
  account: null,
  sessionStats: null,
  loopStatus: null,
});

export default store;
