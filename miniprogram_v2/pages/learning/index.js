import learningStore from '../../stores/learning';
import {
  consumeLearningGovernanceFocus,
  refreshLearning,
  reviewSuggestion,
  runLearningGovernance,
  materializeTemplateRecommendation,
  reviewOfflineCandidate,
  releaseOfflineCandidate,
  rollbackOfflineCandidate,
} from '../../services/learning';
import { openTradeTracePage } from '../../services/ops';
import { formatDateTime, toneFromStatus } from '../../utils/format';
import { sortGovernanceItemsByPriority } from '../../utils/governance';

function humanizeScopeKey(scopeKey = '') {
  if (!scopeKey) return '未命名因子';
  if (String(scopeKey).includes(':')) {
    const [factorId, regimeKey] = String(scopeKey).split(':');
    return regimeKey && regimeKey !== 'default'
      ? `${humanizeFactorId(factorId)} / ${regimeKey}`
      : `${humanizeFactorId(factorId)} / 默认模板`;
  }
  return String(scopeKey)
    .replace(/^dsl_auto_/, 'DSL 自动因子 ')
    .replace(/_/g, ' ');
}

function humanizeBoundaryScope(scope = '') {
  return String(scope || '').toLowerCase() === 'offline_deep' ? '离线深调' : '在线轻调';
}

function humanizeBoundaryReason(reason = '') {
  const key = String(reason || '').toLowerCase();
  if (key === 'fits_runtime_guardrail') return '满足当前运行态护栏';
  if (key === 'factor_not_runtime_tunable') return '该因子暂不支持运行时直接改参数';
  if (key === 'formula_version_changed') return '模板公式版本发生变化';
  if (key === 'factor_family_changed') return '模板所属因子家族发生变化';
  if (key === 'parameter_delta_too_large') return '参数跳变幅度超过在线护栏';
  if (key === 'unsupported_template_role') return '模板角色不在在线护栏允许范围内';
  return reason || '未分类边界原因';
}

function describeSuggestion(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const evidence = item.evidence || {};
  const templateDisplay = item.parameter_template_display || {};
  const confidence = Number(item.confidence || 0);
  let actionLabel = '观察';
  let impactText = '暂时不改权重，继续积累样本。';
  let reasonText = item.reason || '系统正在积累证据。';

  if (action.includes('downweight')) {
    actionLabel = '降低权重';
    impactText = '下一轮开仓会更谨慎地使用这个因子。';
  } else if (action.includes('boost')) {
    actionLabel = '提高权重';
    impactText = '下一轮开仓会更重视这个因子。';
  } else if (action.includes('quarantine')) {
    actionLabel = '隔离观察';
    impactText = '短期内减少这个因子的直接影响。';
  } else if (action.includes('retire')) {
    actionLabel = '候选退役';
    impactText = '系统倾向于让这个因子退出主要决策。';
  } else if (action.includes('tighten_gate')) {
    actionLabel = '收紧闸门';
    impactText = '后续开仓会更严格通过风险条件。';
  } else if (action.includes('watch')) {
    actionLabel = '继续观察';
    impactText = '暂时不自动改权重，只记录进经验池。';
  } else if (action.includes('switch_parameter_template')) {
    actionLabel = '切换参数模板';
  }

  if (reasonText.includes('still accumulating evidence')) {
    reasonText = '最近有表现，但证据还不够强，系统先继续观察。';
  }

  const boundary = evidence.boundary || {};
  const isTemplateSwitch = action.includes('switch_parameter_template');
  const evidenceText = isTemplateSwitch
    ? String(templateDisplay.evidence_text || '')
    : evidence.sample_count
      ? `样本 ${evidence.sample_count} 条，平均反馈 ${evidence.avg_reward ?? '--'}。`
      : '当前样本还不多，结论以观察为主。';

  let boundaryScopeLabel = '';
  let boundaryReasonText = '';
  let approvalPathText = '';
  if (isTemplateSwitch) {
    boundaryScopeLabel = String(templateDisplay.boundary_scope_label || '');
    boundaryReasonText = String(templateDisplay.boundary_reason_text || '');
    approvalPathText = String(templateDisplay.approval_path_text || '');
    impactText = String(templateDisplay.impact_text || impactText);
  }

  return {
    actionLabel,
    reasonText,
    impactText,
    evidenceText,
    confidenceText: `${Math.round(confidence * 100)}%`,
    boundaryScopeLabel,
    boundaryReasonText,
    approvalPathText,
    actionStateLabel: '',
    actionStateSummary: '',
    actionStateTargetType: '',
    actionStateTargetId: '',
    actionStateButtonText: '',
  };
}

function resolveSuggestionProgress(item = {}) {
  const backendProgress = item.progress || {};
  return {
    actionStateLabel: backendProgress.state_label || '',
    actionStateSummary: backendProgress.state_summary || '',
    actionStateTargetType: backendProgress.target_type || '',
    actionStateTargetId: backendProgress.target_id || '',
    actionStateButtonText: backendProgress.button_text || '',
  };
}

function describeReview(item = {}) {
  const outcome = String(item.outcome_label || '').toLowerCase();
  const review = item.review || {};
  let outcomeLabel = '中性结果';
  let meaningText = '这笔交易还没有形成明显的经验结论。';

  if (outcome === 'lucky_win') {
    outcomeLabel = '幸运盈利';
    meaningText = '赚到了钱，但系统认为这次更多是运气，不应该立刻放大信心。';
  } else if (outcome === 'good_win') {
    outcomeLabel = '高质量盈利';
    meaningText = '这次盈利和系统判断一致，适合作为正向经验。';
  } else if (outcome === 'good_loss') {
    outcomeLabel = '可接受亏损';
    meaningText = '虽然亏损，但执行过程仍符合规则，不一定要否定策略。';
  } else if (outcome === 'bad_loss') {
    outcomeLabel = '无效亏损';
    meaningText = '这次亏损说明策略或执行环节可能有明显问题。';
  }

  return {
    outcomeLabel,
    meaningText,
    primaryFactorText: humanizeScopeKey(review.top_factor || review.top_weight_factor || ''),
    worstFactorText: humanizeScopeKey(review.worst_factor || ''),
    primaryResponsibilityText: humanizeResponsibility(item.primary_responsibility || review.primary_responsibility || ''),
    responsibilityLabelsText: (item.responsibility_labels || review.responsibility_labels || []).map(humanizeResponsibilityLabel),
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

function humanizeResponsibilityLabel(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'entry_good_exit_bad') return '入场对但退出差';
  if (key === 'alpha_correct_but_capture_failed') return '方向对但利润捕获差';
  if (key === 'holding_too_long') return '持仓过久';
  if (key === 'regime_changed_during_hold') return '持仓期间市场切换';
  if (key === 'factor_logic_ok_but_param_suspect') return '逻辑可用但参数可疑';
  if (key === 'thesis_broken') return '交易 thesis 已失效';
  if (key === 'holding_inefficient') return '持仓效率偏低';
  return value || '未分类';
}

function describeApplication(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const status = String(item.status || 'observing').toLowerCase();
  let actionLabel = '观察生效';
  if (action.includes('downweight')) actionLabel = '降低权重已生效';
  else if (action.includes('boost')) actionLabel = '提高权重已生效';
  else if (action.includes('watch')) actionLabel = '观察策略已记录';
  else if (action.includes('quarantine')) actionLabel = '隔离策略已生效';

  let effectLabel = '观察中';
  let effectText = '系统正在等待更多样本来判断这次调整是否真的有帮助。';
  if (status === 'effective') {
    effectLabel = '已见效果';
    effectText = '应用后表现优于历史基线，这次调整正在起正向作用。';
  } else if (status === 'reinforced') {
    effectLabel = '已增强';
    effectText = '应用后持续有效，系统已经自动追加了一次增强。';
  } else if (status === 'ineffective') {
    effectLabel = '已回退';
    effectText = '应用后效果变差，系统已经自动回滚相关建议。';
  } else if (status === 'mixed') {
    effectLabel = '效果混合';
    effectText = '应用后有变化，但还不足以下明确结论。';
  }

  const observed = Number(item.observed_trade_count || 0);
  const baseline = Number(item.baseline_trade_count || 0);
  const delta = Number(item.delta_avg_reward || 0);
  const postWinRate = Number(item.post_win_rate || 0);
  const baselineWinRate = Number(item.baseline_win_rate || 0);
  const deltaText = `${delta >= 0 ? '+' : ''}${delta.toFixed(3)}`;
  const postWinText = `${Math.round(postWinRate * 100)}%`;
  const baselineWinText = `${Math.round(baselineWinRate * 100)}%`;

  return {
    actionLabel,
    scopeLabel: humanizeScopeKey(item.scope_key),
    impactText: `${item.old_weight} -> ${item.new_weight}，bias ${item.bias_multiplier}`,
    effectLabel,
    effectText,
    effectTone: toneFromStatus(status),
    effectSummary: `后验 ${observed} 笔 / 基线 ${baseline} 笔，reward Δ ${deltaText}`,
    effectStatsText: `胜率 ${postWinText}，基线 ${baselineWinText}`,
    isObservationOnly: action === 'watch',
  };
}

function isMeaningfulApplication(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const bias = Number(item.bias_multiplier || 1);
  const oldWeight = Number(item.old_weight || 0);
  const newWeight = Number(item.new_weight || 0);
  if (action === 'watch') return false;
  if (Math.abs(bias - 1) < 0.000001 && Math.abs(newWeight - oldWeight) < 0.000001) return false;
  return true;
}

function humanizeCandidateStatus(status = '') {
  const key = String(status || '').toLowerCase();
  if (key === 'pending_review') return '待审候选';
  if (key === 'approved') return '已批准';
  if (key === 'rejected') return '已拒绝';
  if (key === 'deployed') return '已发布';
  if (key === 'rolled_back') return '已回滚';
  return status || '未知状态';
}

function humanizeFactorId(value = '') {
  if (!value) return '未命名模板';
  return String(value).replace(/_/g, ' ');
}

function buildLearningGovernanceTodoCard(todo = null) {
  if (!todo) return null;
  const entryType = String(todo.entry_type || '').toLowerCase();
  const recommendationId = String(todo.recommendation_id || '');
  const candidateId = String(todo.candidate_id || '');
  return {
    title: todo.title || '参数治理待办',
    stageTag: todo.stage_tag || '',
    stageTone: todo.stage_tone || 'neutral',
    actionLabel: todo.action_label || '继续推进',
    priorityLabel: todo.priority_label || '',
    prioritySummary: todo.priority_summary || '',
    summary: todo.summary || '继续积累更多治理证据。',
    queueHint: todo.queue_hint || '',
    targetType: todo.target_type || '',
    entryType,
    recommendationId,
    candidateId,
    buttonText:
      entryType === 'candidate'
        ? '查看候选'
        : recommendationId
          ? '查看建议'
          : '',
  };
}

function describeOfflineCandidate(item = {}) {
  const status = String(item.status || '').toLowerCase();
  const governance = item.governance || {};
  let stageText = '等待人审决定是否发布。';
  if (status === 'approved') stageText = '离线证据已通过，等待正式发布。';
  else if (status === 'deployed') stageText = '模板已经进入运行态，继续观察后验效果。';
  else if (status === 'rolled_back') stageText = '候选模板已回滚到发布前版本。';
  else if (status === 'rejected') stageText = '候选模板被拒绝，暂不进入运行态。';
  const priority = governance.priority_label
    ? {
      score: Number(governance.priority_score || 0),
      label: governance.priority_label || '',
      summary: governance.priority_summary || '',
    }
    : { score: 0, label: '', summary: '' };
  const actionButtons = (governance.action_buttons || []).map((entry) => ({
    key: String(entry.key || ''),
    label: String(entry.label || ''),
    tone: String(entry.tone || 'secondary'),
    disabled: !!entry.disabled,
  }));
  return {
    statusLabel: governance.status_label || '',
    statusTone: toneFromStatus(governance.stage_tone || ''),
    factorLabel: humanizeFactorId(item.factor_id),
    templateLabel: String(item.template_id || '').split(':')[1] || item.template_id || '未命名版本',
    evidenceText: governance.evidence_display || '',
    stageText,
    recommendationText: governance.source_summary || '',
    approvalPathText: governance.approval_path_text || '',
    reviewText: governance.review_display || '',
    deploymentText: governance.deployment_display || '',
    rollbackText: governance.rollback_display || '',
    stageText: governance.stage_summary || stageText,
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governancePriorityScore: priority.score,
    governancePriorityLabel: priority.label,
    governancePrioritySummary: priority.summary,
    governanceActionLabel: governance.action_label || '',
    governanceStageLabel: governance.stage_label || governance.status_label || '',
    actionButtons,
  };
}

function describeTemplateRecommendation(item = {}) {
  const responsibility = item.responsibility || {};
  const governance = item.governance || {};
  const labels = (responsibility.responsibility_labels || []).map(humanizeResponsibilityLabel);
  const priority = governance.priority_label
    ? {
      score: Number(governance.priority_score || 0),
      label: governance.priority_label || '',
      summary: governance.priority_summary || '',
    }
    : { score: 0, label: '', summary: '' };
  const actionButtonText = governance.action_button_text || '';
  return {
    factorLabel: humanizeFactorId(item.factor_id),
    templateLabel: String(item.target_template_version || item.target_template_id || '未命名版本'),
    roleLabel: String(item.template_role || 'default'),
    statusLabel: governance.status_label || governance.stage_label || '',
    statusTone: toneFromStatus(governance.stage_tone || ''),
    reasonText: item.reason || '系统识别到参数可疑，建议评估替代模板。',
    responsibilityText: humanizeResponsibility(responsibility.primary_responsibility || ''),
    labelText: labels.join(' / '),
    stageSummary: governance.stage_summary || '',
    actionText: governance.action_summary || '',
    actionButtonText,
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governancePriorityScore: priority.score,
    governancePriorityLabel: priority.label,
    governancePrioritySummary: priority.summary,
    governanceActionLabel: governance.action_label || '',
    actionDoneText: governance.followup_hint || '',
    actionStateLabel: '',
    actionStateSummary: '',
    actionStateDone: false,
    actionStateTargetType: '',
    actionStateTargetId: '',
    actionStateButtonText: '',
  };
}

function resolveRecommendationProgress(item = {}) {
  const backendProgress = item.progress || {};
  return {
    actionStateLabel: backendProgress.state_label || '',
    actionStateSummary: backendProgress.state_summary || '',
    actionStateDone: !!backendProgress.state_done,
    actionStateTargetType: backendProgress.target_type || '',
    actionStateTargetId: backendProgress.target_id || '',
    actionStateButtonText: backendProgress.button_text || '',
  };
}

function showGovernanceFocusMissToast(type = '', pending = {}) {
  const source = String(pending.source || '');
  const sourceText = source === 'trade_trace_timeline' ? '证据链入口' : '治理入口';
  const typeText = type === 'offline_candidate'
    ? '候选'
    : type === 'template_recommendation'
      ? '推荐'
      : type === 'parameter_lifecycle'
        ? '轨迹'
        : type === 'suggestion'
          ? '建议'
          : '对象';
  wx.showToast({
    title: `${sourceText}未找到对应${typeText}`,
    icon: 'none',
    duration: 1800,
  });
}

function findByFactor(items = [], factorId = '') {
  const targetFactor = String(factorId || '');
  if (!targetFactor) return null;
  return (items || []).find((item) => String(item.factor_id || item.factor || '') === targetFactor) || null;
}

function describeLifecycleEvent(item = {}) {
  const trace = ((item.metrics || {}).candidate_trace || {});
  const governance = item.governance || {};
  const candidateId = String(governance.candidate_id || trace.candidate_id || '');
  const recommendationId = String(governance.recommendation_id || trace.recommendation_id || '');
  return {
    factorLabel: humanizeFactorId(item.factor || ''),
    eventLabel: governance.status_label || governance.stage_label || '',
    eventTone: toneFromStatus(governance.stage_tone || ''),
    createdText: formatDateTime(item.ts),
    reasonText: governance.stage_summary || item.reason || item.description || '治理轨迹已记录。',
    recommendationText: governance.source_summary || '',
    approvalPathText: governance.approval_path_text || '',
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governanceActionLabel: governance.action_label || '',
    governanceTargetTypeText: governance.target_type || '',
    linkedCandidateId: candidateId,
    linkedRecommendationId: recommendationId,
    linkedActionText: governance.button_text || governance.action_label || '',
  };
}

Page({
  data: {
    summary: {},
    summaryStatus: 'idle',
    summaryError: '',
    templateOpsSummary: '',
    pendingGovernanceTodoCard: null,
    suggestions: [],
    proposedSuggestions: [],
    approvedSuggestions: [],
    rolledBackSuggestions: [],
    suggestionTab: 'proposed',
    visibleSuggestions: [],
    selectedSuggestion: null,
    reviews: [],
    selectedReview: null,
    allApplications: [],
    applications: [],
    applicationCountDisplay: 0,
    observationApplicationCount: 0,
    selectedApplication: null,
    offlineCandidates: [],
    templateRecommendations: [],
    selectedTemplateRecommendation: null,
    recommendationBusyId: '',
    selectedOfflineCandidate: null,
    offlineCandidateBusyId: '',
    offlineCandidateCountDisplay: 0,
    recommendationCountDisplay: 0,
    parameterTemplateEmptyStates: {
      offline_candidates: '还没有参数模板候选',
      lifecycle: '还没有参数治理轨迹',
      recommendations: '还没有参数模板建议',
    },
    parameterTemplateTaskCards: [],
    latestApplicationExpanded: false,
    closureSteps: [],
    latestApplication: null,
    lifecycleEvents: [],
    selectedLifecycleEvent: null,
    lifecycleCountDisplay: 0,
    governBusy: false,
    updatedAt: '--',
  },

  onLoad() {
    this._unsub = learningStore.subscribe(() => this.syncView());
    this.syncView();
    refreshLearning();
  },

  async onShow() {
    await refreshLearning();
    this.consumeGovernanceFocus();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = learningStore.getState();
    const summary = state.summary || {};
    const summaryStatus = String(state.summaryStatus || 'idle');
    const summaryError = String(state.summaryError || '');
    const parameterTemplateEmptyStates = summary.parameter_template_empty_states || {};
    const parameterTemplateTaskCards = (summary.parameter_template_task_cards || []).map((item) => ({
      id: String(item.id || ''),
      index: String(item.index || ''),
      title: String(item.title || ''),
      note: String(item.note || ''),
      tone: String(item.tone || 'neutral'),
    })).filter((item) => item.id && item.title);
    const rawSuggestions = (state.suggestions || []).map((item) => ({
      ...item,
      tone: toneFromStatus(item.status),
      createdText: formatDateTime(item.created_at),
      scopeLabel: humanizeScopeKey(item.scope_key),
      ...describeSuggestion(item),
    }));
    const reviews = (state.reviews || []).map((item) => ({
      ...item,
      tone: toneFromStatus(item.outcome_label),
      createdText: formatDateTime(item.created_at),
      ...describeReview(item),
    }));
    const allApplications = (state.applications || []).map((item) => ({
      ...item,
      createdText: formatDateTime(item.created_at),
      reviewAtText: formatDateTime(item.last_review_at),
      ...describeApplication(item),
    }));
    const offlineCandidates = sortGovernanceItemsByPriority((state.offlineCandidates || []).map((item) => ({
      ...item,
      createdText: formatDateTime(item.created_at),
      updatedText: formatDateTime(item.updated_at),
      ...describeOfflineCandidate(item),
    })));
    const lifecycleEvents = (state.lifecycle || [])
      .filter((item) => item && item.source === 'parameter_template')
      .map((item) => ({
        ...item,
        ...describeLifecycleEvent(item),
      }));
    const suggestions = rawSuggestions.map((item) => ({
      ...item,
      ...resolveSuggestionProgress(item),
    }));
    const templateRecommendations = sortGovernanceItemsByPriority((state.templateRecommendations || []).map((item) => ({
      ...item,
      ...describeTemplateRecommendation(item),
      ...resolveRecommendationProgress(item),
    })));
    const applications = allApplications.filter((item) => isMeaningfulApplication(item));
    const observationApplicationCount = Math.max(0, allApplications.length - applications.length);
    const pendingGovernanceTodoCard = buildLearningGovernanceTodoCard(summary.parameter_template_todo || null);
    const suggestionTab = this.data.suggestionTab || 'proposed';
    const proposedSuggestions = suggestions.filter((item) => item.status === 'proposed');
    const approvedSuggestions = suggestions.filter((item) => item.status === 'approved');
    const rolledBackSuggestions = suggestions.filter((item) => item.status === 'rolled_back');
    const visibleSuggestions =
      suggestionTab === 'approved'
        ? approvedSuggestions
        : suggestionTab === 'rolled_back'
          ? rolledBackSuggestions
          : proposedSuggestions;
    this.setData({
      summary,
      summaryStatus,
      summaryError,
      templateOpsSummary: String(summary.parameter_template_ops_summary || ''),
      pendingGovernanceTodoCard,
      suggestions,
      proposedSuggestions,
      approvedSuggestions,
      rolledBackSuggestions,
      visibleSuggestions,
      reviews,
      allApplications,
      applications,
      applicationCountDisplay: applications.length,
      observationApplicationCount,
      offlineCandidates,
      lifecycleEvents,
      templateRecommendations,
      parameterTemplateEmptyStates,
      parameterTemplateTaskCards,
      offlineCandidateCountDisplay: offlineCandidates.length,
      lifecycleCountDisplay: lifecycleEvents.length,
      recommendationCountDisplay: templateRecommendations.length,
      closureSteps: [
        {
          id: 'review',
          index: '1',
          title: '平仓复盘',
          note: reviews.length ? `${reviews.length} 条复盘已入库` : '等待平仓样本',
          tone: reviews.length ? 'positive' : 'neutral',
        },
        {
          id: 'suggest',
          index: '2',
          title: '生成建议',
          note: suggestions.length ? `${suggestions.length} 条治理建议` : '尚未形成建议',
          tone: suggestions.length ? 'positive' : 'neutral',
        },
        {
          id: 'approve',
          index: '3',
          title: '治理审批',
          note: proposedSuggestions.length ? `${proposedSuggestions.length} 条待审` : approvedSuggestions.length ? `${approvedSuggestions.length} 条已批` : '暂无审批动作',
          tone: proposedSuggestions.length ? 'warning' : approvedSuggestions.length ? 'positive' : 'neutral',
        },
        {
          id: 'apply',
          index: '4',
          title: '权重应用',
          note: applications.length ? `${applications.length} 次权重应用` : '还未影响运行权重',
          tone: applications.length ? 'positive' : 'neutral',
        },
        {
          id: 'effect',
          index: '5',
          title: '效果追踪',
          note: applications.length
            ? applications[0].effectLabel
            : allApplications.length
              ? '当前以观察记录为主'
              : '等待应用后样本',
          tone: applications.length ? applications[0].effectTone : 'neutral',
        },
        ...parameterTemplateTaskCards,
      ],
      latestApplication: applications[0] || allApplications[0] || null,
      updatedAt: formatDateTime(state.updatedAt),
    });
  },

  openPendingGovernanceTodo() {
    const todo = this.data.pendingGovernanceTodoCard || null;
    if (!todo) return;
    if (todo.entryType === 'candidate' && todo.candidateId) {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(todo.candidateId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (todo.recommendationId) {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(todo.recommendationId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
    }
  },

  async onRunGovernance() {
    if (this.data.governBusy) return;
    this.setData({ governBusy: true });
    try {
      const result = await runLearningGovernance();
      wx.showToast({
        title: String((result && result.result_label) || '处理完成'),
        icon: 'none',
        duration: 2200,
      });
    } finally {
      this.setData({ governBusy: false });
    }
  },

  async approveSuggestion(e) {
    const id = e.currentTarget.dataset.id;
    const result = await reviewSuggestion(id, 'approved', 'manual approve from mini-program');
    wx.showToast({
      title: String((result && result.result_label) || '处理完成'),
      icon: 'none',
      duration: 2000,
    });
  },

  async rejectSuggestion(e) {
    const id = e.currentTarget.dataset.id;
    const result = await reviewSuggestion(id, 'rejected', 'manual reject from mini-program');
    wx.showToast({
      title: String((result && result.result_label) || '处理完成'),
      icon: 'none',
      duration: 2000,
    });
  },

  switchSuggestionTab(e) {
    const tab = e.currentTarget.dataset.tab;
    this.setData({ suggestionTab: tab }, () => this.syncView());
  },

  openSuggestionDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.suggestions || []).find((x) => x.suggestion_id === id) || null;
    this.setData({ selectedSuggestion: item });
  },

  openSuggestionProgressTarget() {
    const item = this.data.selectedSuggestion || null;
    if (!item || !item.actionStateTargetType || !item.actionStateTargetId) return;
    if (item.actionStateTargetType === 'recommendation') {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(item.actionStateTargetId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
      return;
    }
    if (item.actionStateTargetType === 'candidate') {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.actionStateTargetId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
    }
  },

  openReviewDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.reviews || []).find((x) => x.review_id === id) || null;
    this.setData({ selectedReview: item });
  },

  openApplicationDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.allApplications || []).find((x) => x.application_id === id) || null;
    this.setData({ selectedApplication: item });
  },

  openOfflineCandidateDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.offlineCandidates || []).find((x) => x.candidate_id === id) || null;
    this.setData({ selectedOfflineCandidate: item });
  },

  openTemplateRecommendationDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.templateRecommendations || []).find((x) => x.recommendation_id === id) || null;
    this.setData({ selectedTemplateRecommendation: item });
  },

  openLifecycleDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.lifecycleEvents || []).find((x) => String(x.id) === String(id)) || null;
    this.setData({ selectedLifecycleEvent: item });
  },

  async materializeTemplateRecommendation(e) {
    const id = e.currentTarget.dataset.id;
    if (!id || this.data.recommendationBusyId) return;
    this.setData({ recommendationBusyId: id });
    try {
      const result = await materializeTemplateRecommendation(id, 'materialize from learning page');
      wx.showToast({
        title: result && result.ok
          ? String(result.result_label || '处理完成')
          : '生成失败',
        icon: 'none',
        duration: 2000,
      });
      const refreshed = (learningStore.getState().templateRecommendations || []).find((x) => x.recommendation_id === id) || null;
      this.setData({ selectedTemplateRecommendation: refreshed });
    } finally {
      this.setData({ recommendationBusyId: '' });
    }
  },

  openRecommendationProgressTarget() {
    const item = this.data.selectedTemplateRecommendation || null;
    if (!item || !item.actionStateTargetType || !item.actionStateTargetId) return;
    if (item.actionStateTargetType === 'candidate') {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.actionStateTargetId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (item.actionStateTargetType === 'suggestion') {
      const suggestion = (this.data.suggestions || []).find(
        (entry) => String(entry.suggestion_id) === String(item.actionStateTargetId)
      ) || null;
      if (suggestion) this.setData({ selectedSuggestion: suggestion });
      return;
    }
    if (item.actionStateTargetType === 'lifecycle') {
      const lifecycle = (this.data.lifecycleEvents || []).find(
        (entry) => String(entry.id) === String(item.actionStateTargetId)
      ) || null;
      if (lifecycle) this.setData({ selectedLifecycleEvent: lifecycle });
    }
  },

  openLifecycleProgressTarget() {
    const item = this.data.selectedLifecycleEvent || null;
    if (!item) return;
    if (item.linkedCandidateId) {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.linkedCandidateId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (item.linkedRecommendationId) {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(item.linkedRecommendationId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
    }
  },

  async actOnOfflineCandidate(e) {
    const candidateId = String((e.currentTarget.dataset && e.currentTarget.dataset.id) || '');
    const action = String((e.currentTarget.dataset && e.currentTarget.dataset.action) || '');
    if (!candidateId || !action || this.data.offlineCandidateBusyId) return;
    this.setData({ offlineCandidateBusyId: candidateId });
    try {
      let result = null;
      if (action === 'approve') {
        result = await reviewOfflineCandidate(candidateId, 'approved', 'approved from learning page');
      } else if (action === 'reject') {
        result = await reviewOfflineCandidate(candidateId, 'rejected', 'rejected from learning page');
      } else if (action === 'release') {
        result = await releaseOfflineCandidate(candidateId, 'release from learning page');
      } else if (action === 'rollback') {
        result = await rollbackOfflineCandidate(candidateId, 'rollback from learning page');
      } else {
        return;
      }
      const refreshed = (learningStore.getState().offlineCandidates || []).find((x) => x.candidate_id === candidateId) || null;
      this.setData({ selectedOfflineCandidate: refreshed });
      wx.showToast({
        title: result && result.blocked
          ? String(result.result_label || '当前动作被阻断')
          : String((result && result.result_label) || '处理完成'),
        icon: 'none',
        duration: 2200,
      });
    } finally {
      this.setData({ offlineCandidateBusyId: '' });
    }
  },

  toggleLatestApplicationDetail() {
    this.setData({ latestApplicationExpanded: !this.data.latestApplicationExpanded });
  },

  closeSuggestionDetail() {
    this.setData({ selectedSuggestion: null });
  },

  closeReviewDetail() {
    this.setData({ selectedReview: null });
  },

  closeApplicationDetail() {
    this.setData({ selectedApplication: null });
  },

  closeOfflineCandidateDetail() {
    this.setData({ selectedOfflineCandidate: null });
  },

  closeTemplateRecommendationDetail() {
    this.setData({ selectedTemplateRecommendation: null });
  },

  closeLifecycleDetail() {
    this.setData({ selectedLifecycleEvent: null });
  },

  consumeGovernanceFocus() {
    const pending = consumeLearningGovernanceFocus();
    if (!pending) return;
    const type = String(pending.type || '');
    if (type === 'suggestion') {
      const item = (this.data.suggestions || []).find(
        (x) => String(x.suggestion_id) === String(pending.suggestionId || '')
      ) || findByFactor(this.data.suggestions || [], pending.factorId);
      if (item) {
        this.setData({ selectedSuggestion: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'template_recommendation') {
      const item = (this.data.templateRecommendations || []).find(
        (x) => String(x.recommendation_id) === String(pending.recommendationId || '')
      ) || findByFactor(this.data.templateRecommendations || [], pending.factorId);
      if (item) {
        this.setData({ selectedTemplateRecommendation: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'offline_candidate') {
      const item = (this.data.offlineCandidates || []).find(
        (x) => String(x.candidate_id) === String(pending.candidateId || '')
      ) || findByFactor(this.data.offlineCandidates || [], pending.factorId);
      if (item) {
        this.setData({ selectedOfflineCandidate: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'parameter_lifecycle') {
      const item = (this.data.lifecycleEvents || []).find(
        (x) => String(x.id) === String(pending.lifecycleEventId || '')
      ) || findByFactor(this.data.lifecycleEvents || [], pending.factorId);
      if (item) {
        this.setData({ selectedLifecycleEvent: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
    }
  },

  openTraceFromReview() {
    const review = this.data.selectedReview || null;
    const locator = (review && review.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || review.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromLifecycle() {
    const event = this.data.selectedLifecycleEvent || null;
    const locator = (((event || {}).metrics || {}).candidate_trace || {}).trace_locator || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromTemplateRecommendation() {
    const item = this.data.selectedTemplateRecommendation || null;
    const locator = (item && item.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromOfflineCandidate() {
    const item = this.data.selectedOfflineCandidate || null;
    const locator = (item && item.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },
});
