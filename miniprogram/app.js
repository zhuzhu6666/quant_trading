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
  },

  _pollTimer: null,

  onLaunch() {
    this._tryAutoLogin();
  },

  async _tryAutoLogin() {
    const token = wx.getStorageSync('jwt_token');
    if (!token) return; // 无 token，等用户手动登录

    api.loadToken();
    // 用 /api/state 验证 token 有效性
    const data = await api.get('/api/state');
    if (data) {
      // token 有效，跳过登录页
      this.startChannels();
    }
    // token 无效 → 静默失败，登录页会显示
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
    const data = await api.get('/api/state');
    if (data) this._applyState(data);
  },
});
