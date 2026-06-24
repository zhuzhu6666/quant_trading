import learningStore from '../../stores/learning';
import { refreshLearning, reviewSuggestion, runLearningGovernance } from '../../services/learning';
import { formatDateTime, toneFromStatus } from '../../utils/format';

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
    applications: [],
    selectedApplication: null,
    closureSteps: [],
    latestApplication: null,
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
    }));
    const reviews = (state.reviews || []).map((item) => ({
      ...item,
      tone: toneFromStatus(item.outcome_label),
      createdText: formatDateTime(item.created_at),
    }));
    const applications = (state.applications || []).map((item) => ({
      ...item,
      createdText: formatDateTime(item.created_at),
    }));
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
      applications,
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
      ],
      latestApplication: applications[0] || null,
      updatedAt: formatDateTime(state.updatedAt),
    });
  },

  async onRunGovernance() {
    await runLearningGovernance();
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
    const item = (this.data.applications || []).find((x) => x.application_id === id) || null;
    this.setData({ selectedApplication: item });
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
