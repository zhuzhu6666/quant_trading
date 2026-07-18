export function sourceState(state = {}, name = '') {
  return String(state.sources?.[name]?.state || 'unknown').toLowerCase();
}

export function sourceUsable(state = {}, name = '') {
  return ['known', 'stale'].includes(sourceState(state, name));
}

export function isRiskFactKnown(state = {}) {
  return sourceState(state, 'risk') === 'known';
}

function epochSeconds(value) {
  const numeric = Number(value);
  if (Number.isFinite(numeric) && numeric > 0) return numeric > 1e12 ? numeric / 1000 : numeric;
  const parsed = typeof value === 'string' ? Date.parse(value) : NaN;
  return Number.isFinite(parsed) && parsed > 0 ? parsed / 1000 : 0;
}

export function metricFactPresentation(value, fact = {}, lastKnown = null, now = Date.now()) {
  let state = String(fact.state || 'unknown').toLowerCase();
  if (state === 'known') {
    const observedAt = epochSeconds(fact.observedAt);
    const staleAfterSec = Number(fact.staleAfterSec || 15);
    const nowSec = epochSeconds(now);
    if (observedAt <= 0) state = 'unknown';
    else if (staleAfterSec > 0 && nowSec - observedAt > staleAfterSec) state = 'stale';
  }
  const direct = value === null || value === undefined || value === '' ? undefined : Number(value);
  const last = lastKnown && lastKnown.value !== null && lastKnown.value !== undefined
    ? Number(lastKnown.value)
    : undefined;
  const directUsable = Number.isFinite(direct);
  const lastUsable = Number.isFinite(last);
  if (state === 'known' && directUsable) {
    return {
      state,
      value: direct,
      tone: direct > 0 ? 'positive' : direct < 0 ? 'negative' : 'neutral',
      observedAt: fact.observedAt || 0,
      label: '',
    };
  }
  if (state === 'stale' && (directUsable || lastUsable)) {
    return {
      state,
      value: directUsable ? direct : last,
      tone: 'warning',
      observedAt: fact.observedAt || lastKnown?.observedAt || 0,
      label: '已过期',
    };
  }
  return {
    state: state === 'error' ? 'error' : 'unknown',
    value: undefined,
    tone: 'warning',
    observedAt: fact.observedAt || 0,
    label: state === 'error' ? '读取失败' : '未知',
  };
}
