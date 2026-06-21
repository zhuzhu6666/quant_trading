import api from '../../utils/api';

const app = getApp();

Page({
  data: {
    connected: false,
    connLabel: '等待数据',
    source: '',
    // 账户 — WS 实时推送, HTTP 兜底
    equity: '—', balance: '—', pnl: '—', pnlCls: 'text-gray',
    margin: '—', marginFree: '—', leverage: '—',
    currency: '',
    // 持仓 — WS 实时推送 / HTTP 兜底
    positions: [],
    // 统计
    trades: 0, wins: 0, losses: 0,
    winRate: '—', winRateCls: 'text-gray', winRateBar: 0, winRateBarCls: 'progress-green',
    drawdown: '—',
    price: '—',
    // 风控
    circuitBreaker: false, consecLoss: 0,
  },

  _fallbackTimer: null,

  onLoad() {
    this._update();
    this._fallbackTimer = setInterval(() => this._fallbackFetch(), 60000);
  },

  onShow() {
    this._update();
  },

  onHide() {
    this._clearFallback();
  },

  onUnload() {
    this._clearFallback();
  },

  _clearFallback() {
    if (this._fallbackTimer) { clearInterval(this._fallbackTimer); this._fallbackTimer = null; }
  },

  onGlobalStateUpdate() { this._update(); },

  // ── 主数据源: WS 推送 → global state → 实时更新 ──
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
    const connected = !!(t.source && t.source !== 'none');

    let connLabel = '等待数据';
    if (t.source === 'live') connLabel = 'cTrader 实盘 · 实时';
    else if (t.source === 'frozen') connLabel = '数据冻结 · 已停止';
    else if (t.source === 'none') connLabel = '等待连接';

    // 账户 — 来自 WS (global state)
    const eq = t.equity || 0;
    const bal = t.balance || 0;

    // 持仓 — 来自 WS (单笔) 或 HTTP 兜底 (多笔)
    const hasPos = t.n_positions > 0;

    this.setData({
      connected, connLabel, source: t.source || 'none',
      // 账户 (WS 实时)
      equity: eq > 0 ? Number(eq).toFixed(2) : '—',
      balance: bal > 0 ? Number(bal).toFixed(2) : '—',
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

    // 如果 WS 没数据, 触发 HTTP 兜底
    if (!eq && !bal) {
      this._fallbackFetch();
    }
  },

  // ── HTTP 兜底: WS 断开时从 /api/live/account + positions 补数据 ──
  async _fallbackFetch() {
    try {
      const [acct, pos] = await Promise.all([
        api.get('/api/live/account'),
        api.get('/api/live/positions'),
      ]);

      const hasAcct = acct && acct.ok;

      // 只在 WS 没推送时才覆盖账户数据
      this.setData({
        equity: (hasAcct && acct.equity && !this.data.equity || this.data.equity === '—')
          ? Number(acct.equity).toFixed(2) : this.data.equity,
        balance: (hasAcct && acct.balance && !this.data.balance || this.data.balance === '—')
          ? Number(acct.balance).toFixed(2) : this.data.balance,
        margin: hasAcct && acct.margin ? Number(acct.margin).toFixed(2) : '—',
        marginFree: hasAcct && acct.margin_free ? Number(acct.margin_free).toFixed(2) : '—',
        leverage: hasAcct && acct.leverage ? acct.leverage : '—',
        currency: hasAcct && acct.currency ? acct.currency : '',
      });

      // 补持仓 (WS 只传单笔, HTTP 有多笔)
      const hasPos = pos && (pos.positions || pos.ok);
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
        }
      }
    } catch (e) {
      // 静默失败, WS 主通道下次会更新
    }
  },
});
