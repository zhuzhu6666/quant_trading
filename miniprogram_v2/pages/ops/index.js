import systemStore from '../../stores/system';
import { refreshOpsDomain } from '../../services/ops';
import { formatDateTime, humanizeRiskAction, humanizeRiskReason, toneFromStatus } from '../../utils/format';

function humanizeHealthComponent(key) {
  const mapping = {
    ctrader_bridge: '交易桥',
    live_loop: '实盘循环',
    bar_m1: '1分钟K线',
    bar_m5: '5分钟K线',
    tick_data: 'Tick 数据',
    l2_depth: '盘口深度',
    db_ctrader_data: '主行情库',
    db_ticks: 'Tick 库',
    db_l2: '深度库',
    disk_space: '磁盘空间',
  };
  return mapping[key] || key || '--';
}

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

function summarizePolicy(policy = null) {
  if (!policy) return null;
  const counts = policy.counts || {};
  const blocked = Number(counts.blocked || 0);
  const allowed = Number(counts.allowed || 0);
  const items = Array.isArray(policy.items) ? policy.items : [];
  const latest = items[0] || null;
  const byReason = policy.by_reason || {};
  const byAction = policy.by_action || {};
  const topReason = Object.keys(byReason)
    .sort((a, b) => Number(byReason[b] || 0) - Number(byReason[a] || 0))[0] || '';
  const actionRows = Object.keys(byAction)
    .sort((a, b) => Number(byAction[b] || 0) - Number(byAction[a] || 0))
    .slice(0, 4)
    .map((key) => ({ key, label: humanizeRiskAction(key), value: byAction[key] }));
  return {
    blocked,
    allowed,
    total: Number(policy.total || items.length || 0),
    latestAction: latest ? humanizeRiskAction(latest.action || latest.event_type || '--') : '--',
    latestReason: latest ? humanizeRiskReason(latest.reason || '--') : '--',
    latestTone: latest ? (latest.allowed ? 'positive' : 'negative') : 'neutral',
    topReason: humanizeRiskReason(topReason || '暂无'),
    actionRows,
  };
}

function summarizeSystemHealth(systemHealth = null) {
  if (!systemHealth) return null;
  const critical = Array.isArray(systemHealth.critical_components) ? systemHealth.critical_components : [];
  const degraded = Array.isArray(systemHealth.degraded_components) ? systemHealth.degraded_components : [];
  const blocking = Array.isArray(systemHealth.blocking_components) ? systemHealth.blocking_components : [];
  const advisoryCritical = Array.isArray(systemHealth.advisory_critical_components) ? systemHealth.advisory_critical_components : [];
  const components = systemHealth.components && typeof systemHealth.components === 'object' ? systemHealth.components : {};
  const componentRows = Object.keys(components)
    .slice(0, 6)
    .map((key) => ({
      key: humanizeHealthComponent(key),
      status: components[key] && components[key].status ? components[key].status : 'unknown',
    }));
  const impactStatus = systemHealth.impact_status || (blocking.length ? 'blocked' : critical.length || degraded.length ? 'observe' : 'ok');
  const impactTone =
    impactStatus === 'blocked'
      ? 'negative'
      : impactStatus === 'observe'
        ? 'warning'
        : toneFromStatus(systemHealth.overall || 'unknown');
  return {
    overall: systemHealth.overall || 'unknown',
    tone: impactTone,
    impactStatus,
    impactLabel:
      impactStatus === 'blocked'
        ? '会阻断交易'
        : impactStatus === 'observe'
          ? '需要盯住'
          : '暂不影响交易',
    impactSummary: systemHealth.impact_summary || '暂无运行风险摘要',
    criticalCount: critical.length,
    degradedCount: degraded.length,
    blockingCount: blocking.length,
    criticalText: critical.length ? critical.map(humanizeHealthComponent).join(' / ') : '无',
    degradedText: degraded.length ? degraded.map(humanizeHealthComponent).join(' / ') : '无',
    blockingText: blocking.length ? blocking.map(humanizeHealthComponent).join(' / ') : '无',
    advisoryText: advisoryCritical.length ? advisoryCritical.map(humanizeHealthComponent).join(' / ') : '无',
    scoreText: Number(systemHealth.overall_score || 0).toFixed(2),
    componentRows,
  };
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
    policySummary: null,
    systemHealthSummary: null,
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
    const riskSummary = state.riskSummary || null;
    const systemHealthSummary = summarizeSystemHealth(riskSummary && riskSummary.system_health);
    const runningJobs = jobs.filter((item) => item.running).length;
    const dbCards = buildDbCards(dbHealth);
    const policySummary = summarizePolicy(riskSummary && riskSummary.policy);
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
      policySummary,
      systemHealthSummary,
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
