import systemStore from '../../stores/system';
import { refreshOpsDomain } from '../../services/ops';
import { formatDateTime, toneFromStatus } from '../../utils/format';

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
  },

  onLoad() {
    this._unsub = systemStore.subscribe(() => this.syncView());
    this.syncView();
    refreshOpsDomain();
  },

  onShow() {
    refreshOpsDomain();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = systemStore.getState();
    const apiHealth = state.apiHealth || {};
    const scheduler = state.scheduler || null;
    const jobs = (scheduler && scheduler.jobs) || [];
    const dbHealth = state.dbHealth;
    const runningJobs = jobs.filter((item) => item.running).length;
    const dbCards = buildDbCards(dbHealth);
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
});
