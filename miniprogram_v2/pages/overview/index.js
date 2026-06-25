import liveStore from '../../stores/live';
import learningStore from '../../stores/learning';
import { refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { openLearningGovernancePage, refreshLearning, runLearningGovernance } from '../../services/learning';
import { formatMoney, formatPct, formatTime, toneFromPnl, toneFromStatus } from '../../utils/format';

function humanizeResponsibility(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'exit') return '退出问题';
  if (key === 'timing') return '时长问题';
  if (key === 'regime') return '市场切换问题';
  if (key === 'parameter') return '参数问题';
  if (key === 'thesis') return 'thesis 失效';
  if (key === 'holding') return '持仓效率问题';
  return '暂未定责';
}

function formatOverviewHintText(item = null) {
  if (!item) return '';
  return `${item.title || '--'} · ${item.stage_tag || '--'} · ${item.summary || ''}`;
}

Page({
  data: {
    wsLabel: '未连接',
    wsTone: 'warning',
    equity: '--',
    pnlToday: '--',
    pnlTodayTone: 'neutral',
    drawdown: '--',
    positions: '0',
    loopLabel: '未知',
    loopTone: 'neutral',
    latestReview: null,
    learningSummary: {},
    governanceLabel: '观察中',
    governanceTone: 'neutral',
    learningApplications: 0,
    templateOpsSummary: '',
    closureSteps: [],
    loopRunning: false,
    startBusy: false,
    stopBusy: false,
    governBusy: false,
    pendingTemplateCandidateCount: 0,
    pendingTemplateRecommendationCount: 0,
    onlineLightRecommendationCount: 0,
    offlineDeepRecommendationCount: 0,
    pendingCandidateHint: '',
    pendingOnlineRecommendationHint: '',
    pendingOfflineRecommendationHint: '',
    governanceHeadlineSummary: '',
    governanceTodoCard: null,
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
    const candidateCounts = summary.parameter_template_candidates || {};
    const recommendationCounts = summary.parameter_template_recommendations || {};
    const proposed = Number(suggestions.proposed || 0);
    const summaryOverview = summary.parameter_template_overview || {};
    const summaryHeadline = summaryOverview.headline || {};
    const governanceLabel = String(summaryHeadline.label || '学习观察中');
    const governanceTone = String(summaryHeadline.tone || 'neutral');
    const summaryGovernanceTodo = summary.parameter_template_todo || null;
    const governanceTodoCard = summaryGovernanceTodo
      ? {
          factorId: String(summaryGovernanceTodo.factor_id || ''),
          candidateId: String(summaryGovernanceTodo.candidate_id || ''),
          recommendationId: String(summaryGovernanceTodo.recommendation_id || ''),
          title: String(summaryGovernanceTodo.title || ''),
          priorityLabel: String(summaryGovernanceTodo.priority_label || ''),
          stageTag: String(summaryGovernanceTodo.stage_tag || ''),
          targetTypeText: String(summaryGovernanceTodo.target_type || ''),
          actionLabel: String(summaryGovernanceTodo.action_label || ''),
          summary: String(summaryGovernanceTodo.priority_summary || summaryGovernanceTodo.summary || ''),
          queueHint: String(summaryGovernanceTodo.queue_hint || ''),
        }
      : null;
    const pendingCandidateHintObject = summaryOverview.pending_candidate_hint || null;
    const onlineLightHintObject = summaryOverview.online_light_hint || null;
    const offlineDeepHintObject = summaryOverview.offline_deep_hint || null;
    const templateOpsSummary = String(summary.parameter_template_ops_summary || '');
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
      loopRunning: !!loopStatus.running,
      latestReview: summary.latest_review,
      learningSummaryRaw: summary,
      learningSummary: suggestions,
      governanceLabel,
      governanceTone,
      governanceHeadlineSummary: String(summaryHeadline.summary || ''),
      governanceTodoCard,
      learningApplications: applications,
      templateOpsSummary,
      pendingTemplateCandidateCount: Number(candidateCounts.pending_review || 0),
      pendingTemplateRecommendationCount: Number(recommendationCounts.total || 0),
      onlineLightRecommendationCount: Number(recommendationCounts.online_light || 0),
      offlineDeepRecommendationCount: Number(recommendationCounts.offline_deep || 0),
      pendingCandidateHint: pendingCandidateHintObject
        ? formatOverviewHintText(pendingCandidateHintObject)
        : '',
      pendingOnlineRecommendationHint: onlineLightHintObject
        ? formatOverviewHintText(onlineLightHintObject)
        : '',
      pendingOfflineRecommendationHint: offlineDeepHintObject
        ? formatOverviewHintText(offlineDeepHintObject)
        : '',
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
        {
          id: 'template',
          index: '5',
          title: '参数治理',
          note: templateOpsSummary,
          tone: recommendationCounts.total ? 'warning' : 'neutral',
        },
      ],
      updatedAt: formatTime(live.lastUpdate || learning.updatedAt),
    });
  },

  async refreshAll() {
    await Promise.all([refreshLiveSnapshot(), refreshLearning()]);
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

  async onGovern() {
    if (this.data.governBusy) return;
    this.setData({ governBusy: true });
    try {
      await runLearningGovernance();
    } finally {
      this.setData({ governBusy: false });
    }
  },

  goLearning() {
    wx.switchTab({ url: '/pages/learning/index' });
  },

  goTrading() {
    wx.switchTab({ url: '/pages/trading/index' });
  },

  openLatestGovernance() {
    const summary = this.data.learningSummaryRaw || {};
    const latestCandidate = summary.latest_parameter_template_candidate || null;
    const latestCandidateTrace = summary.latest_parameter_template_candidate_trace || null;
    const latestRecommendation = summary.latest_parameter_template_recommendation || null;
    if (latestCandidate && latestCandidate.candidate_id) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId: latestCandidate.candidate_id,
        factorId: latestCandidate.factor_id || '',
      });
      return;
    }
    if (latestCandidateTrace && latestCandidateTrace.recommendation_id) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: latestCandidateTrace.recommendation_id,
      });
      return;
    }
    if (latestRecommendation && latestRecommendation.recommendation_id) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: latestRecommendation.recommendation_id,
        factorId: latestRecommendation.factor_id || '',
      });
    }
  },

  openPendingCandidate() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).pending_candidate_hint) || null;
    if (!(hint && hint.candidate_id)) return;
    openLearningGovernancePage({
      type: 'offline_candidate',
      candidateId: hint.candidate_id,
      factorId: hint.factor_id || '',
    });
  },

  openPendingRecommendation() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).online_light_hint) || null;
    if (!(hint && hint.recommendation_id)) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: hint.recommendation_id,
      factorId: hint.factor_id || '',
    });
  },

  openOfflineRecommendation() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).offline_deep_hint) || null;
    if (!(hint && hint.recommendation_id)) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: hint.recommendation_id,
      factorId: hint.factor_id || '',
    });
  },

  openGovernanceTodo() {
    const item = this.data.governanceTodoCard || null;
    if (!item) return;
    if (item.candidateId) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId: item.candidateId,
        factorId: item.factorId,
      });
      return;
    }
    if (item.recommendationId) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: item.recommendationId,
        factorId: item.factorId,
      });
    }
  },
});
