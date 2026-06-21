import api from '../../utils/api';
import CONFIG from '../../utils/config';

const FRESHNESS_MAP = {
  fresh:  { dot: 'dot-green',  label: '实时',  cls: 'text-green' },
  recent: { dot: 'dot-green',  label: '正常',  cls: 'text-green' },
  stale:  { dot: 'dot-orange', label: '延迟',  cls: 'text-orange' },
  old:    { dot: 'dot-red',    label: '过旧',  cls: 'text-red' },
  missing:{ dot: 'dot-gray',   label: '缺失',  cls: 'text-gray' },
  unknown:{ dot: 'dot-gray',   label: '未知',  cls: 'text-gray' },
};

Page({
  data: {
    pipelineRunning: false,
    evolution: { hasData: false, summary: '', time: '', detail: [] },
    server: CONFIG.SERVER,
    // db health
    dbOverall: '',
    dbOverallCls: 'text-gray',
    dbSummary: '',
    dbList: [],
    dbCheckedAt: '',
  },

  onLoad() { this._fetch(); },
  onShow() { this._fetch(); },

  onGlobalStateUpdate() {
    const loop = getApp().globalData.closedLoop;
    this.setData({ pipelineRunning: !!(loop && loop.pipeline_active) });
  },

  async _fetch() {
    const loop = getApp().globalData.closedLoop;
    this.setData({ pipelineRunning: !!(loop && loop.pipeline_active) });
    await Promise.all([this._fetchEvolution(), this._fetchDbHealth()]);
  },

  async _fetchEvolution() {
    const d = await api.get('/api/control/evolution/latest');
    if (!d || !d.ts) return;

    const gp = d.gp_new_candidates || 0;
    const shadow = d.gp_registered_shadow || 0;
    const oos = d.oos_passed || 0;
    const promotions = d.canary_promotions || [];
    const rollbacks = d.canary_rollbacks || [];
    const retires = d.retire_candidates || [];
    const weightUpd = d.weights_updated;
    const dur = d.duration_sec || 0;
    const err = d.error || '';

    let summary = `GP 生成 ${gp} 候选`;
    if (shadow > 0) summary += `, 注册 ${shadow}`;
    if (oos > 0) summary += `, OOS 通过 ${oos}`;
    if (promotions.length) summary += `, 晋升 ${promotions.length}`;
    if (rollbacks.length) summary += `, 回滚 ${rollbacks.length}`;
    if (retires.length) summary += `, 退役 ${retires.length}`;
    summary += ` | ${weightUpd ? '权重已更新' : '权重未变'}`;
    summary += ` | 耗时 ${dur}s`;
    if (err) summary += ` | ⚠️ ${err}`;

    const detail = [];
    if (promotions.length) detail.push({ label: '晋升', value: promotions.join(', '), cls: 'text-green' });
    if (rollbacks.length) detail.push({ label: '回滚', value: rollbacks.join(', '), cls: 'text-red' });
    if (retires.length) detail.push({ label: '退役', value: retires.join(', ') + (d.retire_reason ? ` (${d.retire_reason})` : ''), cls: 'text-gray' });

    this.setData({
      'evolution.hasData': true,
      'evolution.summary': summary,
      'evolution.detail': detail,
      'evolution.time': d.ts_iso ? new Date(d.ts_iso).toLocaleString('zh-CN') : '',
    });
  },

  async _fetchDbHealth() {
    const d = await api.get('/api/system/db-health');
    if (!d || !d.ok) return;

    const overallCls = {
      healthy: 'text-green', degraded: 'text-orange', stale: 'text-red'
    }[d.overall] || 'text-gray';
    const s = d.summary || {};

    this.setData({
      dbOverall: d.overall === 'healthy' ? '健康' : d.overall === 'degraded' ? '降级' : '延迟',
      dbOverallCls: overallCls,
      dbSummary: `${s.fresh || 0}正常 / ${s.stale || 0}延迟 / ${s.missing || 0}缺失`,
      dbList: (d.databases || []).map(function(db) {
        const fm = FRESHNESS_MAP[db.freshness] || FRESHNESS_MAP.unknown;
        const tables = db.tables || [];
        const mainTable = tables.length > 0 ? tables[0] : null;
        const ageText = mainTable && mainTable.latest_ts
          ? _fmtAge(Date.now() / 1000 - mainTable.latest_ts)
          : '—';
        return {
          name: db.name,
          file: db.file,
          size: db.size,
          rows: _fmtNum(db.total_rows),
          freshness: fm.label,
          freshnessCls: fm.cls,
          freshnessDot: fm.dot,
          ageText: ageText,
          tableCount: tables.length,
          hasError: (db.errors || []).length > 0,
        };
      }),
      dbCheckedAt: _fmtTime(Date.now()),
    });
  },
});

function _fmtAge(sec) {
  if (sec < 60) return Math.round(sec) + 's前';
  if (sec < 3600) return Math.round(sec / 60) + 'm前';
  if (sec < 86400) return Math.round(sec / 3600) + 'h前';
  return Math.round(sec / 86400) + 'd前';
}

function _fmtNum(n) {
  if (!n) return '0';
  if (n >= 1e8) return (n / 1e8).toFixed(1) + '亿';
  if (n >= 1e4) return (n / 1e4).toFixed(1) + '万';
  return n.toLocaleString();
}

function _fmtTime(ts) {
  const d = new Date(ts);
  const pad = n => String(n).padStart(2, '0');
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
