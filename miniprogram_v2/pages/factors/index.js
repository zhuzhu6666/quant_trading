import systemStore from '../../stores/system';
import { refreshFactorDomain } from '../../services/factors';
import learningStore from '../../stores/learning';
import { refreshLearning } from '../../services/learning';
import { formatDateTime } from '../../utils/format';

function humanizeScopeKey(scopeKey = '') {
  if (!scopeKey) return '未命名因子';
  return String(scopeKey)
    .replace(/^dsl_auto_/, 'DSL 自动因子 ')
    .replace(/_/g, ' ');
}

function truncateText(value, max = 24) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}...` : text;
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

  return {
    title,
    tone,
    note,
    noteCompact: truncateText(note, 44),
    factorText: humanizeScopeKey(item.factor || ''),
    factorCompactText: truncateText(humanizeScopeKey(item.factor || ''), 18),
    stageText: item.next_stage || item.status || item.source || '--',
    metricText: metricBits.join(' · '),
  };
}

Page({
  data: {
    rows: [],
    summary: null,
    health: null,
    sortMode: 'weight',
    selectedRow: null,
    topContributors: [],
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
    const rows = weights.map((item) => {
      const name = item.factor;
      const stat = stats[name] || {};
      return {
        factor: name,
        weight: Number(item.new || 0),
        avg_mc: stat.avg_mc || 0,
        win_rate: stat.win_rate || 0,
        trades: stat.n_trades || 0,
        total_mc: stat.total_mc || 0,
        sharpe: stat.composite_sharpe_score || 0,
      };
    });
    const sortMode = this.data.sortMode || 'weight';
    const sortedRows = this.sortRows(rows, sortMode).slice(0, 24);
    const selectedRow = this.resolveSelectedRow(sortedRows, this.data.selectedRow && this.data.selectedRow.factor);
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
    const lifecycleExpanded = !!this.data.lifecycleExpanded;
    const visibleLifecycle = lifecycleExpanded ? filteredLifecycle : filteredLifecycle.slice(0, 4);
    this.setData({
      rows: sortedRows.map((item, index) => ({
        ...item,
        rank: index + 1,
      })),
      summary: systemState.factorStats && systemState.factorStats.summary,
      health: systemState.factorHealth,
      selectedRow: selectedRow
        ? {
            ...selectedRow,
            rank: (sortedRows.findIndex((item) => item.factor === selectedRow.factor) || 0) + 1,
          }
        : null,
      topContributors: (systemState.factorStats && systemState.factorStats.summary && systemState.factorStats.summary.top_contributors) || [],
      lifecycle,
      lifecycleSummary,
      lifecycleFilteredCount: filteredLifecycle.length,
      visibleLifecycle,
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
});
