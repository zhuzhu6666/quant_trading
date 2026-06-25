import { get } from './client';
import systemStore from '../stores/system';

export async function refreshOpsDomain() {
  const [scheduler, evolution, dbHealth, apiHealth, riskSummary, recentTradeTraces] = await Promise.all([
    get('/api/control/scheduler').catch(() => null),
    get('/api/control/evolution/latest').catch(() => null),
    get('/api/system/db-health').catch(() => null),
    get('/api/health').catch(() => null),
    get('/api/risk/summary').catch(() => null),
    get('/api/risk/trade-trace/recent?limit=12').catch(() => null),
  ]);
  systemStore.setState({
    scheduler: scheduler || null,
    evolution: evolution || null,
    dbHealth: dbHealth || null,
    apiHealth: apiHealth || null,
    riskSummary: riskSummary || null,
    recentTradeTraces: (recentTradeTraces && recentTradeTraces.items) || [],
    updatedAt: Date.now(),
  });
  return systemStore.getState();
}

export async function fetchTradeTrace({ positionId = '', decisionId = '' } = {}) {
  const params = [];
  if (positionId) params.push(`position_id=${encodeURIComponent(positionId)}`);
  if (decisionId) params.push(`decision_id=${encodeURIComponent(decisionId)}`);
  const query = params.length ? `?${params.join('&')}` : '';
  return get(`/api/risk/trade-trace${query}`);
}

export function stageTradeTraceQuery({ positionId = '', decisionId = '' } = {}) {
  systemStore.setState({
    pendingTradeTraceQuery: {
      positionId: String(positionId || ''),
      decisionId: String(decisionId || ''),
      ts: Date.now(),
    },
  });
}

export function consumePendingTradeTraceQuery() {
  const state = systemStore.getState();
  const pending = state.pendingTradeTraceQuery || null;
  if (pending) {
    systemStore.setState({ pendingTradeTraceQuery: null });
  }
  return pending;
}

export function rememberTradeTraceQuery({ positionId = '', decisionId = '' } = {}) {
  const normalized = {
    positionId: String(positionId || ''),
    decisionId: String(decisionId || ''),
    ts: Date.now(),
  };
  if (!normalized.positionId && !normalized.decisionId) return;
  const state = systemStore.getState();
  const current = Array.isArray(state.recentTradeTraceQueries) ? state.recentTradeTraceQueries : [];
  const deduped = current.filter(
    (item) => String(item.positionId || '') !== normalized.positionId
      || String(item.decisionId || '') !== normalized.decisionId
  );
  systemStore.setState({
    recentTradeTraceQueries: [normalized, ...deduped].slice(0, 8),
  });
}

export function openTradeTracePage({ positionId = '', decisionId = '' } = {}) {
  const normalized = {
    positionId: String(positionId || '').trim(),
    decisionId: String(decisionId || '').trim(),
  };
  if (!normalized.positionId && !normalized.decisionId) return false;
  stageTradeTraceQuery(normalized);
  wx.navigateTo({ url: '/pages/trade-trace/index' });
  return true;
}
