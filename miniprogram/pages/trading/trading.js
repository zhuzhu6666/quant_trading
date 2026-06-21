const app = getApp();

Page({
  data: {
    connected: false,
    connLabel: '等待数据',
    source: '',
    // 账户
    equity: '—', balance: '—', pnl: '—', pnlCls: 'text-gray',
    margin: '—', marginFree: '—', leverage: '—',
    // 持仓
    hasPos: false, posDir: '—', posDirCls: 'text-gray',
    posEntry: '—', posSize: '—', posPnl: '—', posPnlCls: 'text-gray',
    // 统计
    trades: 0, wins: 0, losses: 0,
    winRate: '—', winRateCls: 'text-gray', winRateBar: 0, winRateBarCls: 'progress-green',
    drawdown: '—',
    price: '—',
    // 风控
    circuitBreaker: false, consecLoss: 0,
  },

  onLoad() { this._update(); },
  onShow() { this._update(); },
  onGlobalStateUpdate() { this._update(); },

  _update() {
    const g = app.globalData;
    const t = g.trading || {};
    const pos = t.position || {};
    const daily = t.daily || {};
    const risk = t.risk || {};

    const pnl = daily.pnl || 0;
    const trades = daily.trades || 0;
    const wins = daily.win || 0;
    const losses = daily.loss || 0;
    const wr = trades > 0 ? (wins / trades * 100) : 0;
    const dd = daily.drawdown_pct || 0;
    const hasPos = t.n_positions > 0;
    const connected = !!(t.source && t.source !== 'none');

    let connLabel = '等待数据';
    if (t.source === 'live') connLabel = 'cTrader 实盘 · 实时';
    else if (t.source === 'frozen') connLabel = '数据冻结 · 已停止';
    else if (t.source === 'none') connLabel = '等待连接';

    this.setData({
      connected,
      connLabel,
      source: t.source || 'none',
      // 账户
      equity: (t.equity || 0) > 0 ? Number(t.equity).toFixed(2) : '—',
      balance: (t.balance || 0) > 0 ? Number(t.balance).toFixed(2) : '—',
      pnl: (pnl >= 0 ? '+' : '') + pnl.toFixed(2),
      pnlCls: pnl > 0 ? 'text-green' : pnl < 0 ? 'text-red' : 'text-gray',
      margin: (risk.margin != null ? Number(risk.margin).toFixed(2) : '—'),
      marginFree: (risk.margin_free != null ? Number(risk.margin_free).toFixed(2) : '—'),
      leverage: t.leverage || '—',
      // 持仓
      hasPos,
      posDir: hasPos ? (pos.dir === 'LONG' ? '多头' : '空头') : '空仓',
      posDirCls: hasPos ? (pos.dir === 'LONG' ? 'text-green' : 'text-red') : 'text-gray',
      posEntry: hasPos && pos.entry ? Number(pos.entry).toFixed(2) : '—',
      posSize: hasPos && pos.size ? Number(pos.size).toFixed(2) : '—',
      posPnl: hasPos ? ((pos.unrealized >= 0 ? '+' : '') + Number(pos.unrealized).toFixed(2)) : '—',
      posPnlCls: hasPos ? (pos.unrealized > 0 ? 'text-green' : pos.unrealized < 0 ? 'text-red' : 'text-gray') : 'text-gray',
      // 统计
      trades, wins, losses,
      winRate: trades > 0 ? wr.toFixed(1) + '%' : '—',
      winRateCls: trades > 0 ? (wr >= 50 ? 'text-green' : 'text-red') : 'text-gray',
      winRateBar: wr,
      winRateBarCls: wr >= 50 ? 'progress-green' : 'progress-red',
      drawdown: dd > 0 ? dd.toFixed(2) + '%' : '0%',
      price: t.current_price ? Number(t.current_price).toFixed(2) : '—',
      // 风控
      circuitBreaker: !!risk.circuit_breaker,
      consecLoss: risk.consecutive_loss || 0,
    });
  },
});
