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

export function humanizeRiskAction(action) {
  const key = String(action || '').trim();
  const map = {
    open_trade: '开新仓',
    close_position: '平仓',
    update_weight: '更新权重',
    register_factor: '注册新因子',
    promote_factor: '晋升因子',
    start_shadow_model: '启动模型影子验证',
    start_canary_model: '启动模型金丝雀',
    signal: '信号评估',
    open: '已开仓',
    close: '已平仓',
    skip: '已拦截',
    order_failed: '下单失败',
    amend_failed: '保护价调整失败',
  };
  return map[key] || key || '--';
}

export function humanizeRiskReason(reason) {
  const key = String(reason || '').trim();
  const map = {
    ok: '风控已放行',
    loop_not_running: '实盘循环未运行，系统不允许开仓',
    bridge_disconnected: '交易桥接当前未连通，系统不允许开仓',
    data_lag: '行情数据延迟过高，系统暂停开仓',
    circuit_broken: '熔断已触发，系统暂停开仓',
    drawdown_too_high: '回撤过高，系统暂停开仓',
    consecutive_losses: '连续亏损过多，系统暂停开仓',
    daily_loss_limit: '日内亏损超限，系统暂停开仓',
    daily_trade_limit: '日内交易次数超限，系统暂停开仓',
    drawdown_approaching_limit: '回撤接近上限，先冻结权重调整',
    drawdown_too_high_for_promotion: '当前回撤偏高，先暂停因子晋升',
    drawdown_too_high_for_new_factor: '当前回撤偏高，先暂停注册新因子',
    live_trading_capability_not_allowed: '候选模型带有实盘能力，已被拒绝',
    candidate_status_not_allowed: '候选模型状态还没到这一步，已被拒绝',
    risk_reducing_action: '这是降风险动作，风控允许继续',
  };
  return map[key] || key || '--';
}
