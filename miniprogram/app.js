import api from './utils/api';
import ws from './utils/ws';

App({
  globalData: {
    closedLoop: null,
    trading: {
      source: 'none', equity: 0, balance: 0, pnl_today: 0,
      position: { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
      daily: { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
      risk: { circuit_breaker: false, consecutive_loss: 0 },
      n_positions: 0, current_price: null,
    },
    lastUpdate: null,
    // 策略状态缓存（跨页面共享）
    strategyStatus: null,
  },

  _pollTimer: null,

  onLaunch() {
    this._tryAutoLogin();
  },

  async _tryAutoLogin() {
    const token = wx.getStorageSync('jwt_token');
    if (!token) return;
    api.loadToken();
    // v5: /api/state 目前 500，改用 /api/auth/me 验证 token
    const data = await api.get('/api/auth/me');
    if (data && (data.user || data.username)) {
      this.startChannels();
    }
  },

  startChannels() {
    if (this._pollTimer) return;

    ws.connect();
    ws.onMessage((data) => {
      this._applyState(data);
    });

    this._pollTimer = setInterval(() => this._poll(), 5000);
    this._poll();
  },

  _applyState(data) {
    if (!data) return;
    if (data.closed_loop) {
      this.globalData.closedLoop = data.closed_loop;
    }
    this.globalData.trading = {
      source: data.source || 'none',
      equity: data.equity || 0,
      balance: data.balance || 0,
      pnl_today: data.pnl_today || 0,
      position: data.position || { dir: 'FLAT', entry: 0, size: 0, unrealized: 0 },
      positions_list: data.positions_list || [],
      daily: data.daily || { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 },
      risk: data.risk || { circuit_breaker: false, consecutive_loss: 0 },
      n_positions: data.n_positions || 0,
      current_price: data.current_price,
    };
    this.globalData.lastUpdate = Date.now();

    const pages = getCurrentPages();
    pages.forEach(p => {
      if (p.onGlobalStateUpdate) p.onGlobalStateUpdate();
    });
  },

  async _poll() {
    // ═════════════════════════════════════════════════
    // v5 HTTP 兜底: 仅补充账户/统计/策略状态 (持仓由 WS 推送, 不轮询)
    // ═════════════════════════════════════════════════
    try {
      const [acct, strat, stats] = await Promise.all([
        api.get('/api/live/account'),
        api.get('/api/live/strategy-status'),
        api.get('/api/live/session-stats'),
      ]);

      const hasAcct = acct && acct.ok;
      const hasStrat = strat && typeof strat.running === 'boolean';
      const hasStats = stats && typeof stats.trades === 'number';

      if (!hasAcct && !hasStrat) return;

      const pos = (hasStrat && strat.position) || {};
      const src = hasStrat && strat.running ? 'live' : 'frozen';

      // 只更新 trading 数据，不覆盖 closed_loop (仅 WS 有)
      const prevTrading = this.globalData.trading || {};
      // 保留 WS 已有的持仓列表 (不靠轮询覆盖)
      const prevPositionsList = prevTrading.positions_list || [];
      const newTrading = {
        source: src,
        equity: hasAcct ? (acct.equity || prevTrading.equity || 0) : (prevTrading.equity || 0),
        balance: hasAcct ? (acct.balance || prevTrading.balance || 0) : (prevTrading.balance || 0),
        pnl_today: hasStats ? (stats.pnl_today || 0) : (prevTrading.pnl_today || 0),
        position: {
          dir: pos.dir || 'FLAT',
          entry: pos.entry || 0,
          size: pos.size || pos.volume || pos.api_volume || 0,
          unrealized: 0,
        },
        positions_list: prevPositionsList,
        daily: hasStats ? {
          trades: stats.trades || 0,
          win: stats.wins || 0,
          loss: stats.losses || 0,
          pnl: stats.pnl_today || 0,
          drawdown_pct: stats.drawdown_pct || 0,
        } : (prevTrading.daily || { trades: 0, win: 0, loss: 0, pnl: 0, drawdown_pct: 0 }),
        risk: {
          circuit_breaker: hasStrat ? !!strat.circuit_breaker : (prevTrading.risk && prevTrading.risk.circuit_breaker) || false,
          consecutive_loss: hasStats ? (stats.consecutive_loss || 0) : 0,
        },
        n_positions: prevPositionsList.length,
        current_price: prevTrading.current_price || null,
      };

      this.globalData.trading = newTrading;
      this.globalData.lastUpdate = Date.now();
      // 缓存策略状态供页面使用
      this.globalData.strategyStatus = hasStrat ? strat : null;

      const pages = getCurrentPages();
      pages.forEach(p => {
        if (p.onGlobalStateUpdate) p.onGlobalStateUpdate();
      });
    } catch (e) {
      // 静默失败，WS 主通道下次会更新
    }
  },
});
