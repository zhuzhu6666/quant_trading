import CONFIG from '../utils/config';
import { get, post } from './client';
import liveStore from '../stores/live';

let socketTask = null;
let reconnectTimer = null;
let pollTimer = null;
let started = false;
let pollInFlight = null;
let lastPollAt = 0;

function normalizePositionsPayload(rawPositions = []) {
  return (rawPositions || []).map((item) => {
    const pnl = Number(
      item.netUnrealizedPnL ?? item.unrealized ?? item.pnl ?? item.profit ?? 0
    );
    return {
      ...item,
      pnl,
      unrealized: pnl,
      current_price: item.current_price ?? item.price_current ?? 0,
      open_price: item.open_price ?? item.price_open ?? 0,
      volume: item.volume ?? item.size ?? 0,
    };
  });
}

function buildPositionSummary(positions = []) {
  let buys = 0;
  let sells = 0;
  let grossVolume = 0;
  positions.forEach((item) => {
    const volume = Number(item.volume ?? item.size ?? 0);
    grossVolume += volume;
    if (item.type === 'buy') buys += volume;
    if (item.type === 'sell') sells += volume;
  });
  const netVolume = buys - sells;
  const direction = netVolume > 0 ? 'LONG' : netVolume < 0 ? 'SHORT' : 'FLAT';
  let label = '当前无持仓';
  if (positions.length > 0) {
    label = `${direction} ${positions.length} 笔`;
    if (direction === 'FLAT') {
      label = `对冲/混合 ${positions.length} 笔`;
    }
  }
  return {
    direction,
    label,
    netVolume,
    grossVolume,
    buys,
    sells,
  };
}

function enrichTradingSnapshot(baseTrading = {}) {
  const positionsList = normalizePositionsPayload(baseTrading.positions_list || []);
  const realizedPnl = Number(baseTrading.realized_pnl ?? (baseTrading.daily && baseTrading.daily.pnl) ?? 0);
  const unrealizedPnl = positionsList.reduce((sum, item) => sum + Number(item.pnl || 0), 0);
  const positionSummary = buildPositionSummary(positionsList);
  return {
    ...baseTrading,
    positions_list: positionsList,
    n_positions: positionsList.length,
    realized_pnl: realizedPnl,
    unrealized_pnl: unrealizedPnl,
    live_pnl: unrealizedPnl,
    position_summary: positionSummary,
  };
}

function patchFromStatePayload(data) {
  if (!data) return;
  const trading = enrichTradingSnapshot({
    source: data.source || 'none',
    equity: data.equity || 0,
    balance: data.balance || 0,
    pnl_today: data.pnl_today || 0,
    realized_pnl: data.pnl_today || 0,
    position: data.position || { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
    positions_list: data.positions_list || [],
    daily: data.daily || { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
    risk: data.risk || { circuit_breaker: false, consecutive_loss: 0 },
    n_positions: data.n_positions || 0,
    current_price: data.current_price || 0,
  });
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
    url: CONFIG.WS_URL,
    protocols: [token],
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

async function pollLoop(options = {}) {
  const now = Date.now();
  const state = liveStore.getState();
  const force = !!options.force;
  const wsFresh = state.wsConnected && now - (state.lastUpdate || 0) < 3000;

  if (!force && wsFresh) {
    return state;
  }
  if (!force && pollInFlight) {
    return pollInFlight;
  }
  if (!force && now - lastPollAt < 1200) {
    return state;
  }

  lastPollAt = now;
  pollInFlight = (async () => {
    try {
    const [account, positions, strategyStatus, sessionStats, loopStatus, riskSummary, realizedPnlSeries] = await Promise.all([
      get('/api/live/account').catch(() => null),
      get('/api/live/positions').catch(() => null),
      get('/api/live/strategy-status').catch(() => null),
      get('/api/live/session-stats').catch(() => null),
      get('/api/live/loop-status').catch(() => null),
      get('/api/risk/summary').catch(() => null),
      get('/api/live/realized-pnl-series?scope=today').catch(() => null),
    ]);

    const currentTrading = liveStore.getState().trading || {};
    const posList = normalizePositionsPayload((positions && positions.positions) || []);
    const realizedSummary = (realizedPnlSeries && realizedPnlSeries.summary) || {};
    const primaryPosition = posList[0] || null;
    const nextTrading = enrichTradingSnapshot({
      ...currentTrading,
      equity: (account && account.equity) || currentTrading.equity || 0,
      balance: (account && account.balance) || currentTrading.balance || 0,
      positions_list: posList,
      n_positions: posList.length,
      realized_pnl: Number(realizedSummary.realized_pnl ?? (sessionStats && sessionStats.pnl_today) ?? 0),
      position: primaryPosition
        ? {
            dir: primaryPosition.type === 'buy' ? 'LONG' : primaryPosition.type === 'sell' ? 'SHORT' : 'FLAT',
            entry: primaryPosition.open_price || 0,
            size: primaryPosition.volume || 0,
            unrealized: primaryPosition.pnl || 0,
          }
        : { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
      current_price:
        (primaryPosition && primaryPosition.current_price) ||
        currentTrading.current_price ||
        0,
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
    });

    liveStore.setState({
      account,
      strategyStatus,
      sessionStats,
      loopStatus,
      riskSummary,
      realizedPnlSeries: realizedPnlSeries || liveStore.getState().realizedPnlSeries,
      trading: nextTrading,
      lastUpdate: Date.now(),
    });
    return liveStore.getState();
  } catch (err) {
    console.warn('[v2-live] poll failed', err && err.statusCode);
    return liveStore.getState();
  } finally {
    pollInFlight = null;
  }
  })();

  return pollInFlight;
}

export function startLiveRuntime() {
  if (started) return;
  started = true;
  connectSocket();
  pollLoop({ force: true });
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

export async function refreshLiveSnapshot(options = {}) {
  await pollLoop(options);
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
