import liveStore from '../../stores/live';
import learningStore from '../../stores/learning';
import { refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { refreshLearning, runLearningGovernance } from '../../services/learning';
import { formatMoney, formatPct, formatTime, toneFromPnl, toneFromStatus } from '../../utils/format';

Page({
  data: {
    wsLabel: '未连接',
    wsTone: 'warning',
    equity: '--',
    pnlToday: '--',
    drawdown: '--',
    positions: '0',
    loopLabel: '未知',
    loopTone: 'neutral',
    latestReview: null,
    learningSummary: {},
    governanceLabel: '观察中',
    governanceTone: 'neutral',
    learningApplications: 0,
    closureSteps: [],
    updatedAt: '--',
  },

  onLoad() {
    this._unsubs = [
      liveStore.subscribe(() => this.syncView()),
      learningStore.subscribe(() => this.syncView()),
    ];
    this.syncView();
    this.refreshAll();
  },

  onShow() {
    this.refreshAll();
  },

  onUnload() {
    (this._unsubs || []).forEach((fn) => fn && fn());
  },

  syncView() {
    const live = liveStore.getState();
    const learning = learningStore.getState();
    const trading = live.trading || {};
    const loopStatus = live.loopStatus || {};
    const summary = learning.summary || { suggestions: {}, latest_review: null };
    const suggestions = summary.suggestions || {};
    const applications = Number(summary.applications || 0);
    const proposed = Number(suggestions.proposed || 0);
    const approved = Number(suggestions.approved || 0);
    const governanceLabel = proposed > 0 ? '待审核经验' : approved > 0 ? '已形成可用经验' : '学习观察中';
    const governanceTone = proposed > 0 ? 'warning' : approved > 0 ? 'positive' : 'neutral';
    this.setData({
      wsLabel: live.wsConnected ? '实时已连接' : '轮询兜底中',
      wsTone: live.wsConnected ? 'positive' : 'warning',
      equity: formatMoney(trading.equity),
      pnlToday: formatMoney(trading.daily && trading.daily.pnl),
      pnlTodayTone: toneFromPnl(trading.daily && trading.daily.pnl),
      drawdown: formatPct(trading.daily && trading.daily.drawdown_pct),
      positions: String(trading.n_positions || 0),
      loopLabel: loopStatus.running ? '交易循环运行中' : '交易循环已停止',
      loopTone: loopStatus.running ? 'positive' : 'warning',
      latestReview: summary.latest_review,
      learningSummary: suggestions,
      governanceLabel,
      governanceTone,
      learningApplications: applications,
      closureSteps: [
        {
          id: 'signal',
          index: '1',
          title: '信号生成',
          note: loopStatus.running ? '交易循环在线' : '等待启动',
          tone: loopStatus.running ? 'positive' : 'warning',
        },
        {
          id: 'position',
          index: '2',
          title: '仓位执行',
          note: trading.n_positions ? `当前 ${trading.n_positions} 笔持仓` : '当前无持仓',
          tone: trading.n_positions ? 'positive' : 'neutral',
        },
        {
          id: 'review',
          index: '3',
          title: '平仓复盘',
          note: summary.latest_review ? summary.latest_review.outcome_label || '已生成复盘' : '等待平仓样本',
          tone: summary.latest_review ? 'positive' : 'neutral',
        },
        {
          id: 'apply',
          index: '4',
          title: '经验应用',
          note: applications ? `已应用 ${applications} 次` : proposed ? '有建议待治理' : '暂未应用',
          tone: applications ? 'positive' : proposed ? 'warning' : 'neutral',
        },
      ],
      updatedAt: formatTime(live.lastUpdate || learning.updatedAt),
    });
  },

  async refreshAll() {
    await Promise.all([refreshLiveSnapshot(), refreshLearning()]);
  },

  async onStart() {
    await startTradingLoop();
    await refreshLiveSnapshot();
  },

  async onStop() {
    await stopTradingLoop();
    await refreshLiveSnapshot();
  },

  async onGovern() {
    await runLearningGovernance();
  },

  goLearning() {
    wx.switchTab({ url: '/pages/learning/index' });
  },

  goTrading() {
    wx.switchTab({ url: '/pages/trading/index' });
  },
});
