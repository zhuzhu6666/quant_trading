import CONFIG from '../utils/config';
import { get, post } from './client';
import liveStore from '../stores/live';
import {
  factSource,
  factUsable,
  reduceLivePollOutcome,
  reduceLiveWsDisconnected,
  reduceLiveWsOutcome,
} from '../stores/liveReducer';
import { reducePositionFactSnapshot } from '../stores/livePositionFacts';

let socketTask = null;
let reconnectTimer = null;
let pollTimer = null;
let started = false;
let pollInFlight = null;
let lastPollAt = 0;
let reconnectAttempts = 0;

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
  const positionsList = Array.isArray(baseTrading.positions_list) ? baseTrading.positions_list : [];
  const positionSummary = buildPositionSummary(positionsList);
  return {
    ...baseTrading,
    positions_list: positionsList,
    position_summary: positionSummary,
  };
}

const POLL_SOURCES = [
  { name: 'account', contract: 'live.account.v2' },
  { name: 'positions', contract: 'live.positions.v2' },
  { name: 'strategy', contract: 'live.strategy.v2' },
  { name: 'session', contract: 'live.session-risk.v2' },
  { name: 'loop', contract: 'live.loop.v2' },
  { name: 'risk', contract: 'risk.summary.v2' },
  { name: 'realized', contract: 'live.realized-pnl.v2' },
];

function settledSource(result, now, expectedContract) {
  if (!result || result.status === 'rejected') {
    return { state: 'error', reason: 'request_failed', observedAt: 0, staleAfterSec: 0 };
  }
  return factSource(result.value, now, expectedContract);
}

function patchFromStatePayload(data) {
  if (!data) return;
  const attemptedAt = Date.now();
  const current = liveStore.getState();
  const patch = reduceLiveWsOutcome(current, data, attemptedAt);
  const positionOutcome = reducePositionFactSnapshot(current.trading || {}, data, attemptedAt);
  if (patch.trading || positionOutcome.changed) {
    patch.trading = enrichTradingSnapshot({
      ...(patch.trading || current.trading || {}),
      ...(positionOutcome.patch || {}),
    });
  }
  if (!patch.lastSuccessAt && positionOutcome.usable) {
    patch.lastSuccessAt = attemptedAt;
    patch.lastUpdate = attemptedAt;
  }
  liveStore.setState(patch);
}

function scheduleReconnect() {
  const token = wx.getStorageSync('jwt_token') || '';
  if (!started || !token || reconnectTimer !== null) return;

  const delayMs = Math.min(4000 * (2 ** reconnectAttempts), CONFIG.REFRESH_INTERVAL);
  reconnectAttempts += 1;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    void connectSocket();
  }, delayMs);
}

async function connectSocket() {
  if (socketTask) return;
  const token = wx.getStorageSync('jwt_token') || '';
  if (!token) return;

  let ticket = '';
  try {
    const response = await post('/api/auth/ws-ticket', {});
    ticket = response && response.ticket || '';
  } catch (err) {
    liveStore.setState(reduceLiveWsDisconnected(
      liveStore.getState(),
      Date.now(),
      'ws_ticket_failed',
    ));
    scheduleReconnect();
    return;
  }
  if (!ticket) {
    liveStore.setState(reduceLiveWsDisconnected(
      liveStore.getState(),
      Date.now(),
      'ws_ticket_missing',
    ));
    scheduleReconnect();
    return;
  }
  if (!started || socketTask) return;

  const socket = wx.connectSocket({
    url: `${CONFIG.WS_URL}?ticket=${encodeURIComponent(ticket)}`,
    timeout: 6000,
  });
  socketTask = socket;

  socket.onOpen(() => {
    if (socketTask !== socket) {
      socket.close();
      return;
    }
    reconnectAttempts = 0;
    liveStore.setState({ wsConnected: true });
    if (reconnectTimer !== null) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  });

  socket.onMessage((res) => {
    if (socketTask !== socket) return;
    try {
      patchFromStatePayload(JSON.parse(res.data));
    } catch (err) {
      console.warn('[v2-live] ws parse failed', err);
    }
  });

  socket.onClose(() => {
    if (socketTask !== socket) return;
    const attemptedAt = Date.now();
    liveStore.setState(reduceLiveWsDisconnected(
      liveStore.getState(),
      attemptedAt,
      'ws_closed',
    ));
    socketTask = null;
    scheduleReconnect();
  });

  socket.onError(() => {
    if (socketTask !== socket) return;
    liveStore.setState(reduceLiveWsDisconnected(
      liveStore.getState(),
      Date.now(),
      'ws_error',
    ));
    socketTask = null;
    try {
      socket.close();
    } catch (err) {
      // The runtime may already have closed the failed socket.
    }
    scheduleReconnect();
  });
}

async function pollLoop(options = {}) {
  const now = Date.now();
  const state = liveStore.getState();
  const force = !!options.force;
  const wsFresh = state.wsConnected && now - (state.lastUpdate || 0) < 3000;

  if (pollInFlight) {
    return pollInFlight;
  }
  if (!force && wsFresh) {
    return state;
  }
  if (now - lastPollAt < 1200) {
    return state;
  }

  lastPollAt = now;
  pollInFlight = (async () => {
    const attemptedAt = Date.now();
    try {
    const settled = await Promise.allSettled([
      get('/api/live/account'),
      get('/api/live/positions'),
      get('/api/live/strategy-status'),
      get('/api/live/session-stats'),
      get('/api/live/loop-status'),
      get('/api/risk/summary'),
      get('/api/live/realized-pnl-series?scope=all'),
    ]);
    const sourcePatch = {};
    const payloads = {};
    settled.forEach((result, index) => {
      const source = POLL_SOURCES[index];
      sourcePatch[source.name] = settledSource(result, attemptedAt, source.contract);
      if (result.status === 'fulfilled') payloads[source.name] = result.value;
    });
    const previous = liveStore.getState();
    const sources = { ...(previous.sources || {}), ...sourcePatch };
    const payloadIsUsable = (name) => {
      const source = POLL_SOURCES.find((item) => item.name === name);
      return !!source && factUsable(payloads[name], attemptedAt, source.contract);
    };
    const positionOutcome = settled[1]?.status === 'fulfilled'
      ? reducePositionFactSnapshot(previous.trading || {}, payloads.positions || {}, attemptedAt)
      : { changed: false, usable: false, patch: {} };
    const topLevelUsableCount = POLL_SOURCES.filter((source) => payloadIsUsable(source.name)).length;
    const usableCount = topLevelUsableCount
      + (positionOutcome.usable && !payloadIsUsable('positions') ? 1 : 0);
    if (usableCount === 0) {
      const failedPatch = reduceLivePollOutcome(previous, {
        attemptedAt,
        sources,
        usableCount,
      });
      if (positionOutcome.changed) {
        failedPatch.trading = enrichTradingSnapshot({
          ...(previous.trading || {}),
          ...positionOutcome.patch,
        });
      }
      liveStore.setState(failedPatch);
      return liveStore.getState();
    }

    const account = payloadIsUsable('account') ? payloads.account : previous.account;
    const strategyStatus = payloadIsUsable('strategy') ? payloads.strategy : previous.strategyStatus;
    const sessionStats = payloadIsUsable('session') ? payloads.session : previous.sessionStats;
    const loopStatus = payloadIsUsable('loop') ? payloads.loop : previous.loopStatus;
    const riskSummary = payloadIsUsable('risk') ? payloads.risk : previous.riskSummary;
    const realizedPnlSeries = payloadIsUsable('realized') ? payloads.realized : previous.realizedPnlSeries;

    const currentTrading = previous.trading || {};
    const realizedSummary = (realizedPnlSeries && realizedPnlSeries.summary) || {};
    const nextTrading = enrichTradingSnapshot({
      ...currentTrading,
      ...positionOutcome.patch,
      equity: account && account.equity !== undefined ? account.equity : currentTrading.equity,
      balance: account && account.balance !== undefined ? account.balance : currentTrading.balance,
      realized_pnl: Number(realizedSummary.realized_pnl ?? (sessionStats && sessionStats.pnl_today) ?? currentTrading.realized_pnl ?? 0),
      daily: sessionStats
        ? {
            trades: sessionStats.trades ?? currentTrading.daily?.trades ?? 0,
            win: sessionStats.wins ?? currentTrading.daily?.win ?? 0,
            loss: sessionStats.losses ?? currentTrading.daily?.loss ?? 0,
            pnl: sessionStats.pnl_today ?? currentTrading.daily?.pnl ?? 0,
            drawdown_pct: sessionStats.drawdown_pct ?? currentTrading.daily?.drawdown_pct ?? 0,
          }
        : currentTrading.daily,
      risk: {
        circuit_breaker: strategyStatus
          ? !!strategyStatus.circuit_breaker
          : !!currentTrading.risk?.circuit_breaker,
        consecutive_loss: sessionStats && sessionStats.consecutive_loss !== undefined
          ? sessionStats.consecutive_loss
          : currentTrading.risk?.consecutive_loss,
      },
    });

    liveStore.setState(reduceLivePollOutcome(previous, {
      sources,
      attemptedAt,
      usableCount,
      dataPatch: {
        account,
        strategyStatus,
        sessionStats,
        loopStatus,
        riskSummary,
        realizedPnlSeries,
        trading: nextTrading,
      },
    }));
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
  void connectSocket();
  pollLoop({ force: true });
  pollTimer = setInterval(pollLoop, CONFIG.POLL_INTERVAL);
}

export function stopLiveRuntime() {
  started = false;
  if (socketTask) {
    socketTask.close();
    socketTask = null;
  }
  if (reconnectTimer !== null) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
  liveStore.setState(reduceLiveWsDisconnected(
    liveStore.getState(),
    Date.now(),
    'runtime_stopped',
  ));
}

export async function refreshLiveSnapshot(options = {}) {
  await pollLoop(options);
  return liveStore.getState();
}
