import systemStore from '../../stores/system';
import { refreshFactorDomain } from '../../services/factors';
import learningStore from '../../stores/learning';
import { openLearningGovernancePage, refreshLearning } from '../../services/learning';
import { openTradeTracePage } from '../../services/ops';
import { formatDateTime, formatCount } from '../../utils/format';
import {
  describeGovernanceStageSummary,
} from '../../utils/governance';

function humanizeScopeKey(scopeKey = '') {
  if (!scopeKey) return '未命名因子';
  return String(scopeKey)
    .replace(/^dsl_auto_/, 'DSL 自动因子 ')
    .replace(/_/g, ' ');
}

const FACTOR_HINT_MAP = {
  cot: '持仓/订单流线索',
  gld: '黄金 ETF/库存线索',
  cb: '宏观/债券变化线索',
  unknown: '策略因子',
};

function truncateText(value, max = 24) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}...` : text;
}

function describeFactorHint(factor = '') {
  const key = String(factor || '').toLowerCase();
  if (key.startsWith('cot')) return FACTOR_HINT_MAP.cot;
  if (key.startsWith('gld')) return FACTOR_HINT_MAP.gld;
  if (key.startsWith('cb')) return FACTOR_HINT_MAP.cb;
  const match = key.match(/^([a-z0-9_]+?)(?:_|$)/);
  if (!match) return FACTOR_HINT_MAP.unknown;
  const prefix = match[1];
  return FACTOR_HINT_MAP[prefix] || FACTOR_HINT_MAP.unknown;
}

function formatMetric(value, digits = 4) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '--';
  return n.toFixed(digits);
}

function formatWinRate(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '--';
  return `${(n * 100).toFixed(2)}%`;
}

function buildFactorRow(item, stat = {}) {
  const factor = item.factor;
  const weight = Number(item.new || 0);
  const avgMc = Number(stat.avg_mc || 0);
  const winRate = Number(stat.win_rate || 0);
  const trades = Number(stat.n_trades || 0);
  const totalMc = Number(stat.total_mc || 0);
  const sharpe = Number(stat.composite_sharpe_score || 0);
  const hasSamples = trades > 0;

  return {
    factor,
    factorHint: describeFactorHint(factor),
    weight,
    weightText: formatMetric(weight),
    avg_mc: avgMc,
    avgMcText: formatMetric(avgMc),
    win_rate: winRate,
    winRateText: formatWinRate(winRate),
    trades,
    tradesText: formatCount(trades),
    total_mc: totalMc,
    totalMcText: formatMetric(totalMc),
    sharpe,
    hasSamples,
    noSampleTip: hasSamples ? '' : '还没有真实样本，先不要评价好坏',
    hasContribution: hasSamples && (Math.abs(avgMc) > 0 || Math.abs(totalMc) > 0),
  };
}

function buildTodayInsight(rows = []) {
  const first = rows[0];
  if (!first) {
    return {
      hasData: false,
      driverFactor: '--',
      driverFactorHint: '策略因子',
      sampleText: '还没有真实样本',
      contributionState: '默认权重',
      explainText: '当前还没有真实样本，先不要评价好坏。',
      tipText: '',
      hasSamples: false,
    };
  }
  const hasSamples = first.hasSamples;
  const contributionState = hasSamples
    ? (first.hasContribution ? '有样本贡献' : '有样本但还没贡献')
    : '默认权重';
  const sampleText = hasSamples ? `有 ${first.tradesText} 个真实样本` : '还没有真实样本';
  const explainText = hasSamples
    ? (first.hasContribution ? `当前主要驱动因子是“${first.factor}”（${first.factorHint}），它在真实样本下有可见贡献。`
      : `当前主要驱动因子是“${first.factor}”（${first.factorHint}），但有真实样本下还没贡献。`)
    : `当前主要驱动因子是“${first.factor}”（${first.factorHint}），还没有真实样本，先不要评价好坏。`;

  return {
    hasData: true,
    driverFactor: first.factor,
    driverFactorHint: first.factorHint,
    sampleText,
    contributionState,
    explainText,
    tipText: hasSamples ? '' : first.noSampleTip,
    hasSamples,
  };
}

function describeLifecycle(item = {}) {
  const event = String(item.event || '').toLowerCase();
  const status = String(item.status || '').toUpperCase();
  const metrics = item.metrics || {};
  let title = '生命周期事件';
  let tone = 'neutral';
  let note = item.reason || '系统记录了一次因子状态变化。';

  if (event === 'register') {
    title = '进入候选池';
    tone = 'info';
    note = '新因子已注册，先进入影子观察。';
  } else if (event === 'promote') {
    title = status === 'ACTIVE' ? '晋升为正式因子' : '通过晋升检查';
    tone = 'positive';
    note = item.next_stage ? `状态推进到 ${item.next_stage}` : (item.reason || '因子正在通过晋升链路。');
  } else if (event === 'qualify') {
    title = '满足下一阶段条件';
    tone = 'positive';
    note = item.next_stage ? `已达到 ${item.next_stage} 条件，等待进入下一步。` : '已满足晋升条件。';
  } else if (event === 'stay') {
    title = '继续观察';
    tone = 'warning';
    note = item.reason || '证据还不够，系统继续观察。';
  } else if (event === 'rollback') {
    title = '回滚观察';
    tone = 'warning';
    note = item.reason || '表现不稳定，系统退回观察阶段。';
  } else if (event === 'quarantine') {
    title = '隔离处理';
    tone = 'danger';
    note = item.reason || '因子被隔离，暂停进一步使用。';
  } else if (event === 'retire' || event === 'unregister') {
    title = '退出主流程';
    tone = 'danger';
    note = item.reason || '因子已从当前流程移除。';
  } else if (event === 'unquarantine' || event === 'unretire') {
    title = '恢复观察';
    tone = 'info';
    note = item.reason || '因子重新回到观察队列。';
  }

  const metricBits = [];
  if (typeof item.oos_bars === 'number' && item.oos_bars > 0) metricBits.push(`bars ${item.oos_bars}`);
  if (typeof item.oos_pnl === 'number' && item.oos_pnl) metricBits.push(`pnl ${item.oos_pnl.toFixed(4)}`);
  if (typeof metrics.hit_rate === 'number' && metrics.hit_rate > 0) metricBits.push(`胜率 ${Math.round(metrics.hit_rate * 100)}%`);
  if (typeof metrics.health_score === 'number' && metrics.health_score > 0) metricBits.push(`健康 ${metrics.health_score.toFixed(1)}`);
  if (typeof metrics.independence_score === 'number' && metrics.independence_score > 0) metricBits.push(`独立 ${metrics.independence_score.toFixed(1)}`);
  const candidateTrace = (metrics.candidate_trace || {});
  const governance = item.governance || {};
  const linkedCandidateId = String(governance.candidate_id || candidateTrace.candidate_id || '');
  const linkedRecommendationId = String(governance.recommendation_id || candidateTrace.recommendation_id || '');
  const governanceStageTag = String(governance.stage_label || '');
  const governanceStageSummary = String(
    governance.stage_summary
    || governance.next_step_summary
    || (governanceStageTag ? describeGovernanceStageSummary(governanceStageTag) : '')
  );
  const governanceTargetTypeText = String(governance.target_type || '');
  const governanceActionLabel = String(governance.action_label || '');
  const lifecycleJumpType = String(governance.jump_type || '');

  return {
    title,
    tone,
    note,
    noteCompact: truncateText(note, 44),
    factorText: humanizeScopeKey(item.factor || ''),
    factorCompactText: truncateText(humanizeScopeKey(item.factor || ''), 18),
    stageText: item.next_stage || item.status || item.source || '--',
    metricText: metricBits.join(' · '),
    governanceStageTag,
    governanceStageSummary,
    governanceTargetTypeText,
    governanceActionLabel,
    lifecycleJumpType,
    linkedCandidateId,
    linkedRecommendationId,
  };
}

Page({
  data: {
    rows: [],
    summary: null,
    health: null,
    sortMode: 'weight',
    factorPanel: '',
    factorPanelTitle: '',
    factorPanelSubtitle: '',
    selectedRow: null,
    previewRows: [],
    topContributors: [],
    previewTopContributors: [],
    todayInsight: null,
    lifecycle: [],
    lifecycleSummary: {
      promote: 0,
      watch: 0,
      risk: 0,
    },
    lifecycleTab: 'all',
    lifecycleExpanded: false,
    lifecycleFilteredCount: 0,
    visibleLifecycle: [],
    previewLifecycle: [],
    selectedLifecycle: null,
  },

  onLoad() {
    this._unsubSystem = systemStore.subscribe(() => this.syncView());
    this._unsubLearning = learningStore.subscribe(() => this.syncView());
    this.syncView();
    refreshFactorDomain();
    refreshLearning();
  },

  onShow() {
    refreshFactorDomain();
    refreshLearning();
  },

  onUnload() {
    this._unsubSystem && this._unsubSystem();
    this._unsubLearning && this._unsubLearning();
  },

  syncView() {
    const systemState = systemStore.getState();
    const learningState = learningStore.getState();
    const weights = systemState.factorWeights || [];
    const stats = (systemState.factorStats && systemState.factorStats.per_factor) || {};
    const rows = weights.map((item) => buildFactorRow(item, stats[item.factor] || {}));
    const sortMode = this.data.sortMode || 'weight';
    const sortedRows = this.sortRows(rows, sortMode).slice(0, 24);
    const selectedFactor = this.data.selectedRow && this.data.selectedRow.factor;
    const selectedRow = selectedFactor ? this.resolveSelectedRow(sortedRows, selectedFactor) : null;
    const lifecycle = (learningState.lifecycle || []).map((item, index) => ({
      ...item,
      id: item.id || `${item.kind || 'life'}-${item.factor || 'factor'}-${item.event || 'event'}-${item.ts || index}`,
      createdText: formatDateTime(item.ts),
      ...describeLifecycle(item),
    }));
    const lifecycleTab = this.data.lifecycleTab || 'all';
    const lifecycleSummary = lifecycle.reduce(
      (acc, item) => {
        if (item.tone === 'positive') acc.promote += 1;
        else if (item.tone === 'danger') acc.risk += 1;
        else acc.watch += 1;
        return acc;
      },
      { promote: 0, watch: 0, risk: 0 },
    );
    const filteredLifecycle = lifecycle.filter((item) => {
      if (lifecycleTab === 'promote') return item.tone === 'positive';
      if (lifecycleTab === 'watch') return item.tone === 'warning' || item.tone === 'info' || item.tone === 'neutral';
      if (lifecycleTab === 'risk') return item.tone === 'danger';
      return true;
    });
    const visibleLifecycle = filteredLifecycle;
    const sortedForInsight = this.sortRows(rows, 'weight');
    const todayInsight = buildTodayInsight(sortedForInsight);
    const rankedRows = sortedRows.map((item, index) => ({
      ...item,
      rank: index + 1,
    }));
    const topContributors = (((systemState.factorStats || {}).summary || {}).top_contributors || []).map((item = {}) => ({
      ...item,
      factor: item.name || item.factor || '',
      factorHint: describeFactorHint(item.name || item.factor || ''),
      avgMcText: formatMetric(item.avg_mc || 0),
      winRateText: formatWinRate(item.win_rate || 0),
    }));

    this.setData({
      rows: rankedRows,
      previewRows: rankedRows.slice(0, 1),
      summary: systemState.factorStats && systemState.factorStats.summary,
      health: systemState.factorHealth,
      todayInsight,
      selectedRow: selectedRow
        ? {
            ...selectedRow,
            rank: (sortedRows.findIndex((item) => item.factor === selectedRow.factor) || 0) + 1,
          }
        : null,
      topContributors,
      previewTopContributors: topContributors.slice(0, 1),
      lifecycle,
      lifecycleSummary,
      lifecycleFilteredCount: filteredLifecycle.length,
      visibleLifecycle,
      previewLifecycle: visibleLifecycle.slice(0, 1),
    });
  },

  sortRows(rows, mode) {
    const list = [...rows];
    const key = mode === 'avg_mc' ? 'avg_mc' : mode === 'win_rate' ? 'win_rate' : mode === 'trades' ? 'trades' : 'weight';
    return list.sort((a, b) => Number(b[key] || 0) - Number(a[key] || 0));
  },

  resolveSelectedRow(rows, factor) {
    if (factor) {
      const found = rows.find((item) => item.factor === factor);
      if (found) return found;
    }
    return rows[0] || null;
  },

  changeSortMode(e) {
    const mode = e.currentTarget.dataset.mode;
    this.setData({ sortMode: mode }, () => this.syncView());
  },

  openFactorDetail(e) {
    const factor = e.currentTarget.dataset.factor;
    const row = (this.data.rows || []).find((item) => item.factor === factor) || null;
    this.setData({ selectedRow: row });
  },

  closeFactorDetail() {
    this.setData({ selectedRow: null });
  },

  openFactorPanel(e) {
    const panel = String((e.currentTarget.dataset && e.currentTarget.dataset.panel) || '');
    const titles = {
      factors: ['核心因子', '按当前排序查看完整因子列表'],
      lifecycle: ['因子生命周期', '查看晋升、观察、风险事件'],
      contributors: ['主要贡献来源', '最近贡献排行，不等于永久有效'],
    };
    if (!titles[panel]) return;
    this.setData({
      factorPanel: panel,
      factorPanelTitle: titles[panel][0],
      factorPanelSubtitle: titles[panel][1],
    });
  },

  closeFactorPanel() {
    this.setData({
      factorPanel: '',
      factorPanelTitle: '',
      factorPanelSubtitle: '',
    });
  },

  noop() {},

  switchLifecycleTab(e) {
    const tab = e.currentTarget.dataset.tab;
    this.setData({ lifecycleTab: tab, lifecycleExpanded: false }, () => this.syncView());
  },

  toggleLifecycleExpanded() {
    this.setData({ lifecycleExpanded: !this.data.lifecycleExpanded }, () => this.syncView());
  },

  openLifecycleDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.lifecycle || []).find((x) => x.id === id) || null;
    this.setData({ selectedLifecycle: item });
  },

  closeLifecycleDetail() {
    this.setData({ selectedLifecycle: null });
  },

  openLifecycleTrace() {
    const item = this.data.selectedLifecycle || null;
    const locator = ((((item || {}).metrics || {}).candidate_trace || {}).trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openLifecycleGovernance() {
    const item = this.data.selectedLifecycle || null;
    if (!item) return;
    if (item.lifecycleJumpType === 'offline_candidate' || item.linkedCandidateId) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId: item.linkedCandidateId,
        factorId: item.factor || '',
      });
      return;
    }
    if (item.lifecycleJumpType === 'template_recommendation' || item.linkedRecommendationId) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: item.linkedRecommendationId,
        factorId: item.factor || '',
      });
    }
  },
});
