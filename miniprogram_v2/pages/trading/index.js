import liveStore from '../../stores/live';
import { emergencyCloseAll, refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { formatMoney, formatPct, formatPrice, humanizeRiskAction, humanizeRiskReason } from '../../utils/format';

function normalizePosition(item = {}) {
  const pnl = Number(item.pnl ?? item.netUnrealizedPnL ?? item.unrealized ?? item.profit ?? 0);
  const currentPrice = item.current_price ?? item.price_current ?? 0;
  const openPrice = item.open_price ?? item.price_open ?? 0;
  return {
    ...item,
    pnlValue: pnl,
    pnlText: formatMoney(pnl),
    currentPriceText: formatPrice(currentPrice, 3),
    openPriceText: formatPrice(openPrice, 3),
    volumeText: String(item.volume ?? item.size ?? '--'),
    directionText: item.type === 'buy' ? 'LONG' : item.type === 'sell' ? 'SHORT' : (item.direction || '--'),
    pnlToneClass: pnl >= 0 ? 'accent-pos' : 'accent-neg',
  };
}

Page({
  data: {
    signalLabel: '无信号',
    signalTone: 'neutral',
    gateLabel: '等待信号',
    gateTone: 'neutral',
    strategy: {},
    positions: [],
    daily: {},
    risk: {},
    currentPrice: '--',
    loopRunning: false,
    startBusy: false,
    stopBusy: false,
    emergencyBusy: false,
    policyView: null,
  },

  onLoad() {
    this._unsub = liveStore.subscribe(() => this.syncView());
    this.syncView();
    refreshLiveSnapshot();
  },

  onShow() {
    refreshLiveSnapshot();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = liveStore.getState();
    const trading = state.trading || {};
    const positions = (trading.positions_list || []).map(normalizePosition);
    const strategy = state.strategyStatus || {};
    const loopStatus = state.loopStatus || {};
    const riskSummary = state.riskSummary || {};
    const v4Status = strategy.v4_status || {};
    const recentSignal = strategy.recent_signal || strategy.signal || {};
    const direction = recentSignal.direction || strategy.direction || trading.position.dir || 'FLAT';
    const positionDir = trading.position && trading.position.dir;
    const gateReason = strategy.gate_reason || '';
    const circuitBreaker = !!strategy.circuit_breaker;
    const pipelineRunning = !!(
      strategy.pipeline_running ||
      strategy.running ||
      v4Status.pipeline_active ||
      loopStatus.running
    );
    let gateLabel = '等待信号';
    let gateTone = 'neutral';
    if (circuitBreaker) {
      gateLabel = '风控熔断中';
      gateTone = 'negative';
    } else if (gateReason) {
      gateLabel = '信号被闸门拦截';
      gateTone = 'warning';
    } else if (positionDir) {
      gateLabel = '策略已持仓';
      gateTone = 'positive';
    } else if (direction === 1 || direction === 'LONG' || direction === -1 || direction === 'SHORT') {
      gateLabel = '信号可执行';
      gateTone = 'positive';
    }
    const realizedDailyPnl = Number((trading.daily && trading.daily.pnl) || 0);
    const unrealizedPnl = positions.reduce((sum, item) => sum + Number(item.pnlValue || 0), 0);
    const livePnl = realizedDailyPnl + unrealizedPnl;
    const balance = Number(trading.balance || 0);
    const equity = Number(trading.equity || 0);
    const equityDrawdownPct = balance > 0 ? Math.max(0, ((balance - equity) / balance) * 100) : 0;
    const sessionDrawdownPct = Number((trading.daily && trading.daily.drawdown_pct) || 0);
    const liveDrawdownPct = Math.max(sessionDrawdownPct, equityDrawdownPct);
    const policy = riskSummary.policy || {};
    const latestVerdict = Array.isArray(policy.items) ? policy.items[0] : null;
    this.setData({
      signalLabel: direction === 1 || direction === 'LONG' ? '偏多' : direction === -1 || direction === 'SHORT' ? '偏空' : '观望',
      signalTone: direction === 1 || direction === 'LONG' ? 'positive' : direction === -1 || direction === 'SHORT' ? 'negative' : 'neutral',
      gateLabel,
      gateTone,
      strategy: {
        pipelineRunning,
        positionDir,
        entry: trading.position && trading.position.entry,
        size: trading.position && trading.position.size,
        gateReason,
        circuitBreaker,
      },
      positions,
      loopRunning: !!loopStatus.running,
      daily: {
        pnl: formatMoney(livePnl),
        drawdown: formatPct(liveDrawdownPct),
        trades: trading.daily && trading.daily.trades,
      },
      risk: trading.risk || {},
      currentPrice: formatPrice(trading.current_price, 3),
      policyView: latestVerdict
        ? {
            action: humanizeRiskAction(latestVerdict.action || latestVerdict.event_type || '--'),
            reason: humanizeRiskReason(latestVerdict.reason || '--'),
            tone: latestVerdict.allowed ? 'positive' : 'negative',
            blocked: Number((policy.counts && policy.counts.blocked) || 0),
            allowed: Number((policy.counts && policy.counts.allowed) || 0),
          }
        : null,
    });
  },

  async onStart() {
    if (this.data.loopRunning || this.data.startBusy || this.data.stopBusy) return;
    this.setData({ startBusy: true });
    try {
      await startTradingLoop();
      await refreshLiveSnapshot({ force: true });
    } finally {
      this.setData({ startBusy: false });
    }
  },

  async onStop() {
    if (!this.data.loopRunning || this.data.stopBusy || this.data.startBusy) return;
    this.setData({ stopBusy: true });
    try {
      await stopTradingLoop();
      await refreshLiveSnapshot({ force: true });
    } finally {
      this.setData({ stopBusy: false });
    }
  },

  async onEmergencyClose() {
    if (!this.data.positions.length || this.data.emergencyBusy) return;
    this.setData({ emergencyBusy: true });
    try {
      await emergencyCloseAll(null);
      await refreshLiveSnapshot({ force: true });
    } finally {
      this.setData({ emergencyBusy: false });
    }
  },
});
