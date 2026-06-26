import liveStore from '../../stores/live';
import { emergencyCloseAll, refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { openTradeTracePage } from '../../services/ops';
import { formatMoney, formatPct, formatPrice, formatDateTime, formatDurationMinutes, humanizeRiskAction, humanizeRiskReason } from '../../utils/format';

function isActiveDirection(value) {
  return value === 'LONG' || value === 'SHORT' || value === 1 || value === -1;
}

function isReadyToOpen(readiness = {}) {
  const brokerConnected = readiness.broker_status === 'connected';
  return !!(
    readiness.loop_running &&
    readiness.bridge_ready &&
    readiness.account_ready &&
    brokerConnected
  );
}

function humanizeBlockingComponent(key = '') {
  const mapping = {
    ctrader_bridge: '交易桥',
    live_loop: '实盘循环',
    bar_m1: '1分钟K线',
    bar_m5: '5分钟K线',
    tick_data: 'Tick 数据',
    l2_depth: '盘口深度',
    db_ctrader_data: '主行情库',
    db_ticks: 'Tick 库',
    db_l2: '深度库',
    disk_space: '磁盘空间',
  };
  return mapping[key] || key || '--';
}

function normalizePosition(item = {}) {
  const pnl = Number(item.pnl ?? item.netUnrealizedPnL ?? item.unrealized ?? item.profit ?? 0);
  const currentPrice = item.current_price ?? item.price_current ?? 0;
  const openPrice = item.open_price ?? item.price_open ?? 0;
  const holdingMinutes = Number(item.holding_minutes || 0);
  const timeoutStatus = String(item.holding_timeout_status || 'disabled');
  let timeoutLabel = '未启用';
  let timeoutTone = 'neutral';
  if (timeoutStatus === 'expired') {
    timeoutLabel = '已超时';
    timeoutTone = 'negative';
  } else if (timeoutStatus === 'watch') {
    timeoutLabel = '接近上限';
    timeoutTone = 'warning';
  } else if (timeoutStatus === 'normal') {
    timeoutLabel = '正常';
    timeoutTone = 'positive';
  }
  return {
    ...item,
    pnlValue: pnl,
    pnlText: formatMoney(pnl),
    currentPriceText: formatPrice(currentPrice, 3),
    openPriceText: formatPrice(openPrice, 3),
    volumeText: String(item.volume ?? item.size ?? '--'),
    directionText: item.type === 'buy' ? 'LONG' : item.type === 'sell' ? 'SHORT' : (item.direction || '--'),
    pnlToneClass: pnl >= 0 ? 'accent-pos' : 'accent-neg',
    holdingText: formatDurationMinutes(holdingMinutes),
    timeoutLabel,
    timeoutTone,
    timeoutHint: item.timeout_enabled
      ? (item.holding_timeout_exceeded
        ? '已经超过系统设定的持仓时长上限。'
        : `距离上限还剩 ${formatDurationMinutes(Number(item.holding_timeout_remaining_seconds || 0) / 60)}。`)
      : '当前没有启用自动超时平仓。',
    supervisorLabel: item.supervisor_label || '继续持有',
    supervisorSummary: item.supervisor_summary || '当前没有看到足够强的主动收口信号。',
    supervisorTone: item.supervisor_action === 'close'
      ? 'negative'
      : item.supervisor_action === 'reduce' || item.supervisor_action === 'tighten'
        ? 'warning'
        : 'positive',
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
    positionSummaryLabel: '当前无持仓',
    positionExposureLabel: '--',
    realizedPnlText: '--',
    unrealizedPnlText: '--',
    loopRunning: false,
    startBusy: false,
    stopBusy: false,
    emergencyBusy: false,
    currentGateView: null,
    policyHistoryView: null,
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
    const recentSignals = Array.isArray(strategy.recent_signals) ? strategy.recent_signals : [];
    const recentSignal = recentSignals.length ? recentSignals[recentSignals.length - 1] : (strategy.recent_signal || strategy.signal || {});
    const readiness = strategy.readiness || {};
    const direction = recentSignal.direction || strategy.direction || trading.position.dir || 'FLAT';
    const positionDir = trading.position && trading.position.dir;
    const hasActivePosition = positions.length > 0 || isActiveDirection(positionDir);
    const gateReason = strategy.gate_reason || '';
    const circuitBreaker = !!strategy.circuit_breaker;
    const pipelineRunning = !!(
      strategy.pipeline_running ||
      strategy.running ||
      v4Status.pipeline_active ||
      loopStatus.running
    );
    const runtimeBlocked = !!(riskSummary.system_health && riskSummary.system_health.trading_blocked);
    const openReady = isReadyToOpen(readiness) && !circuitBreaker && !runtimeBlocked;
    const strategyReason = String(strategy.reason || '').trim();
    const lastReadinessReason = Array.isArray(readiness.reasons) ? readiness.reasons[0] : '';
    let gateLabel = '等待信号';
    let gateTone = 'neutral';
    if (circuitBreaker) {
      gateLabel = '风控熔断中';
      gateTone = 'negative';
    } else if (runtimeBlocked) {
      gateLabel = '系统暂不允许开仓';
      gateTone = 'negative';
    } else if (gateReason) {
      gateLabel = '信号被闸门拦截';
      gateTone = 'warning';
    } else if (hasActivePosition) {
      gateLabel = '策略已持仓';
      gateTone = 'positive';
    } else if (openReady) {
      gateLabel = '可以开仓';
      gateTone = 'positive';
    } else if (direction === 1 || direction === 'LONG' || direction === -1 || direction === 'SHORT') {
      gateLabel = '信号可执行';
      gateTone = 'positive';
    }
    const realizedDailyPnl = Number((trading.daily && trading.daily.pnl) || 0);
    const unrealizedPnl = Number(trading.unrealized_pnl ?? positions.reduce((sum, item) => sum + Number(item.pnlValue || 0), 0));
    const livePnl = Number(trading.live_pnl ?? (realizedDailyPnl + unrealizedPnl));
    const balance = Number(trading.balance || 0);
    const equity = Number(trading.equity || 0);
    const equityDrawdownPct = balance > 0 ? Math.max(0, ((balance - equity) / balance) * 100) : 0;
    const sessionDrawdownPct = Number((trading.daily && trading.daily.drawdown_pct) || 0);
    const liveDrawdownPct = Math.max(sessionDrawdownPct, equityDrawdownPct);
    const policy = riskSummary.policy || {};
    const systemHealth = riskSummary.system_health || {};
    const latestVerdict = Array.isArray(policy.items) ? policy.items[0] : null;
    const impactSummary = systemHealth.impact_summary || '';
    const blockingComponents = Array.isArray(systemHealth.blocking_components) ? systemHealth.blocking_components : [];
    const currentGateView = {
      tone: circuitBreaker || runtimeBlocked
        ? 'negative'
        : gateReason
          ? 'warning'
          : hasActivePosition || openReady
            ? 'positive'
            : 'neutral',
      title: '现在能不能开新仓',
      status: circuitBreaker
        ? '熔断中'
        : runtimeBlocked
          ? '系统风控阻断'
        : gateReason
          ? '策略暂不开仓'
        : hasActivePosition
          ? '持仓中'
          : openReady
            ? '可以继续开仓'
            : '等待信号',
      summary: circuitBreaker
        ? '风险熔断已触发，系统不会继续推进新的开仓。'
        : runtimeBlocked
          ? (impactSummary || '系统健康检查当前不允许继续开新仓。')
        : gateReason
          ? `系统没拦，但策略闸门当前卡住了这笔信号：${humanizeRiskReason(gateReason)}`
        : hasActivePosition
          ? '当前已有持仓，系统在管理现有仓位。'
          : openReady
            ? '系统风控当前没有阻断；只要后续出现合格信号，就可以继续推进开仓。'
            : (strategyReason || '当前没有阻断，只是暂时没有达到开仓条件。'),
      detail: circuitBreaker || runtimeBlocked
        ? `阻断来源：${blockingComponents.length ? blockingComponents.map(humanizeBlockingComponent).join(' / ') : '风险系统'}`
        : gateReason
          ? '这是策略自己的节奏控制，不是系统硬风控。'
          : openReady
            ? '看到“最近一次历史拦截”不代表现在仍然被拦。'
            : lastReadinessReason
              ? `当前状态：${lastReadinessReason}`
              : '系统在线，正在等待下一次满足条件的信号。'
    };
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
        readiness,
        reason: strategyReason,
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
      positionSummaryLabel: (trading.position_summary && trading.position_summary.label) || '当前无持仓',
      positionExposureLabel: trading.position_summary
        ? `净仓 ${trading.position_summary.netVolume || 0} / 总量 ${trading.position_summary.grossVolume || 0}`
        : '--',
      realizedPnlText: formatMoney(realizedDailyPnl),
      unrealizedPnlText: formatMoney(unrealizedPnl),
      currentGateView,
      policyHistoryView: latestVerdict
        ? {
            title: latestVerdict.allowed ? '最近一次历史放行（仅供回看）' : '最近一次历史拦截（仅供回看）',
            action: humanizeRiskAction(latestVerdict.action || latestVerdict.event_type || '--'),
            reason: humanizeRiskReason(latestVerdict.reason || '--'),
            tone: latestVerdict.allowed ? 'positive' : 'negative',
            blocked: Number((policy.counts && policy.counts.blocked) || 0),
            allowed: Number((policy.counts && policy.counts.allowed) || 0),
            timeText: latestVerdict.decision_ts ? formatDateTime(latestVerdict.decision_ts) : '--',
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

  openTradeTrace(e) {
    const positionId = String((e.currentTarget.dataset && e.currentTarget.dataset.positionId) || '').trim();
    if (!positionId) return;
    openTradeTracePage({ positionId });
  },
});
