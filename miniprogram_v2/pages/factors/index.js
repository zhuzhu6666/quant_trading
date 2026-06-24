import systemStore from '../../stores/system';
import { refreshFactorDomain } from '../../services/factors';

Page({
  data: {
    rows: [],
    summary: null,
    health: null,
    sortMode: 'weight',
    selectedRow: null,
    topContributors: [],
  },

  onLoad() {
    this._unsub = systemStore.subscribe(() => this.syncView());
    this.syncView();
    refreshFactorDomain();
  },

  onShow() {
    refreshFactorDomain();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = systemStore.getState();
    const weights = state.factorWeights || [];
    const stats = (state.factorStats && state.factorStats.per_factor) || {};
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
    this.setData({
      rows: sortedRows.map((item, index) => ({
        ...item,
        rank: index + 1,
      })),
      summary: state.factorStats && state.factorStats.summary,
      health: state.factorHealth,
      selectedRow: selectedRow
        ? {
            ...selectedRow,
            rank: (sortedRows.findIndex((item) => item.factor === selectedRow.factor) || 0) + 1,
          }
        : null,
      topContributors: (state.factorStats && state.factorStats.summary && state.factorStats.summary.top_contributors) || [],
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
});
