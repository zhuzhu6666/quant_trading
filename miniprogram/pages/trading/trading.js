import api from '../../utils/api';

const app = getApp();

Page({
  data: {
    connected: false,
    connLabel: '等待数据',
    source: '',
    // 账户 — 直接来自 /api/live/account
    equity: '—', balance: '—', pnl: '—', pnlCls: 'text-gray',
    margin: '—', marginFree: '—', leverage: '—',
    currency: '',
    // 持仓 — 直接来自 /api/live/positions (支持多笔)
    positions: [],
    // 统计 — 来自 global state (每日会话)
    trades: 0, wins: 0, losses: 0,
    winRate: '—', winRateCls: 'text-gray', winRateBar: 0, winRateBarCls: 'progress-green',
    drawdown: '—',
    price: '—',
    // 风控 — 来自 global state
    circuitBreaker: false, consecLoss: 0,
  },

  _actTimer: null,

  onLoad() {
    this._fetchAccount();
    this._update();
    this._actTimer = setInterval(() => this._fetchAccount(), 15000);
  },

  onShow() {
    this._fetchAccount();
    this._update();
  },

  onHide() {
    if (this._actTimer) { clearInterval(this._actTimer); this._actTimer = null; }
  },

  onUnload() {
    if (this._actTimer) { clearInterval(this._actTimer); this._actTimer = null; }
  },

  onGlobalStateUpdate() { this._update(); },

  async _fetchAccount() {
    const [acct, pos] = await Promise.all([
      api.get('/api/live/account'),
      api.get('/api/live/positions'),
    ]);

    const hasAcct = acct && acct.ok;
    const hasPos = pos && (pos.positions || pos.ok);

    this.setData({
      equity: hasAcct && acct.equity ? Number(acct.equity).toFixed(2) : this.data.equity,
      balance: hasAcct && acct.balance ? Number(acct.balance).toFixed(2) : this.data.balance,
      margin: hasAcct && acct.margin ? Number(acct.margin).toFixed(2) : '—',
      marginFree: hasAcct && acct.margin_free ? Number(acct.margin_free).toFixed(2) : '—',
      leverage: hasAcct && acct.leverage ? acct.leverage : '—',
      currency: hasAcct && acct.currency ? acct.currency : '',
    });

    // 持仓解析 — 支持多笔
    if (hasPos) {
      var plist = pos.positions || [];
      if (plist.length > 0) {
        var list = [];
        for (var i = 0; i < plist.length; i++) {
          var p = plist[i];
          var dir = (p.type === 'buy' || p.direction === 'LONG' || p.tradeSide === 'BUY') ? 'LONG' : 'SHORT';
          var entry = p.price_open || p.openPrice || 0;
          var size = p.volume || p.size || 0;
          var upl = p.profit || p.unrealizedPnl || 0;
          var sym = p.symbol || p.symbolName || '';
          list.push({
            dir: dir === 'LONG' ? '多头' : '空头',
            dirCls: dir === 'LONG' ? 'text-green' : 'text-red',
            entry: entry ? Number(entry).toFixed(2) : '—',
            size: size ? Number(size).toFixed(2) : '—',
            pnl: (upl >= 0 ? '+' : '') + Number(upl).toFixed(2),
            pnlCls: upl > 0 ? 'text-green' : upl < 0 ? 'text-red' : 'text-gray',
            symbol: sym,
          });
        }
        this.setData({ positions: list });
      } else {
        this.setData({ positions: [] });
      }
    }
  },

  _update() {
    const g = app.globalData;
    const t = g.trading || {};
    const daily = t.daily || {};
    const risk = t.risk || {};

    const pnl = daily.pnl || 0;
    const trades = daily.trades || 0;
    const wins = daily.win || 0;
    const losses = daily.loss || 0;
    const wr = trades > 0 ? (wins / trades * 100) : 0;
    const dd = daily.drawdown_pct || 0;
    const connected = !!(t.source && t.source !== 'none');

    let connLabel = '等待数据';
    if (t.source === 'live') connLabel = 'cTrader 实盘 · 实时';
    else if (t.source === 'frozen') connLabel = '数据冻结 · 已停止';
    else if (t.source === 'none') connLabel = '等待连接';

    // source 和 pipeline 状态不变（来自 global state 的 closed_loop / source）
    // 账户和持仓数据由 _fetchAccount() 独立管理，这里只更新统计和风控
    this.setData({
      connected,
      connLabel,
      source: t.source || 'none',
      pnl: (pnl >= 0 ? '+' : '') + pnl.toFixed(2),
      pnlCls: pnl > 0 ? 'text-green' : pnl < 0 ? 'text-red' : 'text-gray',
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
