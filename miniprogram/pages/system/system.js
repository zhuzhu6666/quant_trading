import api from '../../utils/api';
import CONFIG from '../../utils/config';

var FRESHNESS_MAP = {
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
    dbOverall: '', dbOverallCls: 'text-gray', dbSummary: '', dbList: [], dbCheckedAt: '',
    // v5: 服务器健康
    serverStatus: '',
    serverStatusCls: 'text-gray',
    serverUptime: '',
    serverDb: '',
    // v5: 因子健康摘要
    factorHealth: { hasData: false, total: 0, healthy: 0, watch: 0, decaying: 0, dead: 0, top: [] },
    // v5: 调度器
    scheduler: { running: false, jobCount: 0, jobs: [] },
  },

  onLoad() { this._fetch(); },
  onShow() { this._fetch(); },

  onGlobalStateUpdate() {
    var loop = getApp().globalData.closedLoop;
    this.setData({ pipelineRunning: !!(loop && loop.pipeline_active) });
  },

  async _fetch() {
    var loop = getApp().globalData.closedLoop;
    this.setData({ pipelineRunning: !!(loop && loop.pipeline_active) });
    await Promise.all([
      this._fetchEvolution(),
      this._fetchDbHealth(),
      this._fetchServerHealth(),
      this._fetchFactorHealth(),
      this._fetchScheduler(),
    ]);
  },

  // ── 进化 ──
  async _fetchEvolution() {
    var d = await api.get('/api/control/evolution/latest');
    if (!d || !d.ts) return;

    var gp = d.gp_new_candidates || 0;
    var shadow = d.gp_registered_shadow || 0;
    var oos = d.oos_passed || 0;
    var promotions = d.canary_promotions || [];
    var rollbacks = d.canary_rollbacks || [];
    var retires = d.retire_candidates || [];
    var weightUpd = d.weights_updated;
    var dur = d.duration_sec || 0;
    var err = d.error || '';

    var summary = 'GP 生成 ' + gp + ' 候选';
    if (shadow > 0) summary += ', 注册 ' + shadow;
    if (oos > 0) summary += ', OOS 通过 ' + oos;
    if (promotions.length) summary += ', 晋升 ' + promotions.length;
    if (rollbacks.length) summary += ', 回滚 ' + rollbacks.length;
    if (retires.length) summary += ', 退役 ' + retires.length;
    summary += ' | ' + (weightUpd ? '权重已更新' : '权重未变');
    summary += ' | 耗时 ' + dur + 's';
    if (err) summary += ' | ⚠️ ' + err;

    var detail = [];
    if (promotions.length) detail.push({ label: '晋升', value: promotions.join(', '), cls: 'text-green' });
    if (rollbacks.length) detail.push({ label: '回滚', value: rollbacks.join(', '), cls: 'text-red' });
    if (retires.length) detail.push({ label: '退役', value: retires.join(', ') + (d.retire_reason ? ' (' + d.retire_reason + ')' : ''), cls: 'text-gray' });

    this.setData({
      'evolution.hasData': true,
      'evolution.summary': summary,
      'evolution.detail': detail,
      'evolution.time': d.ts_iso ? new Date(d.ts_iso).toLocaleString('zh-CN') : '',
    });
  },

  // ── 数据库健康 ──
  async _fetchDbHealth() {
    var d = await api.get('/api/system/db-health', 45000);
    if (!d || !d.ok) return;

    var overallCls = { healthy: 'text-green', degraded: 'text-orange', stale: 'text-red' }[d.overall] || 'text-gray';
    var s = d.summary || {};

    var dbList = (d.databases || []).map(function(db) {
      var fm = FRESHNESS_MAP[db.freshness] || FRESHNESS_MAP.unknown;
      var tables = db.tables || [];
      var mainTable = tables.length > 0 ? tables[0] : null;
      var ageText = mainTable && mainTable.latest_ts
        ? _fmtAge(Date.now() / 1000 - mainTable.latest_ts)
        : '—';
      return {
        name: db.name, file: db.file, size: db.size,
        rows: _fmtNum(db.total_rows),
        freshness: fm.label, freshnessCls: fm.cls, freshnessDot: fm.dot,
        ageText: ageText, tableCount: tables.length,
        hasError: (db.errors || []).length > 0,
      };
    });

    this.setData({
      dbOverall: d.overall === 'healthy' ? '健康' : d.overall === 'degraded' ? '降级' : '延迟',
      dbOverallCls: overallCls,
      dbSummary: (s.fresh || 0) + '正常 / ' + (s.stale || 0) + '延迟 / ' + (s.missing || 0) + '缺失',
      dbList: dbList,
      dbCheckedAt: _fmtTime(Date.now()),
    });
  },

  // ── v5: 服务器健康 (/api/health) ──
  async _fetchServerHealth() {
    var d = await api.get('/api/health');
    if (!d) return;
    var ok = d.status === 'ok';
    var uptime = d.uptime_seconds || 0;
    var h = Math.floor(uptime / 3600);
    var m = Math.floor((uptime % 3600) / 60);
    this.setData({
      serverStatus: ok ? '运行中' : '降级',
      serverStatusCls: ok ? 'text-green' : 'text-orange',
      serverUptime: h + 'h ' + m + 'm',
      serverDb: d.db || '—',
    });
  },

  // ── v5: 因子健康 (/api/factor-health/latest) ──
  async _fetchFactorHealth() {
    var d = await api.get('/api/factor-health/latest');
    if (!d || !d.report) return;
    var r = d.report;
    var factors = r.factors || [];
    // 取前 6 个有问题的
    var top = factors.filter(function(f) { return f.status !== 'HEALTHY'; }).slice(0, 6);
    var topList = top.map(function(f) {
      return {
        name: f.factor.replace(/_/g, ' '),
        score: f.score != null ? f.score.toFixed(1) : '—',
        status: f.status,
        statusCls: f.status === 'WATCH' ? 'text-orange' : f.status === 'DECAYING' ? 'text-red' : f.status === 'DEAD' ? 'text-gray' : 'text-green',
        statusBadge: f.status === 'WATCH' ? 'badge-orange' : f.status === 'DECAYING' ? 'badge-red' : f.status === 'DEAD' ? 'badge-gray' : 'badge-green',
        rollingIc: f.rolling_ic != null ? (f.rolling_ic >= 0 ? '+' : '') + f.rolling_ic.toFixed(4) : '—',
      };
    });
    this.setData({
      'factorHealth.hasData': true,
      'factorHealth.total': r.total || 0,
      'factorHealth.healthy': r.healthy || 0,
      'factorHealth.watch': r.watch || 0,
      'factorHealth.decaying': r.decaying || 0,
      'factorHealth.dead': r.dead || 0,
      'factorHealth.top': topList,
    });
  },

  // ── v5: 调度器 (/api/control/scheduler) ──
  async _fetchScheduler() {
    var d = await api.get('/api/control/scheduler');
    if (!d) return;
    var jobs = d.jobs || [];
    var runningCount = 0;
    for (var i = 0; i < jobs.length; i++) {
      if (jobs[i].running) runningCount++;
    }
    this.setData({
      'scheduler.running': !!d.running,
      'scheduler.jobCount': jobs.length,
      'scheduler.jobs': jobs.slice(0, 8).map(function(j) {
        return {
          name: j.name,
          cron: _cronLabel(j.cron_expr),
          running: j.running,
          runs: j.run_count || 0,
          errors: j.error_count || 0,
          hasError: (j.error_count || 0) > 0,
          lastError: j.last_error || '',
        };
      }),
    });
  },
});

// ── helpers ──

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
  var d = new Date(ts);
  var pad = function(n) { return String(n).padStart(2, '0'); };
  return pad(d.getHours()) + ':' + pad(d.getMinutes()) + ':' + pad(d.getSeconds());
}

var CRON_LABELS = {
  '0 1 * * *': '每日 01:00',
  '30 1 * * *': '每日 01:30',
  '0 2 * * *': '每日 02:00',
  '0 * * * *': '每小时整点',
  '*/30 * * * *': '每 30 分钟',
  '*/5 * * * *': '每 5 分钟',
  '*/10 * * * *': '每 10 分钟',
  '0 3 * * *': '每日 03:00',
  '0 */6 * * *': '每 6 小时',
};

function _cronLabel(expr) {
  if (!expr) return '—';
  return CRON_LABELS[expr] || expr;
}
