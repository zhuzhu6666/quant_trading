import learningStore from '../../stores/learning';
import { refreshLearning, reviewSuggestion, runLearningGovernance } from '../../services/learning';
import { formatDateTime, toneFromStatus } from '../../utils/format';

function humanizeScopeKey(scopeKey = '') {
  if (!scopeKey) return '未命名因子';
  return String(scopeKey)
    .replace(/^dsl_auto_/, 'DSL 自动因子 ')
    .replace(/_/g, ' ');
}

function describeSuggestion(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const evidence = item.evidence || {};
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
  }

  if (reasonText.includes('still accumulating evidence')) {
    reasonText = '最近有表现，但证据还不够强，系统先继续观察。';
  }

  const evidenceText = evidence.sample_count
    ? `样本 ${evidence.sample_count} 条，平均反馈 ${evidence.avg_reward ?? '--'}。`
    : '当前样本还不多，结论以观察为主。';

  return {
    actionLabel,
    reasonText,
    impactText,
    evidenceText,
    confidenceText: `${Math.round(confidence * 100)}%`,
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
  };
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

Page({
  data: {
    summary: {},
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
    latestApplicationExpanded: false,
    closureSteps: [],
    latestApplication: null,
    governBusy: false,
    updatedAt: '--',
  },

  onLoad() {
    this._unsub = learningStore.subscribe(() => this.syncView());
    this.syncView();
    refreshLearning();
  },

  onShow() {
    refreshLearning();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = learningStore.getState();
    const summary = state.summary || {};
    const suggestions = (state.suggestions || []).map((item) => ({
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
    const applications = allApplications.filter((item) => isMeaningfulApplication(item));
    const observationApplicationCount = Math.max(0, allApplications.length - applications.length);
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
      ],
      latestApplication: applications[0] || allApplications[0] || null,
      updatedAt: formatDateTime(state.updatedAt),
    });
  },

  async onRunGovernance() {
    if (this.data.governBusy) return;
    this.setData({ governBusy: true });
    try {
      const result = await runLearningGovernance();
      wx.showToast({
        title: result.auto_actions ? `已处理 ${result.auto_actions} 条` : '没有新动作',
        icon: 'none',
        duration: 2200,
      });
    } finally {
      this.setData({ governBusy: false });
    }
  },

  async approveSuggestion(e) {
    const id = e.currentTarget.dataset.id;
    await reviewSuggestion(id, 'approved', 'manual approve from mini-program');
  },

  async rejectSuggestion(e) {
    const id = e.currentTarget.dataset.id;
    await reviewSuggestion(id, 'rejected', 'manual reject from mini-program');
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
});
