import { consumePendingTradeTraceQuery, fetchTradeTrace, rememberTradeTraceQuery } from '../../services/ops';
import { openLearningGovernancePage } from '../../services/learning';
import { formatDateTime, humanizeRiskAction } from '../../utils/format';
import { normalizeGovernanceStageKey } from '../../utils/governance';

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

function humanizeSupervisorAction(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'hold') return '继续持有';
  if (key === 'tighten') return '收紧保护';
  if (key === 'reduce') return '减仓保护';
  if (key === 'close') return '主动平仓';
  return value || '暂无';
}

function humanizeLedgerEvent(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'open') return '开仓';
  if (key === 'close') return '平仓';
  if (key.startsWith('supervisor_')) return '持仓监督';
  if (key === 'skip') return '跳过';
  return value || '账本事件';
}

function humanizePositionEvent(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'opened') return '已开仓';
  if (key === 'updated') return '持仓更新';
  if (key === 'closed') return '已平仓';
  return value || '仓位事件';
}

function humanizeOrderEvent(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'filled') return '已成交';
  if (key === 'submitted') return '已提交';
  if (key === 'cancelled') return '已撤单';
  if (key === 'rejected') return '已拒绝';
  return value || '订单事件';
}

function humanizeRecoveryStatus(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'open') return '恢复为在持仓';
  if (key === 'closed') return '恢复为已平仓';
  return value || '未知状态';
}

function humanizeTimelineKind(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'ledger') return '账本';
  if (key === 'supervisor') return '监督';
  if (key === 'position') return '仓位';
  if (key === 'order') return '订单';
  if (key === 'recovery') return '恢复';
  if (key === 'review') return '复盘';
  if (key === 'governance') return '治理';
  return value || '事件';
}

function formatNumberMaybe(value, digits = 2) {
  if (value === null || value === undefined || value === '') return '--';
  const num = Number(value);
  if (Number.isNaN(num)) return String(value);
  return num.toFixed(digits);
}

function classifyTimelineFocus(kind = '') {
  const normalized = String(kind || '').toLowerCase();
  if (normalized === 'governance') return 'governance';
  if (normalized === 'review') return 'review';
  if (normalized === 'ledger' || normalized === 'supervisor') return 'decision';
  if (normalized === 'position' || normalized === 'order' || normalized === 'recovery') return 'execution';
  return 'other';
}

function humanizeTimelineFocus(value = '') {
  const normalized = String(value || '').toLowerCase();
  if (normalized === 'governance') return '治理推进';
  if (normalized === 'review') return '复盘归因';
  if (normalized === 'decision') return '决策与监督';
  if (normalized === 'execution') return '执行落地';
  return '其他';
}

function resolveCountSummary(count = 0, config = {}, fallbackSummary = '', fallbackEmptySummary = '') {
  const normalizedCount = Number(count || 0);
  const summaryTemplate = String(config.summary_template || '');
  const emptySummary = String(config.empty_summary || fallbackEmptySummary || '');
  if (normalizedCount > 0 && summaryTemplate) {
    return summaryTemplate.replace('{count}', String(normalizedCount));
  }
  if (normalizedCount > 0) return fallbackSummary;
  return emptySummary;
}

function buildTimelineFilterOptions(items = [], timelineFilterContext = null) {
  const counts = {
    governance: 0,
    review: 0,
    decision: 0,
    execution: 0,
  };
  const focusFilters = ((timelineFilterContext || {}).focus_filters) || {};
  items.forEach((item) => {
    const focus = String(item.focusGroup || '');
    if (counts[focus] !== undefined) {
      counts[focus] += 1;
    }
  });
  const governanceCount = counts.governance + counts.review;
  return [
    {
      key: 'all',
      label: String(((focusFilters.all || {}).label) || '全部'),
      count: items.length,
      summary: resolveCountSummary(
        items.length,
        focusFilters.all || {},
      ),
    },
    {
      key: 'governance',
      label: String(((focusFilters.governance || {}).label) || '治理相关'),
      count: governanceCount,
      summary: resolveCountSummary(
        governanceCount,
        focusFilters.governance || {},
      ),
    },
    {
      key: 'decision',
      label: String(((focusFilters.decision || {}).label) || '决策监督'),
      count: counts.decision,
      summary: resolveCountSummary(
        counts.decision,
        focusFilters.decision || {},
      ),
    },
    {
      key: 'execution',
      label: String(((focusFilters.execution || {}).label) || '执行落地'),
      count: counts.execution,
      summary: resolveCountSummary(
        counts.execution,
        focusFilters.execution || {},
      ),
    },
  ];
}

function buildGovernanceStageFilterOptions(items = [], timelineFilterContext = null) {
  const governanceItems = items.filter((item) => item.focusGroup === 'governance' || item.focusGroup === 'review');
  const counts = {};
  const stageFilters = ((timelineFilterContext || {}).governance_stage_filters) || {};
  governanceItems.forEach((item) => {
    const key = normalizeGovernanceStageKey(item.governanceStageTag || '');
    if (!key) return;
    counts[key] = (counts[key] || 0) + 1;
  });
  const options = [
    {
      key: 'all',
      label: String(((stageFilters.all || {}).label) || '全部治理态'),
      count: governanceItems.length,
      summary: resolveCountSummary(
        governanceItems.length,
        stageFilters.all || {},
      ),
    },
    {
      key: 'online_light',
      label: String(((stageFilters.online_light || {}).label) || '在线轻调'),
      count: counts.online_light || 0,
      summary: String(((stageFilters.online_light || {}).summary) || ''),
    },
    {
      key: 'offline_deep',
      label: String(((stageFilters.offline_deep || {}).label) || '离线深调'),
      count: counts.offline_deep || 0,
      summary: String(((stageFilters.offline_deep || {}).summary) || ''),
    },
    {
      key: 'pending_review',
      label: String(((stageFilters.pending_review || {}).label) || '待审候选'),
      count: counts.pending_review || 0,
      summary: String(((stageFilters.pending_review || {}).summary) || ''),
    },
    {
      key: 'approved',
      label: String(((stageFilters.approved || {}).label) || '等待发布'),
      count: counts.approved || 0,
      summary: String(((stageFilters.approved || {}).summary) || ''),
    },
    {
      key: 'deployed',
      label: String(((stageFilters.deployed || {}).label) || '发布观察'),
      count: counts.deployed || 0,
      summary: String(((stageFilters.deployed || {}).summary) || ''),
    },
    {
      key: 'rolled_back',
      label: String(((stageFilters.rolled_back || {}).label) || '已回滚'),
      count: counts.rolled_back || 0,
      summary: String(((stageFilters.rolled_back || {}).summary) || ''),
    },
  ];
  return options.filter((item) => item.key === 'all' || item.count > 0);
}

function applyTimelineFilter(items = [], filter = 'all') {
  const normalized = String(filter || 'all').toLowerCase();
  if (normalized === 'governance') {
    return items.filter((item) => item.focusGroup === 'governance' || item.focusGroup === 'review');
  }
  if (normalized === 'decision') {
    return items.filter((item) => item.focusGroup === 'decision');
  }
  if (normalized === 'execution') {
    return items.filter((item) => item.focusGroup === 'execution');
  }
  return items;
}

function applyGovernanceStageFilter(items = [], filter = 'all') {
  const normalized = String(filter || 'all').toLowerCase();
  if (!normalized || normalized === 'all') return items;
  return items.filter((item) => normalizeGovernanceStageKey(item.governanceStageTag || '') === normalized);
}

function mapGovernanceQuickAction(item = {}) {
  return {
    type: String(item.type || ''),
    label: String(item.label || ''),
    buttonTone: String(item.button_tone || 'secondary'),
    summary: String(item.summary || ''),
    factorId: String(item.factor_id || ''),
    recommendationId: String(item.recommendation_id || ''),
    candidateId: String(item.candidate_id || ''),
  };
}

function buildGovernanceOverviewView(governanceOverview = null, parameterGovernance = null) {
  const overview = governanceOverview || {};
  const governance = parameterGovernance || {};
  const entryContext = governance.entry_context || {};
  const stageLabel = String(
    overview.stage_label
    || entryContext.stage_label
    || governance.stage_label
    || ''
  );
  const stageSummary = String(
    overview.stage_summary
    || entryContext.stage_summary
    || governance.priority_summary
    || governance.stage_summary
    || ''
  );
  const nextStepLabel = String(
    overview.next_step_label
    || entryContext.next_step_label
    || governance.next_step_label
    || ''
  );
  const nextStepSummary = String(
    overview.next_step_summary
    || entryContext.next_step_summary
    || governance.next_step_summary
    || ''
  );
  const entryLabel = String(overview.entry_label || entryContext.entry_label || governance.target_type || '');
  const entryType = String(overview.entry_type || entryContext.entry_type || governance.entry_type || '');
  const targetType = String(overview.target_type || governance.target_type || '');
  const actionLabel = String(overview.action_label || entryContext.action_label || governance.action_label || '');
  const priorityLabel = String(overview.priority_label || governance.priority_label || '');
  const latestCandidateId = String(overview.latest_candidate_id || '');
  const latestCandidateStatusText = String(overview.latest_candidate_status_text || '');
  const latestCandidateTraceText = String(overview.latest_candidate_trace_text || '');
  return {
    opsSummary: String(overview.ops_summary || governance.ops_summary || ''),
    stageLabel,
    stageSummary,
    nextStepLabel,
    nextStepSummary,
    entryLabel,
    entryType,
    targetType,
    actionLabel,
    priorityLabel,
    prioritySummary: String(overview.priority_summary || governance.priority_summary || ''),
    latestCandidateId,
    latestCandidateStatusText,
    latestCandidateTraceText,
    latestCandidateSummaryText: String(overview.latest_candidate_summary_text || ''),
    entryHintText: String(overview.entry_hint_text || ''),
    showStageCard: !!overview.show_stage_card,
  };
}

function buildTimelineView(tradeTraceView = null, timelineFilter = 'all', governanceStageFilter = 'all') {
  const view = tradeTraceView || null;
  const filter = String(timelineFilter || 'all');
  const stageFilter = filter === 'governance'
    ? String(governanceStageFilter || 'all')
    : 'all';
  let visibleItems = applyTimelineFilter((view && view.timelineItems) || [], filter);
  if (filter === 'governance') {
    visibleItems = applyGovernanceStageFilter(visibleItems, stageFilter);
  }
  const filters = ((view && view.timelineFilters) || []).map((item) => ({
    ...item,
    active: item.key === filter,
  }));
  const governanceFilters = ((view && view.governanceStageFilters) || []).map((item) => ({
    ...item,
    active: item.key === stageFilter,
  }));
  const selectedFilter = filters.find((item) => item.active) || filters[0] || null;
  const selectedGovernanceFilter = governanceFilters.find((item) => item.active) || governanceFilters[0] || null;
  return {
    filter,
    governanceStageFilter: stageFilter,
    visibleItems,
    filters,
    governanceFilters,
    filterSummary: (selectedFilter && selectedFilter.summary) || '',
    governanceStageSummary: filter === 'governance'
      ? ((selectedGovernanceFilter && selectedGovernanceFilter.summary) || '')
      : '',
  };
}

function describeGovernanceJump(parameterGovernance = null) {
  const backendJump = parameterGovernance && parameterGovernance.governance_jump
    ? parameterGovernance.governance_jump
    : null;
  if (backendJump && backendJump.type) {
    return {
      type: String(backendJump.type || ''),
      typeLabel: String(backendJump.type_label || ''),
      buttonText: String(backendJump.button_text || ''),
      summary: String(backendJump.summary || ''),
      candidateId: String(backendJump.candidate_id || ''),
      recommendationId: String(backendJump.recommendation_id || ''),
      suggestionId: String(backendJump.suggestion_id || ''),
      lifecycleEventId: String(backendJump.lifecycle_event_id || ''),
    };
  }
  return {
    type: '',
    typeLabel: '',
    buttonText: '',
    summary: '',
  };
}

function buildGovernanceTodo(parameterGovernance = null, governanceOverviewView = null, governanceJump = null) {
  if (!parameterGovernance) return null;
  const backendTodoQueue = parameterGovernance.governance_todo_queue || null;
  if (backendTodoQueue && backendTodoQueue.primary_task) {
    const mapTask = (item = {}) => ({
      type: String(item.type || ''),
      typeLabel: String(item.type_label || ''),
      targetId: String(item.target_id || ''),
      title: String(item.title || ''),
      reason: String(item.reason || ''),
      buttonText: String(item.button_text || ''),
      priorityLabel: String(item.priority_label || ''),
      summary: String(item.summary || ''),
      candidateId: String(item.candidate_id || ''),
      recommendationId: String(item.recommendation_id || ''),
      suggestionId: String(item.suggestion_id || ''),
      lifecycleEventId: String(item.lifecycle_event_id || ''),
      priorityScore: Number(item.priority_score || 0),
    });
    return {
      primaryTask: mapTask(backendTodoQueue.primary_task || {}),
      secondaryTasks: (backendTodoQueue.secondary_tasks || []).map((item) => mapTask(item)),
      queueSummary: String(backendTodoQueue.queue_summary || ''),
      queueHint: String(backendTodoQueue.queue_hint || ''),
    };
  }
  return null;
}

function attachTimelineGovernanceLink(item = {}, governanceJump = null, options = {}) {
  const kind = String(item.kind || '');
  const isParameterReview = !!options.isParameterReview;
  const timelineContext = options.timelineContext || null;
  const timelineActions = ((timelineContext && timelineContext.governance_actions) || []).map((action) => ({
    type: String(action.type || ''),
    typeLabel: String(action.type_label || ''),
    buttonText: String(action.button_text || ''),
    summary: String(action.summary || ''),
    factorId: String(action.factor_id || ''),
    source: String(action.source || ''),
    suggestionId: String(action.suggestion_id || ''),
    recommendationId: String(action.recommendation_id || ''),
    candidateId: String(action.candidate_id || ''),
    lifecycleEventId: String(action.lifecycle_event_id || ''),
  })).filter((action) => action.type && action.buttonText);
  if (timelineContext && kind === 'governance') {
    return {
      ...item,
      governanceStageTag: String(timelineContext.stage_tag || ''),
      governanceStageSummary: String(timelineContext.stage_summary || ''),
      jumpType: String(timelineContext.governance_jump_type || ''),
      jumpTypeLabel: String(timelineContext.governance_jump_type_label || ''),
      jumpButtonText: String(timelineContext.governance_jump_button_text || ''),
      jumpSummary: String(timelineContext.governance_jump_summary || ''),
      suggestionId: String(timelineContext.suggestion_id || ''),
      recommendationId: String(timelineContext.recommendation_id || ''),
      candidateId: String(timelineContext.candidate_id || ''),
      lifecycleEventId: String(timelineContext.lifecycle_event_id || ''),
      factorId: '',
      source: '',
      governanceActions: timelineActions,
    };
  }
  if (timelineContext && kind === 'review' && isParameterReview) {
    return {
      ...item,
      governanceStageTag: String(timelineContext.review_stage_tag || timelineContext.stage_tag || ''),
      governanceStageSummary: String(timelineContext.review_stage_summary || timelineContext.stage_summary || ''),
      jumpType: String(timelineContext.governance_jump_type || ''),
      jumpTypeLabel: String(timelineContext.governance_jump_type_label || ''),
      jumpButtonText: String(timelineContext.review_jump_button_text || ''),
      jumpSummary: String(timelineContext.review_jump_summary || ''),
      suggestionId: String(timelineContext.suggestion_id || ''),
      recommendationId: String(timelineContext.recommendation_id || ''),
      candidateId: String(timelineContext.candidate_id || ''),
      lifecycleEventId: String(timelineContext.lifecycle_event_id || ''),
      factorId: '',
      source: '',
      governanceActions: timelineActions,
    };
  }
  return {
    ...item,
    governanceStageTag: '',
    governanceStageSummary: '',
    jumpType: '',
    jumpTypeLabel: '',
    jumpButtonText: '',
    jumpSummary: '',
    suggestionId: '',
    recommendationId: '',
    candidateId: '',
    lifecycleEventId: '',
    factorId: '',
    source: '',
    governanceActions: [],
  };
}

function describeTradeTrace(trace = null) {
  if (!trace) return null;
  const summary = trace.summary || {};
  const parameterGovernance = trace.parameter_governance || null;
  const governanceOverview = (parameterGovernance && parameterGovernance.overview) || null;
  const timelineContext = (parameterGovernance && parameterGovernance.timeline_context) || null;
  const timelineFilterContext = (parameterGovernance && parameterGovernance.timeline_filter_context) || null;
  const quickActions = ((parameterGovernance && parameterGovernance.quick_actions) || []).map((item) => mapGovernanceQuickAction(item));
  const recommendation = parameterGovernance && parameterGovernance.recommendation
    ? parameterGovernance.recommendation
    : null;
  const governanceJump = describeGovernanceJump(parameterGovernance);
  const governanceOverviewView = buildGovernanceOverviewView(
    governanceOverview,
    parameterGovernance,
  );
  const governanceTodo = buildGovernanceTodo(parameterGovernance, governanceOverviewView, governanceJump);
  const ledgerEvents = (trace.decision_ledger || []).map((item) => {
    const verdict = (item.risk_state || {}).policy_verdict || {};
    return {
      id: item.decision_id,
      title: humanizeLedgerEvent(item.event_type),
      timeText: formatDateTime(item.decision_ts || item.created_at || 0),
      reasonText: item.action_reason || verdict.reason || '无额外说明',
      verdictText: verdict.reason || '无统一裁决',
      actionText: humanizeRiskAction((((verdict.audit_payload || {}).action) || item.event_type || '--')),
    };
  });
  const supervisorLatest = (trace.position_supervisor || {}).latest || null;
  const supervisorView = supervisorLatest
    ? {
        actionText: humanizeSupervisorAction((((supervisorLatest.action || {}).supervisor_verdict || {}).action || '')),
        reasonText: (((supervisorLatest.action || {}).supervisor_verdict || {}).human_summary || supervisorLatest.action_reason || '持仓监督已触发'),
        timeText: formatDateTime(supervisorLatest.decision_ts || supervisorLatest.created_at || 0),
      }
    : null;
  const review = trace.review || null;
  const reviewView = review
    ? {
        outcomeText: humanizeOutcome(review.outcome_label || ''),
        responsibilityText: humanizeResponsibility(review.primary_responsibility || ''),
        closeReasonText: ((review.review || {}).close_reason || summary.latest_close_reason || '--'),
        pnlText: formatNumberMaybe(review.pnl, 2),
        summaryText: review.summary_text || '复盘已记录，但还没有额外摘要。',
      }
    : null;
  const reviewCarriesParameterGovernance =
    !!(review && parameterGovernance && (parameterGovernance.recommendation || parameterGovernance.latest_candidate));
  const factorContributions = (trace.factor_contributions || []).slice(0, 3).map((item) => ({
    factor: item.factor || '--',
    roleText: item.factor_role === 'harmful' ? '拖累' : item.factor_role === 'helpful' ? '帮助' : '中性',
    responsibilityText: humanizeResponsibility(item.primary_responsibility || ''),
    contributionText: formatNumberMaybe(item.net_contribution, 3),
  }));
  const positionLifecycle = (trace.position_lifecycle || []).map((item) => ({
    id: item.event_id || `${item.position_id}_${item.event_ts}`,
    title: humanizePositionEvent(item.event_type),
    timeText: formatDateTime(item.event_ts || 0),
    volumeText: formatNumberMaybe(item.net_volume, 2),
    priceText: formatNumberMaybe(item.avg_price, 2),
    reasonText: (item.details || {}).reason || '无额外说明',
  }));
  const orderLifecycle = (trace.order_lifecycle || []).map((item) => ({
    id: item.event_id || `${item.order_id}_${item.event_ts}`,
    title: humanizeOrderEvent(item.event_type),
    timeText: formatDateTime(item.event_ts || 0),
    statusText: item.status || '--',
    priceText: formatNumberMaybe(item.price, 2),
    volumeText: formatNumberMaybe(item.volume, 2),
    controlText: (item.details && ((item.details.sl && `SL ${item.details.sl}`) || (item.details.tp && `TP ${item.details.tp}`))) || '',
  }));
  const recovery = trace.recovery_state || null;
  const recoveryView = recovery
    ? {
        statusText: humanizeRecoveryStatus(recovery.status || ''),
        directionText: Number(recovery.direction || 0) < 0 ? 'SHORT' : Number(recovery.direction || 0) > 0 ? 'LONG' : '--',
        openPriceText: formatNumberMaybe(recovery.open_price, 2),
        volumeText: formatNumberMaybe(recovery.volume, 2),
        closeReasonText: recovery.close_reason || '--',
        sourceText: ((recovery.recovery_meta || {}).source || '--'),
      }
    : null;
  const rawTimelineItems = [
    ...(trace.decision_ledger || []).map((item) => {
      const verdict = (item.risk_state || {}).policy_verdict || {};
      return {
        id: `ledger_${item.decision_id || item.created_at}`,
        ts: Number(item.decision_ts || item.created_at || 0),
        timeText: formatDateTime(item.decision_ts || item.created_at || 0),
        kind: 'ledger',
        kindText: humanizeTimelineKind('ledger'),
        focusGroup: classifyTimelineFocus('ledger'),
        focusText: humanizeTimelineFocus(classifyTimelineFocus('ledger')),
        title: humanizeLedgerEvent(item.event_type),
        note: item.action_reason || verdict.reason || '无额外说明',
      };
    }),
    ...(trace.position_lifecycle || []).map((item) => ({
      id: `position_${item.event_id || item.event_ts}`,
      ts: Number(item.event_ts || 0),
      timeText: formatDateTime(item.event_ts || 0),
      kind: 'position',
      kindText: humanizeTimelineKind('position'),
      focusGroup: classifyTimelineFocus('position'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('position')),
      title: humanizePositionEvent(item.event_type),
      note: (item.details || {}).reason || '仓位生命周期已记录',
    })),
    ...(trace.order_lifecycle || []).map((item) => ({
      id: `order_${item.event_id || item.event_ts}`,
      ts: Number(item.event_ts || 0),
      timeText: formatDateTime(item.event_ts || 0),
      kind: 'order',
      kindText: humanizeTimelineKind('order'),
      focusGroup: classifyTimelineFocus('order'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('order')),
      title: humanizeOrderEvent(item.event_type),
      note: item.status || '订单状态已记录',
    })),
    ...(supervisorLatest ? [{
      id: `supervisor_${supervisorLatest.decision_id || supervisorLatest.created_at || supervisorLatest.decision_ts}`,
      ts: Number(supervisorLatest.decision_ts || supervisorLatest.created_at || 0),
      timeText: formatDateTime(supervisorLatest.decision_ts || supervisorLatest.created_at || 0),
      kind: 'supervisor',
      kindText: humanizeTimelineKind('supervisor'),
      focusGroup: classifyTimelineFocus('supervisor'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('supervisor')),
      title: humanizeSupervisorAction((((supervisorLatest.action || {}).supervisor_verdict || {}).action || '')),
      note: (((supervisorLatest.action || {}).supervisor_verdict || {}).human_summary || supervisorLatest.action_reason || '持仓监督已触发'),
    }] : []),
    ...(review ? [{
      id: `review_${review.review_id || review.created_at}`,
      ts: Number(review.created_at || 0),
      timeText: formatDateTime(review.created_at || 0),
      kind: 'review',
      kindText: humanizeTimelineKind('review'),
      focusGroup: classifyTimelineFocus('review'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('review')),
      title: humanizeOutcome(review.outcome_label || ''),
      note: review.summary_text || '复盘记录已生成',
    }] : []),
    ...(latestCandidate ? [{
      id: `governance_${latestCandidate.candidate_id || latestCandidate.created_at}`,
      ts: Number(latestCandidate.updated_at || latestCandidate.created_at || 0),
      timeText: formatDateTime(latestCandidate.updated_at || latestCandidate.created_at || 0),
      kind: 'governance',
      kindText: humanizeTimelineKind('governance'),
      focusGroup: classifyTimelineFocus('governance'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('governance')),
      title: String((timelineContext && timelineContext.stage_tag) || governanceOverviewView.stageLabel || ''),
      note: String((timelineContext && timelineContext.stage_summary) || governanceOverviewView.stageSummary || governanceOverviewView.nextStepSummary || ''),
    }] : recommendation ? [{
      id: `governance_${recommendation.recommendation_id || summary.position_id || summary.trade_id || 'recommendation'}`,
      ts: Number(recommendation.created_at || 0),
      timeText: formatDateTime(recommendation.created_at || 0),
      kind: 'governance',
      kindText: humanizeTimelineKind('governance'),
      focusGroup: classifyTimelineFocus('governance'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('governance')),
      title: String((timelineContext && timelineContext.stage_tag) || governanceOverviewView.stageLabel || ''),
      note: String((timelineContext && timelineContext.stage_summary) || governanceOverviewView.stageSummary || governanceOverviewView.nextStepSummary || ''),
    }] : []),
    ...(recovery ? [{
      id: `recovery_${summary.position_id || summary.trade_id || recovery.updated_at || recovery.recovered_at || 'state'}`,
      ts: Number(recovery.updated_at || recovery.recovered_at || 0),
      timeText: formatDateTime(recovery.updated_at || recovery.recovered_at || 0),
      kind: 'recovery',
      kindText: humanizeTimelineKind('recovery'),
      focusGroup: classifyTimelineFocus('recovery'),
      focusText: humanizeTimelineFocus(classifyTimelineFocus('recovery')),
      title: humanizeRecoveryStatus(recovery.status || ''),
      note: recovery.close_reason || ((recovery.recovery_meta || {}).source ? `恢复来源 ${(recovery.recovery_meta || {}).source}` : '恢复状态已记录'),
    }] : []),
  ].sort((a, b) => Number(a.ts || 0) - Number(b.ts || 0));
  const timelineItems = rawTimelineItems.map((item) => attachTimelineGovernanceLink(
    item,
    governanceJump,
    {
      isParameterReview: item.kind === 'review' && reviewCarriesParameterGovernance,
      timelineContext,
    }
  ));
  const timelineFilters = buildTimelineFilterOptions(timelineItems, timelineFilterContext);
  const governanceStageFilters = buildGovernanceStageFilterOptions(timelineItems, timelineFilterContext);
  return {
    summary,
    parameterGovernance,
    governanceOverview,
    ledgerEvents,
    supervisorView,
    reviewView,
    factorContributions,
    positionLifecycle,
    orderLifecycle,
    recoveryView,
    timelineItems,
    timelineFilters,
    governanceStageFilters,
    governanceJump,
    governanceTodo,
    governanceQuickActions: quickActions,
    governanceOverviewView,
    timelineFilterContext,
  };
}

Page({
  data: {
    tracePositionId: '',
    traceDecisionId: '',
    traceBusy: false,
    tradeTraceView: null,
    timelineFilter: 'all',
    governanceStageFilter: 'all',
    traceErrorText: '',
    traceSectionOpen: {
      ledger: false,
      supervisor: true,
      review: true,
      factors: false,
      position: false,
      order: false,
      recovery: false,
      timeline: true,
    },
  },

  onLoad(options = {}) {
    const positionId = String(options.position_id || '').trim();
    const decisionId = String(options.decision_id || '').trim();
    if (positionId || decisionId) {
      this.runTradeTraceQuery({ positionId, decisionId, silent: true });
      return;
    }
    this.consumePendingTradeTrace();
  },

  onShow() {
    if (!this.data.tradeTraceView && !this.data.traceBusy) {
      this.consumePendingTradeTrace();
    }
  },

  toggleTraceSection(e) {
    const section = String((e.currentTarget.dataset && e.currentTarget.dataset.section) || '');
    if (!section) return;
    const current = this.data.traceSectionOpen || {};
    this.setData({
      traceSectionOpen: {
        ...current,
        [section]: !current[section],
      },
    });
  },

  syncTimelineView(tradeTraceView = null, timelineFilter = '') {
    const view = tradeTraceView || this.data.tradeTraceView || null;
    const filter = String(timelineFilter || this.data.timelineFilter || 'all');
    const governanceStageFilter = filter === 'governance'
      ? String(this.data.governanceStageFilter || 'all')
      : 'all';
    const timelineView = buildTimelineView(view, filter, governanceStageFilter);
    this.setData({
      timelineFilter: filter,
      governanceStageFilter,
      tradeTraceView: view
        ? {
            ...view,
            timelineView,
          }
        : null,
    });
  },

  switchTimelineFilter(e) {
    const filter = String((e.currentTarget.dataset && e.currentTarget.dataset.filter) || 'all');
    if (filter !== 'governance' && this.data.governanceStageFilter !== 'all') {
      this.setData({ governanceStageFilter: 'all' });
    }
    this.syncTimelineView(this.data.tradeTraceView, filter);
  },

  switchGovernanceStageFilter(e) {
    const filter = String((e.currentTarget.dataset && e.currentTarget.dataset.stageFilter) || 'all');
    this.setData({ governanceStageFilter: filter }, () => {
      this.syncTimelineView(this.data.tradeTraceView, 'governance');
    });
  },

  consumePendingTradeTrace() {
    const pending = consumePendingTradeTraceQuery();
    if (!pending) return;
    this.runTradeTraceQuery({
      positionId: pending.positionId || '',
      decisionId: pending.decisionId || '',
      silent: true,
    });
  },

  async runTradeTraceQuery({ positionId = '', decisionId = '', silent = false } = {}) {
    if (!positionId && !decisionId) {
      this.setData({ traceErrorText: '缺少 position_id 或 decision_id。' });
      return;
    }
    this.setData({
      traceBusy: true,
      tracePositionId: positionId,
      traceDecisionId: decisionId,
      traceErrorText: '',
    });
    try {
      const trace = await fetchTradeTrace({ positionId, decisionId });
      rememberTradeTraceQuery({ positionId, decisionId });
      const tradeTraceView = describeTradeTrace(trace);
      this.setData({ traceErrorText: '' });
      this.syncTimelineView(tradeTraceView, this.data.timelineFilter || 'all');
    } catch (err) {
      const statusCode = Number(err && err.statusCode);
      const traceErrorText = statusCode === 404 ? '没有找到这笔交易。' : '查询失败，请稍后再试。';
      this.setData({
        tradeTraceView: null,
        traceErrorText,
      });
      if (!silent) {
        wx.showToast({
          title: statusCode === 404 ? '没有找到这笔交易' : '查询失败',
          icon: 'none',
          duration: 2000,
        });
      }
    } finally {
      this.setData({ traceBusy: false });
    }
  },

  openGovernanceRecommendation() {
    const actions = ((this.data.tradeTraceView || {}).governanceQuickActions) || [];
    const action = actions.find((item) => item.type === 'template_recommendation') || null;
    if (!action || !action.recommendationId) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: action.recommendationId,
      factorId: action.factorId || '',
    });
  },

  openGovernanceCandidate() {
    const actions = ((this.data.tradeTraceView || {}).governanceQuickActions) || [];
    const action = actions.find((item) => item.type === 'offline_candidate') || null;
    if (!action || !action.candidateId) return;
    openLearningGovernancePage({
      type: 'offline_candidate',
      candidateId: action.candidateId,
      factorId: action.factorId || '',
    });
  },

  openGovernanceAction() {
    const jump = ((this.data.tradeTraceView || {}).governanceJump) || null;
    this.openGovernanceFocus(jump);
  },

  openTimelineGovernanceAction(e) {
    const dataset = (e.currentTarget && e.currentTarget.dataset) || {};
    const jump = {
      type: String(dataset.type || ''),
      suggestionId: String(dataset.suggestionId || ''),
      recommendationId: String(dataset.recommendationId || ''),
      candidateId: String(dataset.candidateId || ''),
      lifecycleEventId: String(dataset.lifecycleEventId || ''),
      factorId: String(dataset.factorId || ''),
      source: String(dataset.source || ''),
    };
    this.openGovernanceFocus(jump);
  },

  openGovernanceFocus(jump = null) {
    if (!jump || !jump.type) return;
    openLearningGovernancePage({
      type: jump.type,
      suggestionId: jump.suggestionId || '',
      recommendationId: jump.recommendationId || '',
      candidateId: jump.candidateId || '',
      lifecycleEventId: jump.lifecycleEventId || '',
      factorId: jump.factorId || '',
      source: jump.source || '',
    });
  },
});
