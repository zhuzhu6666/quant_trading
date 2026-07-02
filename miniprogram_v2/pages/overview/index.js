import liveStore from '../../stores/live';
import sessionStore from '../../stores/session';
import { logout } from '../../services/auth';
import { refreshLiveSnapshot, startLiveRuntime } from '../../services/live';
import { formatDateTime, formatMoney, formatPct, formatPrice, toneFromPnl } from '../../utils/format';

const WEB_CONSOLE_URL = 'https://www.zhuzhu666.icu';

function isFiniteNumber(value) {
  const n = Number(value);
  return Number.isFinite(n);
}

function numberOrZero(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : 0;
}

function formatCurrency(value, currency = 'EUR') {
  if (!isFiniteNumber(value)) return '--';
  const symbol = currency === 'EUR' ? '€' : '';
  return `${symbol}${Number(value).toFixed(2)}`;
}

function formatUpdatedAt(value) {
  return value ? formatDateTime(value) : '--';
}

function normalizeLoop(loopStatus = {}, strategyStatus = {}) {
  const running = !!(loopStatus.running ?? strategyStatus.running);
  const broker = loopStatus.broker || strategyStatus.broker || 'ctrader';
  const strategy = loopStatus.strategy || strategyStatus.strategy || strategyStatus.strategy_name || '--';
  return {
    running,
    label: running ? '运行中' : '未运行',
    tone: running ? 'positive' : 'warning',
    broker,
    strategy,
  };
}

function normalizePositions(positions = []) {
  return positions.slice(0, 3).map((item, index) => {
    const direction = item.type === 'buy' || item.dir === 'LONG'
      ? '多'
      : item.type === 'sell' || item.dir === 'SHORT'
        ? '空'
        : '--';
    const pnl = numberOrZero(item.pnl ?? item.unrealized ?? item.netUnrealizedPnL);
    return {
      id: String(item.position_id || item.id || index),
      symbol: item.symbol || 'XAUUSD+',
      direction,
      volume: formatPrice(item.volume ?? item.size, 2),
      openPrice: formatPrice(item.open_price ?? item.price_open, 2),
      pnl: formatMoney(pnl),
      tone: toneFromPnl(pnl),
    };
  });
}

function buildViewModel(state = {}) {
  const trading = state.trading || {};
  const account = state.account || {};
  const sessionStats = state.sessionStats || {};
  const riskSummary = state.riskSummary || {};
  const strategyStatus = state.strategyStatus || {};
  const loopStatus = state.loopStatus || {};
  const positions = Array.isArray(trading.positions_list) ? trading.positions_list : [];
  const currency = account.currency || 'EUR';
  const equityValue = account.equity ?? trading.equity;
  const balanceValue = account.balance ?? trading.balance;
  const livePnlValue = trading.live_pnl ?? trading.unrealized_pnl ?? 0;
  const realizedPnlValue = trading.realized_pnl ?? sessionStats.pnl_today ?? 0;
  const drawdownPct = sessionStats.drawdown_pct ?? riskSummary.drawdown_pct ?? trading.daily?.drawdown_pct ?? 0;
  const consecutiveLoss = sessionStats.consecutive_loss ?? riskSummary.consecutive_loss ?? trading.risk?.consecutive_loss ?? 0;
  const circuitBroken = !!(strategyStatus.circuit_breaker ?? riskSummary.circuit_breaker ?? trading.risk?.circuit_breaker);
  const dbStatus = String(riskSummary.db_status || riskSummary.database_status || '').toLowerCase();
  const loop = normalizeLoop(loopStatus, strategyStatus);
  const wsFresh = !!state.wsConnected;
  const updatedAt = state.lastUpdate || Date.now();
  const riskOk = !circuitBroken && numberOrZero(consecutiveLoss) === 0;
  const overallTone = loop.running && riskOk ? 'positive' : circuitBroken ? 'negative' : 'warning';
  const overallLabel = overallTone === 'positive'
    ? '系统正常'
    : overallTone === 'negative'
      ? '风控阻断'
      : '需要关注';
  const overallText = loop.running
    ? `交易循环正在运行，当前 ${positions.length} 笔持仓。`
    : `交易循环未运行，完整操作请到 Web 控制台处理。`;

  return {
    wsLabel: wsFresh ? '实时在线' : '轮询中',
    wsTone: wsFresh ? 'positive' : 'warning',
    overallTone,
    overallLabel,
    overallText,
    loopLabel: loop.label,
    loopTone: loop.tone,
    broker: loop.broker,
    strategy: loop.strategy,
    equity: formatCurrency(equityValue, currency),
    balance: formatCurrency(balanceValue, currency),
    livePnl: formatMoney(livePnlValue),
    livePnlTone: toneFromPnl(livePnlValue),
    realizedPnl: formatMoney(realizedPnlValue),
    realizedPnlTone: toneFromPnl(realizedPnlValue),
    positions: String(positions.length),
    positionSummary: trading.position_summary?.label || (positions.length ? `${positions.length} 笔持仓` : '当前无持仓'),
    xauPrice: formatPrice(trading.current_price, 2),
    xauVisible: numberOrZero(trading.current_price) > 0,
    drawdown: formatPct(drawdownPct),
    consecutiveLoss: String(numberOrZero(consecutiveLoss)),
    circuitLabel: circuitBroken ? '已触发' : '未触发',
    circuitTone: circuitBroken ? 'negative' : 'positive',
    dbLabel: dbStatus ? (dbStatus === 'ok' || dbStatus === 'healthy' ? '健康' : dbStatus) : '以 Web 为准',
    updatedAt: formatUpdatedAt(updatedAt),
    positionsList: normalizePositions(positions),
    hasPositions: positions.length > 0,
    webUrl: WEB_CONSOLE_URL,
  };
}

Page({
  data: {
    refreshing: false,
    wsLabel: '连接中',
    wsTone: 'warning',
    overallTone: 'neutral',
    overallLabel: '读取中',
    overallText: '正在连接后端状态。',
    loopLabel: '--',
    loopTone: 'neutral',
    broker: '--',
    strategy: '--',
    equity: '--',
    balance: '--',
    livePnl: '--',
    livePnlTone: 'neutral',
    realizedPnl: '--',
    realizedPnlTone: 'neutral',
    positions: '0',
    positionSummary: '当前无持仓',
    xauPrice: '--',
    xauVisible: false,
    drawdown: '--',
    consecutiveLoss: '0',
    circuitLabel: '--',
    circuitTone: 'neutral',
    dbLabel: '--',
    updatedAt: '--',
    positionsList: [],
    hasPositions: false,
    webUrl: WEB_CONSOLE_URL,
  },

  onLoad() {
    this._unsub = liveStore.subscribe((state) => {
      this.setData(buildViewModel(state));
    });
    this.setData(buildViewModel(liveStore.getState()));
  },

  onShow() {
    this.ensureActive();
  },

  onUnload() {
    if (this._unsub) this._unsub();
  },

  onPullDownRefresh() {
    this.refresh(true).finally(() => wx.stopPullDownRefresh());
  },

  ensureActive() {
    const token = wx.getStorageSync('jwt_token') || '';
    const authed = sessionStore.getState().isAuthenticated || !!token;
    if (!authed) {
      wx.reLaunch({ url: '/pages/login/index' });
      return false;
    }
    startLiveRuntime();
    this.refresh(false);
    return true;
  },

  async refresh(showLoading = true) {
    if (this.data.refreshing) return;
    if (showLoading) this.setData({ refreshing: true });
    try {
      const state = await refreshLiveSnapshot({ force: true });
      this.setData(buildViewModel(state));
    } catch (err) {
      wx.showToast({ title: '刷新失败', icon: 'none' });
    } finally {
      if (showLoading) this.setData({ refreshing: false });
    }
  },

  onRefreshTap() {
    this.refresh(true);
  },

  onCopyWebUrl() {
    wx.setClipboardData({
      data: WEB_CONSOLE_URL,
      success: () => wx.showToast({ title: '已复制 Web 地址', icon: 'success' }),
    });
  },

  onLogout() {
    const app = getApp();
    if (app && app.beforeLogout) app.beforeLogout();
    logout();
    wx.reLaunch({ url: '/pages/login/index' });
  },
});
