import systemStore from '../../stores/system';
import learningStore from '../../stores/learning';
import { openTradeTracePage, refreshOpsDomain } from '../../services/ops';
import { openLearningGovernancePage, refreshLearning } from '../../services/learning';
import { formatDateTime, humanizeRiskAction, humanizeRiskReason, toneFromStatus } from '../../utils/format';
import {
  buildGovernanceTodoCard,
  normalizeGovernanceStageKey,
  sortGovernanceItemsByPriority,
} from '../../utils/governance';

function humanizeHealthComponent(key) {
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

function resolveEventTone(eventType) {
  const normalized = String(eventType || '').toLowerCase();
  if (['cycle_complete', 'cycle_succeeded', 'completed', 'success'].includes(normalized)) {
    return 'positive';
  }
  if (['cycle_warning', 'partial_success'].includes(normalized)) {
    return 'warning';
  }
  if (['cycle_failed', 'failed', 'error'].includes(normalized)) {
    return 'negative';
  }
  return toneFromStatus(normalized || 'ok');
}

function buildDbCards(dbHealth) {
  if (!dbHealth) return [];
  const checkedAt = dbHealth.checked_at ? formatDateTime(dbHealth.checked_at) : '--';
  const summary =
    typeof dbHealth.summary === 'string'
      ? dbHealth.summary
      : dbHealth.summary && typeof dbHealth.summary === 'object'
        ? '已生成数据库健康摘要'
        : '暂无摘要';

  return [
    {
      key: '连接状态',
      value: dbHealth.ok ? '正常' : '异常',
      valueClass: dbHealth.ok ? 'accent-pos' : 'accent-neg',
    },
    {
      key: '总体健康',
      value: dbHealth.overall || 'unknown',
      valueClass:
        toneFromStatus(dbHealth.overall || 'unknown') === 'positive'
          ? 'accent-pos'
          : toneFromStatus(dbHealth.overall || 'unknown') === 'negative'
            ? 'accent-neg'
            : toneFromStatus(dbHealth.overall || 'unknown') === 'warning'
              ? 'accent-warn'
              : '',
    },
    {
      key: '检查时间',
      value: checkedAt,
      valueClass: '',
    },
    {
      key: '健康摘要',
      value: summary,
      valueClass: '',
    },
  ];
}

function summarizePolicy(policy = null, systemHealth = null) {
  if (!policy) return null;
  const counts = policy.counts || {};
  const blocked = Number(counts.blocked || 0);
  const allowed = Number(counts.allowed || 0);
  const items = Array.isArray(policy.items) ? policy.items : [];
  const latest = items[0] || null;
  const byReason = policy.by_reason || {};
  const byAction = policy.by_action || {};
  const topReason = Object.keys(byReason)
    .sort((a, b) => Number(byReason[b] || 0) - Number(byReason[a] || 0))[0] || '';
  const actionRows = Object.keys(byAction)
    .sort((a, b) => Number(byAction[b] || 0) - Number(byAction[a] || 0))
    .slice(0, 4)
    .map((key) => ({ key, label: humanizeRiskAction(key), value: byAction[key] }));
  const tradingBlocked = !!(systemHealth && systemHealth.trading_blocked);
  const impactStatus = systemHealth && systemHealth.impact_status ? systemHealth.impact_status : 'ok';
  const blockingComponents = Array.isArray(systemHealth && systemHealth.blocking_components)
    ? systemHealth.blocking_components
    : [];
  const currentTone = tradingBlocked ? 'negative' : impactStatus === 'observe' ? 'warning' : 'positive';
  const currentStatus = tradingBlocked ? '现在不能开仓' : '现在可以开仓';
  const currentReason = tradingBlocked
    ? ((systemHealth && systemHealth.impact_summary) || '当前有硬风控项会直接阻断开仓。')
    : ((systemHealth && systemHealth.impact_summary) || '当前没有会直接阻断开仓的硬风控项。');
  const currentDetail = tradingBlocked
    ? `直接阻断项：${blockingComponents.length ? blockingComponents.map(humanizeHealthComponent).join(' / ') : '未提供'}`
    : '最近的拦截记录只代表历史，不代表现在这一刻。';
  return {
    blocked,
    allowed,
    total: Number(policy.total || items.length || 0),
    currentTone,
    currentStatus,
    currentReason,
    currentDetail,
    latestAction: latest ? humanizeRiskAction(latest.action || latest.event_type || '--') : '--',
    latestReason: latest ? humanizeRiskReason(latest.reason || '--') : '--',
    latestTone: latest ? (latest.allowed ? 'positive' : 'negative') : 'neutral',
    latestTitle: latest
      ? (latest.allowed ? '最近一次历史放行' : '最近一次历史拦截')
      : '最近一次历史裁决',
    latestTime: latest && latest.decision_ts ? formatDateTime(latest.decision_ts) : '--',
    topReason: humanizeRiskReason(topReason || '暂无'),
    actionRows,
  };
}

function summarizeSystemHealth(systemHealth = null) {
  if (!systemHealth) return null;
  const critical = Array.isArray(systemHealth.critical_components) ? systemHealth.critical_components : [];
  const degraded = Array.isArray(systemHealth.degraded_components) ? systemHealth.degraded_components : [];
  const blocking = Array.isArray(systemHealth.blocking_components) ? systemHealth.blocking_components : [];
  const advisoryCritical = Array.isArray(systemHealth.advisory_critical_components) ? systemHealth.advisory_critical_components : [];
  const effectiveCritical = critical.filter((key) => !advisoryCritical.includes(key));
  const components = systemHealth.components && typeof systemHealth.components === 'object' ? systemHealth.components : {};
  const componentRows = Object.keys(components)
    .slice(0, 6)
    .map((key) => {
      const rawStatus = components[key] && components[key].status ? components[key].status : 'unknown';
      const statusText = advisoryCritical.includes(key)
        ? 'observe-only'
        : blocking.includes(key)
          ? 'blocking'
          : rawStatus;
      return {
        key: humanizeHealthComponent(key),
        status: statusText,
      };
    });
  const impactStatus = systemHealth.impact_status || (blocking.length ? 'blocked' : critical.length || degraded.length ? 'observe' : 'ok');
  const impactTone =
    impactStatus === 'blocked'
      ? 'negative'
      : impactStatus === 'observe'
        ? 'warning'
        : toneFromStatus(systemHealth.overall || 'unknown');
  return {
    overall: systemHealth.overall || 'unknown',
    tone: impactTone,
    impactStatus,
    impactLabel:
      impactStatus === 'blocked'
        ? '会阻断交易'
        : impactStatus === 'observe'
          ? '需要盯住'
          : '暂不影响交易',
    impactSummary: systemHealth.impact_summary || '暂无运行风险摘要',
    criticalCount: critical.length,
    degradedCount: degraded.length,
    blockingCount: blocking.length,
    criticalText: effectiveCritical.length ? effectiveCritical.map(humanizeHealthComponent).join(' / ') : '无',
    degradedText: degraded.length ? degraded.map(humanizeHealthComponent).join(' / ') : '无',
    blockingText: blocking.length ? blocking.map(humanizeHealthComponent).join(' / ') : '无',
    advisoryText: advisoryCritical.length ? advisoryCritical.map(humanizeHealthComponent).join(' / ') : '无',
    scoreText: Number(systemHealth.overall_score || 0).toFixed(2),
    componentRows,
  };
}

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

function humanizeOutcome(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'good_win') return '高质量盈利';
  if (key === 'lucky_win') return '幸运盈利';
  if (key === 'good_loss') return '可接受亏损';
  if (key === 'bad_loss') return '无效亏损';
  if (key === 'win') return '盈利';
  if (key === 'loss') return '亏损';
  return value || '进行中';
}

function humanizeTraceResponsibility(value = '') {
  return humanizeResponsibility(value || '');
}

function matchesRecentTraceFilter(item = {}, filter = 'all') {
  const key = String(filter || 'all').toLowerCase();
  const governanceStageKey = normalizeGovernanceStageKey(item.parameter_governance_stage || '');
  if (key === 'all') return true;
  if (key === 'parameter_only') return !!item.parameter_governance_factor;
  if (key === 'exit_only') return String(item.primary_responsibility || '') === 'exit';
  if (key === 'timing_only') return String(item.primary_responsibility || '') === 'timing';
  if (key === 'governance_hint') return !!item.parameter_governance_factor || !!item.parameter_candidate_status;
  if (key === 'candidate_review') return String(item.parameter_governance_entry_type || '') === 'candidate';
  if (key === 'recommendation_only') return String(item.parameter_governance_entry_type || '') === 'recommendation';
  if (key === 'online_light_only') return governanceStageKey === 'online_light';
  if (key === 'offline_deep_only') return governanceStageKey === 'offline_deep';
  if (key === 'deployed_watch') return governanceStageKey === 'deployed';
  return true;
}

function matchesRecentTraceSearch(item = {}, keyword = '') {
  const text = String(keyword || '').trim().toLowerCase();
  if (!text) return true;
  const haystack = [
    item.position_id,
    item.trade_id,
    item.entry_decision_id,
    item.exit_decision_id,
    item.summary_text,
    item.parameter_governance_factor,
    item.symbol,
    item.primary_responsibility,
  ]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');
  return haystack.includes(text);
}

function mapOverviewHintCard(item = null) {
  if (!item) return null;
  return {
    factorId: String(item.factor_id || ''),
    candidateId: String(item.candidate_id || ''),
    recommendationId: String(item.recommendation_id || ''),
    title: String(item.title || ''),
    stageText: String(item.stage_tag || ''),
    note: String(item.summary || ''),
    actionLabel: String(item.action_label || ''),
  };
}

Page({
  data: {
    scheduler: null,
    evolution: null,
    dbHealth: null,
    apiHealth: null,
    schedulerSummary: null,
    dbCards: [],
    apiMetricClass: '',
    evolutionMetricClass: '',
    jobsMetricClass: '',
    policySummary: null,
    systemHealthSummary: null,
    recentTradeTraceItems: [],
    totalRecentTradeTraceCount: 0,
    recentTraceFilter: 'all',
    recentTraceSearch: '',
    tracePositionId: '',
    traceDecisionId: '',
    recentTraceQueries: [],
    pendingTemplateCandidateCount: 0,
    pendingTemplateRecommendationCount: 0,
    onlineLightRecommendationCount: 0,
    offlineDeepRecommendationCount: 0,
    pendingCandidateCard: null,
    pendingOnlineRecommendationCard: null,
    pendingOfflineRecommendationCard: null,
    pendingGovernanceTodoCard: null,
    recentTraceTodoCard: null,
  },

  onLoad() {
    this._unsubSystem = systemStore.subscribe(() => this.syncView());
    this._unsubLearning = learningStore.subscribe(() => this.syncView());
    this.syncView();
    refreshOpsDomain();
    refreshLearning();
  },

  onShow() {
    refreshOpsDomain();
    refreshLearning();
  },

  onUnload() {
    this._unsubSystem && this._unsubSystem();
    this._unsubLearning && this._unsubLearning();
  },

  syncView() {
    const state = systemStore.getState();
    const apiHealth = state.apiHealth || {};
    const scheduler = state.scheduler || null;
    const jobs = (scheduler && scheduler.jobs) || [];
    const dbHealth = state.dbHealth;
    const riskSummary = state.riskSummary || null;
    const systemHealthSummary = summarizeSystemHealth(riskSummary && riskSummary.system_health);
    const runningJobs = jobs.filter((item) => item.running).length;
    const dbCards = buildDbCards(dbHealth);
    const policySummary = summarizePolicy(riskSummary && riskSummary.policy, riskSummary && riskSummary.system_health);
    const recentTradeTraceItems = (state.recentTradeTraces || []).map((item) => {
      const governanceStageTag = String(item.parameter_governance_stage || '');
      const governanceStageSummary = String(item.parameter_governance_stage_summary || '');
      const governanceNextStepText = String(item.parameter_governance_next_step || '');
      const governanceTargetTypeText = String(item.parameter_governance_target_type || '');
      const governanceEntryHintText = String(item.parameter_governance_entry_hint_text || '');
      const governanceActionLabel = String(item.parameter_governance_action_label || '');
      const governancePriority = {
        score: Number(item.parameter_governance_priority_score || 0),
        label: String(item.parameter_governance_priority_label || ''),
        summary: String(item.parameter_governance_priority_summary || ''),
      };
      return {
        ...item,
      id: item.review_id || `${item.position_id || '--'}_${item.created_at || 0}`,
      title: item.position_id ? `position ${item.position_id}` : `trade ${item.trade_id || '--'}`,
      outcomeText: humanizeOutcome(item.outcome_label || ''),
      responsibilityText: humanizeTraceResponsibility(item.primary_responsibility || ''),
      governanceText: item.parameter_governance_factor
        ? `${item.parameter_governance_factor} · ${item.parameter_candidate_status || '待评估'}`
        : '',
      governanceStageText: governanceStageTag,
      governanceStageTag,
      governanceStageSummary,
      governanceNextStepText,
      governanceActionLabel,
      governanceTargetTypeText,
      governanceEntryTypeText: governanceEntryHintText,
      governanceCandidateId: String(item.parameter_candidate_id || ''),
      governanceRecommendationId: String(item.parameter_recommendation_id || ''),
      governancePriorityScore: governancePriority.score,
      governancePriorityLabel: governancePriority.label,
      governancePrioritySummary: governancePriority.summary,
      timeText: formatDateTime(item.created_at || 0),
      };
    });
    const recentTraceFilter = this.data.recentTraceFilter || 'all';
    const recentTraceSearch = this.data.recentTraceSearch || '';
    const learning = learningStore.getState();
    const learningSummary = learning.summary || {};
    const candidateCounts = learningSummary.parameter_template_candidates || {};
    const recommendationCounts = learningSummary.parameter_template_recommendations || {};
    const summaryOverview = learningSummary.parameter_template_overview || {};
    const pendingCandidateCard = mapOverviewHintCard(summaryOverview.pending_candidate_hint);
    const pendingOnlineRecommendationCard = mapOverviewHintCard(summaryOverview.online_light_hint);
    const pendingOfflineRecommendationCard = mapOverviewHintCard(summaryOverview.offline_deep_hint);
    const filteredRecentTradeTraceItems = recentTradeTraceItems.filter(
      (item) => matchesRecentTraceFilter(item, recentTraceFilter) && matchesRecentTraceSearch(item, recentTraceSearch)
    );
    const sortedRecentTradeTraceItems = sortGovernanceItemsByPriority(filteredRecentTradeTraceItems);
    const recentTraceTodoCard = buildGovernanceTodoCard(sortedRecentTradeTraceItems);
    const summaryGovernanceTodo = learningSummary.parameter_template_todo || null;
    const pendingGovernanceTodoCard = summaryGovernanceTodo
      ? {
          factorId: String(summaryGovernanceTodo.factor_id || ''),
          candidateId: String(summaryGovernanceTodo.candidate_id || ''),
          recommendationId: String(summaryGovernanceTodo.recommendation_id || ''),
          title: String(summaryGovernanceTodo.title || ''),
          stageText: String(summaryGovernanceTodo.stage_tag || ''),
          note: String(summaryGovernanceTodo.priority_summary || summaryGovernanceTodo.summary || ''),
          actionLabel: String(summaryGovernanceTodo.action_label || ''),
          targetTypeText: String(summaryGovernanceTodo.target_type || ''),
          priorityLabel: String(summaryGovernanceTodo.priority_label || ''),
          queueHint: String(summaryGovernanceTodo.queue_hint || ''),
        }
      : null;
    const recentTraceQueries = (state.recentTradeTraceQueries || []).map((item) => ({
      ...item,
      id: `${item.positionId || '--'}_${item.decisionId || '--'}_${item.ts || 0}`,
      label: item.positionId
        ? `position ${item.positionId}`
        : `decision ${item.decisionId || '--'}`,
      sublabel: item.positionId && item.decisionId
        ? `decision ${item.decisionId}`
        : item.positionId
          ? 'position 查询'
          : 'decision 查询',
      timeText: formatDateTime(item.ts || 0),
    }));
    this.setData({
      scheduler,
      evolution: state.evolution
        ? {
            ...state.evolution,
            tone: resolveEventTone(state.evolution.event_type || 'ok'),
            tsText: formatDateTime(state.evolution.ts || state.evolution.timestamp || 0),
          }
        : null,
      dbHealth,
      policySummary,
      systemHealthSummary,
      recentTradeTraceItems: sortedRecentTradeTraceItems,
      totalRecentTradeTraceCount: recentTradeTraceItems.length,
      recentTraceQueries,
      pendingTemplateCandidateCount: Number(candidateCounts.pending_review || 0),
      pendingTemplateRecommendationCount: Number(recommendationCounts.total || 0),
      onlineLightRecommendationCount: Number(recommendationCounts.online_light || 0),
      offlineDeepRecommendationCount: Number(recommendationCounts.offline_deep || 0),
      pendingCandidateCard,
      pendingOnlineRecommendationCard,
      pendingOfflineRecommendationCard,
      pendingGovernanceTodoCard,
      recentTraceTodoCard,
      apiHealth: {
        ...apiHealth,
        tone: toneFromStatus(apiHealth.status || 'ok'),
      },
      schedulerSummary: {
        total: jobs.length,
        running: runningJobs,
        errors: jobs.reduce((sum, item) => sum + Number(item.error_count || 0), 0),
      },
      dbCards,
      apiMetricClass:
        toneFromStatus(apiHealth.status || 'ok') === 'positive'
          ? 'accent-pos'
          : toneFromStatus(apiHealth.status || 'ok') === 'negative'
            ? 'accent-neg'
            : toneFromStatus(apiHealth.status || 'ok') === 'warning'
              ? 'accent-warn'
              : '',
      evolutionMetricClass: state.evolution
        ? resolveEventTone(state.evolution.event_type || 'ok') === 'positive'
          ? 'accent-pos'
          : resolveEventTone(state.evolution.event_type || 'ok') === 'negative'
            ? 'accent-neg'
            : resolveEventTone(state.evolution.event_type || 'ok') === 'warning'
              ? 'accent-warn'
              : ''
        : '',
      jobsMetricClass: jobs.length
        ? runningJobs
          ? 'accent-pos'
          : 'accent-warn'
        : '',
    });
  },

  async onRefresh() {
    await refreshOpsDomain();
  },

  onTracePositionInput(e) {
    this.setData({ tracePositionId: e.detail.value || '' });
  },

  onTraceDecisionInput(e) {
    this.setData({ traceDecisionId: e.detail.value || '' });
  },

  async onQueryTradeTrace() {
    const positionId = String(this.data.tracePositionId || '').trim();
    const decisionId = String(this.data.traceDecisionId || '').trim();
    if (!positionId && !decisionId) {
      wx.showToast({
        title: '请输入 position_id 或 decision_id',
        icon: 'none',
        duration: 1800,
      });
      return;
    }
    openTradeTracePage({ positionId, decisionId });
  },

  replayRecentTrace(e) {
    const positionId = String((e.currentTarget.dataset && e.currentTarget.dataset.positionId) || '').trim();
    const decisionId = String((e.currentTarget.dataset && e.currentTarget.dataset.decisionId) || '').trim();
    openTradeTracePage({ positionId, decisionId });
  },

  openRecentTradeTrace(e) {
    const positionId = String((e.currentTarget.dataset && e.currentTarget.dataset.positionId) || '').trim();
    const decisionId = String((e.currentTarget.dataset && e.currentTarget.dataset.decisionId) || '').trim();
    openTradeTracePage({ positionId, decisionId });
  },

  openRecentGovernance(e) {
    const candidateId = String((e.currentTarget.dataset && e.currentTarget.dataset.candidateId) || '').trim();
    const recommendationId = String((e.currentTarget.dataset && e.currentTarget.dataset.recommendationId) || '').trim();
    const factorId = String((e.currentTarget.dataset && e.currentTarget.dataset.factorId) || '').trim();
    if (candidateId) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId,
        factorId,
      });
      return;
    }
    if (recommendationId) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId,
        factorId,
      });
    }
  },

  openPendingCandidate() {
    const item = this.data.pendingCandidateCard || null;
    if (!item || !item.candidateId) return;
    openLearningGovernancePage({
      type: 'offline_candidate',
      candidateId: item.candidateId,
      factorId: item.factorId,
    });
  },

  openPendingRecommendation() {
    const item = this.data.pendingOnlineRecommendationCard || null;
    if (!item || !item.recommendationId) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: item.recommendationId,
      factorId: item.factorId,
    });
  },

  openOfflineRecommendation() {
    const item = this.data.pendingOfflineRecommendationCard || null;
    if (!item || !item.recommendationId) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: item.recommendationId,
      factorId: item.factorId,
    });
  },

  switchRecentTraceFilter(e) {
    const filter = String((e.currentTarget.dataset && e.currentTarget.dataset.filter) || 'all');
    this.setData({ recentTraceFilter: filter }, () => this.syncView());
  },

  onRecentTraceSearchInput(e) {
    this.setData({ recentTraceSearch: e.detail.value || '' }, () => this.syncView());
  },
});
