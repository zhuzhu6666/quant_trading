import { createStore } from '../utils/store';

const store = createStore({
  wsConnected: false,
  lastUpdate: 0,
  lastAttemptAt: 0,
  lastSuccessAt: 0,
  sources: {},
  trading: {
    source: 'none',
    equity: 0,
    balance: 0,
    pnl_today: 0,
    realized_pnl: 0,
    unrealized_pnl_state: 'unknown',
    position: { dir: 'FLAT', entry: 0, size: 0, pnl_state: 'unknown' },
    positions_list: [],
    daily: { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
    risk: { circuit_breaker: false, consecutive_loss: 0 },
    n_positions: 0,
    current_price_state: 'unknown',
    position_components: {
      identity: { state: 'unknown', reason: 'not_observed', observedAt: 0 },
      protection: { state: 'unknown', reason: 'not_observed', observedAt: 0 },
      price: { state: 'unknown', reason: 'not_observed', observedAt: 0 },
      pnl: { state: 'unknown', reason: 'not_observed', observedAt: 0 },
    },
    position_summary: {
      direction: 'FLAT',
      label: '当前无持仓',
      netVolume: 0,
      grossVolume: 0,
      buys: 0,
      sells: 0,
    },
  },
  strategyStatus: null,
  account: null,
  sessionStats: null,
  loopStatus: null,
  riskSummary: null,
  realizedPnlSeries: {
    summary: { realized_pnl: 0, trades: 0, wins: 0, losses: 0, win_rate: 0 },
    points: [],
  },
});

export default store;
