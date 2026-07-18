import liveStore from '../../stores/live';
import sessionStore from '../../stores/session';
import { logout } from '../../services/auth';
import { refreshLiveSnapshot, startLiveRuntime } from '../../services/live';
import { formatDateTime, formatMoney, formatPct, formatPrice, toneFromPnl } from '../../utils/format';
import {
  isRiskFactKnown,
  metricFactPresentation,
  sourceState,
  sourceUsable,
} from '../../stores/liveViewFacts';
import {
  positionComponentStateAt,
  positionComponentsAllKnown,
} from '../../stores/livePositionFacts';

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

function factHint(label, observedAt) {
  const suffix = observedAt ? ` · ${formatDateTime(observedAt)}` : '';
  return `${label}${suffix}`;
}

function metricText(presentation, formatter) {
  return presentation.value === undefined ? '--' : formatter(presentation.value);
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
    const pnlFact = item.position_facts?.pnl || { state: 'unknown' };
    const priceFact = item.position_facts?.price || { state: 'unknown' };
    const protectionFact = item.position_facts?.protection || { state: 'unknown' };
    const pnl = metricFactPresentation(item.pnl, pnlFact, item.pnl_last_known);
    const price = metricFactPresentation(
      item.current_price,
      priceFact,
      item.current_price_last_known,
    );
    const protectionKnown = ['known', 'stale'].includes(String(protectionFact.state || ''));
    const sl = formatPrice(item.sl ?? item.stop_loss, 2);
    const tp = formatPrice(item.tp ?? item.take_profit, 2);
    const protectionLabel = protectionKnown
      ? `SL ${sl} · TP ${tp}`
      : protectionFact.state === 'error' ? '保护价读取失败' : '保护价未知';
    const protectionHint = protectionFact.state === 'stale'
      ? factHint('保护价已过期', protectionFact.observedAt)
      : '';
    return {
      id: String(item.position_id || item.id || index),
      symbol: item.symbol || 'XAUUSD+',
      direction,
      volume: formatPrice(item.volume ?? item.size, 2),
      openPrice: formatPrice(item.open_price ?? item.price_open, 2),
      protection: protectionLabel,
      protectionHint,
      price: metricText(price, (value) => formatPrice(value, 2)),
      priceHint: price.label ? factHint(`现价${price.label}`, price.observedAt) : '',
      pnl: metricText(pnl, formatMoney),
      pnlHint: pnl.label ? factHint(`盈亏${pnl.label}`, pnl.observedAt) : '',
      tone: pnl.tone,
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
  const stateFactUsable = sourceUsable(state, 'state');
  const accountUsable = sourceUsable(state, 'account') || stateFactUsable;
  const loopKnown = sourceState(state, 'loop') === 'known' || sourceState(state, 'state') === 'known';
  const riskKnown = isRiskFactKnown(state);
  const viewNow = Date.now();
  const identityState = positionComponentStateAt(trading, 'identity', viewNow);
  const positionsUsable = ['known', 'stale'].includes(identityState);
  const positionsKnown = identityState === 'known' && positionComponentsAllKnown(trading, viewNow);
  const equityValue = accountUsable ? (account.equity ?? trading.equity) : undefined;
  const balanceValue = accountUsable ? (account.balance ?? trading.balance) : undefined;
  const livePnlFact = {
    state: trading.unrealized_pnl_state || positionComponentStateAt(trading, 'pnl', viewNow),
    observedAt: trading.unrealized_pnl_observed_at || trading.position_components?.pnl?.observedAt || 0,
    staleAfterSec: trading.position_components?.pnl?.staleAfterSec || 15,
  };
  const livePnl = metricFactPresentation(
    trading.live_pnl ?? trading.unrealized_pnl,
    livePnlFact,
    trading.unrealized_pnl_last_known,
    viewNow,
  );
  const currentPrice = metricFactPresentation(
    trading.current_price,
    {
      state: trading.current_price_state || 'unknown',
      observedAt: trading.current_price_observed_at || 0,
      staleAfterSec: trading.position_components?.price?.staleAfterSec || 15,
    },
    trading.current_price_last_known,
    viewNow,
  );
  const realizedPnlValue = trading.realized_pnl ?? sessionStats.pnl_today ?? 0;
  const drawdownPct = sessionStats.drawdown_pct ?? riskSummary.drawdown_pct ?? trading.daily?.drawdown_pct ?? 0;
  const consecutiveLoss = sessionStats.consecutive_loss ?? riskSummary.consecutive_loss ?? trading.risk?.consecutive_loss ?? 0;
  const circuitBroken = !!(strategyStatus.circuit_breaker ?? riskSummary.circuit_breaker ?? trading.risk?.circuit_breaker);
  const dbStatus = String(riskSummary.db_status || riskSummary.database_status || '').toLowerCase();
  const loop = normalizeLoop(loopStatus, strategyStatus);
  const wsFresh = !!state.wsConnected && sourceState(state, 'state') === 'known';
  const updatedAt = state.lastSuccessAt || 0;
  const riskOk = !circuitBroken && numberOrZero(consecutiveLoss) === 0;
  const factsKnown = loopKnown && riskKnown && accountUsable && positionsKnown;
  const overallTone = factsKnown && loop.running && riskOk ? 'positive' : circuitBroken ? 'negative' : 'warning';
  const overallLabel = overallTone === 'positive'
    ? '系统正常'
    : overallTone === 'negative'
      ? '风控阻断'
      : factsKnown ? '需要关注' : '状态未确认';
  const overallText = !factsKnown
    ? '部分运行事实未知或已过期，请以最后成功时间和 Web 控制台为准。'
    : loop.running
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
    livePnl: metricText(livePnl, formatMoney),
    livePnlTone: livePnl.tone,
    livePnlHint: livePnl.label
      ? factHint(`持仓盈亏${livePnl.label}`, livePnl.observedAt)
      : positions.length ? '按经纪商仓位计算' : '当前无持仓',
    realizedPnl: formatMoney(realizedPnlValue),
    realizedPnlTone: toneFromPnl(realizedPnlValue),
    positions: positionsUsable ? String(positions.length) : '--',
    positionSummary: identityState === 'known'
      ? (trading.position_summary?.label || (positions.length ? `${positions.length} 笔持仓` : '当前无持仓'))
      : identityState === 'stale'
        ? factHint('持仓快照已过期', trading.positions_identity_observed_at)
        : identityState === 'error' ? '持仓读取失败，保留最后快照' : '持仓状态未知',
    xauPrice: metricText(currentPrice, (value) => formatPrice(value, 2)),
    xauHint: currentPrice.label ? factHint(`仓位现价${currentPrice.label}`, currentPrice.observedAt) : '',
    xauVisible: positions.length > 0 && positionsUsable,
    drawdown: formatPct(drawdownPct),
    consecutiveLoss: String(numberOrZero(consecutiveLoss)),
    circuitLabel: riskKnown ? (circuitBroken ? '已触发' : '未触发') : '未知',
    circuitTone: riskKnown ? (circuitBroken ? 'negative' : 'positive') : 'warning',
    dbLabel: sourceUsable(state, 'risk') && dbStatus
      ? (dbStatus === 'ok' || dbStatus === 'healthy' ? '健康' : dbStatus)
      : '未知',
    updatedAt: formatUpdatedAt(updatedAt),
    positionsList: normalizePositions(positions),
    hasPositions: positions.length > 0,
    positionsFactWarning: identityState === 'known'
      ? ''
      : identityState === 'stale'
        ? factHint('以下为最后持仓快照', trading.positions_identity_observed_at)
        : '以下为最后可信快照，当前持仓身份未确认',
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
    livePnlHint: '持仓盈亏未知',
    realizedPnl: '--',
    realizedPnlTone: 'neutral',
    positions: '--',
    positionSummary: '持仓状态未知',
    xauPrice: '--',
    xauHint: '',
    xauVisible: false,
    drawdown: '--',
    consecutiveLoss: '0',
    circuitLabel: '--',
    circuitTone: 'neutral',
    dbLabel: '--',
    updatedAt: '--',
    positionsList: [],
    hasPositions: false,
    positionsFactWarning: '',
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
