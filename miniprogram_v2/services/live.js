import CONFIG from '../utils/config';
import { get, post } from './client';
import liveStore from '../stores/live';

let socketTask = null;
let reconnectTimer = null;
let pollTimer = null;
let started = false;

function patchFromStatePayload(data) {
  if (!data) return;
  const trading = {
    source: data.source || 'none',
    equity: data.equity || 0,
    balance: data.balance || 0,
    pnl_today: data.pnl_today || 0,
    position: data.position || { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
    positions_list: data.positions_list || [],
    daily: data.daily || { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
    risk: data.risk || { circuit_breaker: false, consecutive_loss: 0 },
    n_positions: data.n_positions || 0,
    current_price: data.current_price || 0,
  };
  liveStore.setState({
    trading,
    wsConnected: true,
    lastUpdate: Date.now(),
  });
}

function connectSocket() {
  if (socketTask) return;
  const token = wx.getStorageSync('jwt_token') || '';
  if (!token) return;

  socketTask = wx.connectSocket({
    url: CONFIG.WS_URL + '?token=' + encodeURIComponent(token),
    timeout: 6000,
  });

  socketTask.onOpen(() => {
    liveStore.setState({ wsConnected: true });
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  });

  socketTask.onMessage((res) => {
    try {
      patchFromStatePayload(JSON.parse(res.data));
    } catch (err) {
      console.warn('[v2-live] ws parse failed', err);
    }
  });

  socketTask.onClose(() => {
    liveStore.setState({ wsConnected: false });
    socketTask = null;
    const tokenNow = wx.getStorageSync('jwt_token') || '';
    if (!tokenNow) return;
    reconnectTimer = setTimeout(() => {
      connectSocket();
    }, 4000);
  });

  socketTask.onError(() => {
    liveStore.setState({ wsConnected: false });
  });
}

async function pollLoop() {
    try {
    const [account, positions, strategyStatus, sessionStats, loopStatus] = await Promise.all([
      get('/api/live/account').catch(() => null),
      get('/api/live/positions').catch(() => null),
      get('/api/live/strategy-status').catch(() => null),
      get('/api/live/session-stats').catch(() => null),
      get('/api/live/loop-status').catch(() => null),
    ]);

    liveStore.setState({
      account,
      strategyStatus,
      sessionStats,
      loopStatus,
      lastUpdate: Date.now(),
    });

    const posList = (positions && positions.positions) || [];
    const currentTrading = liveStore.getState().trading || {};
    liveStore.setState({
      trading: {
        ...currentTrading,
        equity: (account && account.equity) || currentTrading.equity || 0,
        balance: (account && account.balance) || currentTrading.balance || 0,
        positions_list: posList,
        n_positions: posList.length,
        daily: sessionStats
          ? {
              trades: sessionStats.trades || 0,
              win: sessionStats.wins || 0,
              loss: sessionStats.losses || 0,
              pnl: sessionStats.pnl_today || 0,
              drawdown_pct: sessionStats.drawdown_pct || 0,
            }
          : currentTrading.daily,
        risk: {
          circuit_breaker: !!(strategyStatus && strategyStatus.circuit_breaker),
          consecutive_loss: (sessionStats && sessionStats.consecutive_loss) || 0,
        },
      },
    });
  } catch (err) {
    console.warn('[v2-live] poll failed', err && err.statusCode);
  }
}

export function startLiveRuntime() {
  if (started) return;
  started = true;
  connectSocket();
  pollLoop();
  pollTimer = setInterval(pollLoop, CONFIG.POLL_INTERVAL);
}

export function stopLiveRuntime() {
  started = false;
  if (socketTask) {
    socketTask.close();
    socketTask = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  liveStore.setState({ wsConnected: false });
}

export async function refreshLiveSnapshot() {
  await pollLoop();
  return liveStore.getState();
}

export async function startTradingLoop() {
  return post('/api/live/start', { broker: 'ctrader', strategy_name: 'factor_v4' });
}

export async function stopTradingLoop() {
  return post('/api/live/stop', {});
}

export async function emergencyCloseAll(symbol = null) {
  return post(
    '/api/live/emergency-close',
    { broker: 'ctrader', symbol },
    { headers: { 'X-Confirm': 'emergency' } }
  );
}
