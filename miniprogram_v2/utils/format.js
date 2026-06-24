export function formatMoney(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? '+' : '';
  return sign + n.toFixed(2);
}

export function formatPct(value) {
  const n = Number(value || 0);
  const sign = n > 0 ? '+' : '';
  return sign + n.toFixed(2) + '%';
}

export function formatPrice(value, digits = 3) {
  if (value === null || value === undefined || value === '') return '--';
  const n = Number(value);
  if (!Number.isFinite(n)) return '--';
  return n.toFixed(digits).replace(/\.?0+$/, '');
}

export function formatCount(value) {
  return String(Number(value || 0));
}

export function formatTime(ts) {
  if (!ts) return '--';
  const d = new Date(ts * (ts < 10_000_000_000 ? 1000 : 1));
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return hh + ':' + mm;
}

export function formatDateTime(ts) {
  if (!ts) return '--';
  const d = new Date(ts * (ts < 10_000_000_000 ? 1000 : 1));
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, '0');
  const day = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  return `${y}-${m}-${day} ${hh}:${mm}`;
}

export function toneFromPnl(value) {
  const n = Number(value || 0);
  if (n > 0) return 'positive';
  if (n < 0) return 'negative';
  return 'neutral';
}

export function toneFromStatus(status) {
  const s = String(status || '').toLowerCase();
  if (['approved', 'ok', 'healthy', 'running', 'connected', 'good_win', 'active', 'effective', 'reinforced'].includes(s)) return 'positive';
  if (['rejected', 'rolled_back', 'error', 'critical', 'bad_loss', 'offline', 'ineffective'].includes(s)) return 'negative';
  if (['proposed', 'warning', 'watch', 'stale', 'observing', 'mixed'].includes(s)) return 'warning';
  return 'neutral';
}
