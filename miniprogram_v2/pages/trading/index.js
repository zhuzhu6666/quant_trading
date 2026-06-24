import liveStore from '../../stores/live';
import { emergencyCloseAll, refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { formatMoney, formatPct, toneFromStatus } from '../../utils/format';

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
    const strategy = state.strategyStatus || {};
    const recentSignal = strategy.recent_signal || strategy.signal || {};
    const direction = recentSignal.direction || strategy.direction || trading.position.dir || 'FLAT';
    const positionDir = trading.position && trading.position.dir;
    const gateReason = strategy.gate_reason || '';
    const circuitBreaker = !!strategy.circuit_breaker;
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
    this.setData({
      signalLabel: direction === 1 || direction === 'LONG' ? '偏多' : direction === -1 || direction === 'SHORT' ? '偏空' : '观望',
      signalTone: direction === 1 || direction === 'LONG' ? 'positive' : direction === -1 || direction === 'SHORT' ? 'negative' : 'neutral',
      gateLabel,
      gateTone,
      strategy: {
        pipelineRunning: !!strategy.pipeline_running,
        positionDir,
        entry: trading.position && trading.position.entry,
        size: trading.position && trading.position.size,
        gateReason,
        circuitBreaker,
      },
      positions: trading.positions_list || [],
      daily: {
        pnl: formatMoney(trading.daily && trading.daily.pnl),
        drawdown: formatPct(trading.daily && trading.daily.drawdown_pct),
        trades: trading.daily && trading.daily.trades,
      },
      risk: trading.risk || {},
      currentPrice: trading.current_price || '--',
    });
  },

  async onStart() {
    await startTradingLoop();
    await refreshLiveSnapshot();
  },

  async onStop() {
    await stopTradingLoop();
    await refreshLiveSnapshot();
  },

  async onEmergencyClose() {
    await emergencyCloseAll(null);
    await refreshLiveSnapshot();
  },
});
