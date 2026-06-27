import { get } from './client';
import systemStore from '../stores/system';
import opsStore from '../stores/ops';
import { buildBackendReadinessView } from '../utils/backendReadiness';

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

const BACKEND_READINESS_TTL = 30000;
let backendReadinessRefreshInFlight = null;
let lastBackendReadinessRefreshAt = 0;

function normalizeBackendReadinessState(payload = null) {
  const raw = payload && typeof payload === 'object' ? payload : null;
  const readinessView = buildBackendReadinessView(raw || {});
  return {
    raw,
    view: readinessView,
  };
}

export async function fetchBackendReadiness() {
  return get('/api/ops/backend-readiness');
}

async function fetchBackendReadinessState() {
  opsStore.setState({
    backendReadinessStatus: 'loading',
    backendReadinessError: '',
  });
  let status = 'ok';
  let error = '';
  let payload = null;
  try {
    payload = await fetchBackendReadiness();
  } catch (err) {
    status = 'error';
    error = String((err && err.errMsg) || (err && err.message) || '后端 readiness 拉取失败');
  }
  const state = normalizeBackendReadinessState(payload);
  opsStore.setState({
    backendReadiness: state.raw,
    backendReadinessView: state.view,
    backendReadinessStatus: status,
    backendReadinessError: error,
    updatedAt: Date.now(),
  });
  return opsStore.getState();
}

export async function refreshBackendReadiness(options = {}) {
  const force = !!options.force;
  const now = Date.now();
  if (!force && backendReadinessRefreshInFlight) {
    return backendReadinessRefreshInFlight;
  }
  if (!force && lastBackendReadinessRefreshAt > 0 && (now - lastBackendReadinessRefreshAt) < BACKEND_READINESS_TTL) {
    return opsStore.getState();
  }

  backendReadinessRefreshInFlight = (async () => {
    try {
      const nextState = await fetchBackendReadinessState();
      lastBackendReadinessRefreshAt = Date.now();
      return nextState;
    } finally {
      backendReadinessRefreshInFlight = null;
    }
  })();
  return backendReadinessRefreshInFlight;
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
