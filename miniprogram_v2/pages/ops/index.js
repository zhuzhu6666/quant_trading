import systemStore from '../../stores/system';
import learningStore from '../../stores/learning';
import opsStore from '../../stores/ops';
import * as opsService from '../../services/ops';
import * as learningService from '../../services/learning';
import { formatDateTime, humanizeRiskAction, humanizeRiskReason, toneFromStatus } from '../../utils/format';
import {
  buildGovernanceTodoCard,
  normalizeGovernanceStageKey,
  sortGovernanceItemsByPriority,
} from '../../utils/governance';

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
    promotion_gate: '交易启停门禁',
    permission_audit: '权限审计',
    live: '实盘运行',
    high_load: '高负载窗口',
    advisory_critical_components: '观察级关键组件',
    blocking_components: '必须先解决',
    known_observations: '可观察项',
  };
  const normalized = String(key || '').trim().toLowerCase();
  return mapping[normalized] || (normalized ? normalized.replace(/_/g, ' ') : '--');
}

function humanizeHealthStatus(status) {
  const normalized = String(status || '').toLowerCase();
  if (['blocking', 'blocked', 'critical', 'error', 'fail', 'failed', 'down', 'offline'].includes(normalized)) return '阻断';
  if (['degraded', 'observe', 'observed', 'warning'].includes(normalized)) return '观察';
  if (['allowed', 'ok', 'healthy', 'running', 'connected', 'active', 'positive', 'ready', 'allowed'].includes(normalized)) return '正常';
  if (['pending', 'stale'].includes(normalized)) return '待定';
  return '未标注';
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

function toneFromReadinessLevel(level) {
  const normalized = String(level || '').toLowerCase();
  if (['critical', 'error', 'blocked', 'fail', 'failed', 'down', 'offline'].includes(normalized)) return 'negative';
  if (['degraded', 'warning', 'observed', 'observe', 'mixed', 'pending', 'stale'].includes(normalized)) return 'warning';
  if (['ok', 'healthy', 'connected', 'running', 'good', 'active', 'allowed', 'ready', 'positive'].includes(normalized)) return 'positive';
  return 'neutral';
}

function buildComponentRows(items = []) {
  const seen = {};
  return (Array.isArray(items) ? items : [])
    .map((item = {}, index = 0) => {
      const source = typeof item === 'string' ? { component: item, status: 'unknown' } : item;
      const rawComponent = String(source.component || source.name || source.key || source.component_name || '--');
      const component = humanizeHealthComponent(rawComponent);
      const status = String(source.status || 'unknown');
      return {
        key: `${rawComponent}-${status}-${index}`,
        rawComponent,
        component,
        status,
        statusText: humanizeHealthStatus(status),
        classification: String(source.classification || ''),
        tone: toneFromReadinessLevel(status),
      };
    })
    .filter((item) => {
      const key = `${item.component}|${item.status}|${item.classification}`;
      if (seen[key]) return false;
      seen[key] = true;
      return true;
    })
    .slice(0, 8);
}

function buildOffmarketRows(items = []) {
  return (Array.isArray(items) ? items : [])
    .map((item = {}, index = 0) => {
      const startedAt = item.startedAt || item.started_at || 0;
      const finishedAt = item.finishedAt || item.finished_at || 0;
      return {
        key: String(item.audit_id || item.auditId || `${item.job_name || 'audit'}-${index}`),
        jobName: String(item.job_name || item.jobName || '--'),
        status: String(item.status || '--'),
        statusTone: toneFromReadinessLevel(item.status || 'unknown'),
        statusText: humanizeHealthStatus(item.status || 'unknown'),
        sessionStatus: String(item.sessionStatus || item.session_status || '--'),
        profile: String(item.high_load_profile || item.profile || '--'),
        profileText: humanizeHealthComponent(item.high_load_profile || item.profile || ''),
        runHintText: (item.status || '').toString().toLowerCase().includes('run') ? '正在用于运行窗口' : '离线任务',
        startedAtText: formatDateTime(startedAt),
        finishedAtText: formatDateTime(finishedAt),
        errorText: String(item.error || item.error_message || ''),
      };
    })
    .slice(0, 5);
}

function deriveReadinessConclusion(context = {}) {
  const readyForFrontend = !!context.readyForFrontend;
  const blockingCount = Number(context.blockingCount || 0);
  const observationCount = Number(context.observationCount || 0);
  const permissionOk = !!context.permissionOk;
  const permissionAuditStatus = String(context.permissionAuditStatus || '').toLowerCase();
  const tradingBlocked = !!context.tradingBlocked;
  const hasBlocking = blockingCount > 0 || tradingBlocked;
  const permissionBlocked = !permissionOk || ['blocked', 'failed', 'error'].includes(permissionAuditStatus);

  let availability = '待确认';
  let availabilityTone = 'neutral';
  if (hasBlocking || permissionBlocked) {
    availability = '有阻断';
    availabilityTone = 'negative';
  } else if (readyForFrontend) {
    availability = observationCount > 0 ? '可用（含观察项）' : '可用';
    availabilityTone = observationCount > 0 ? 'warning' : 'positive';
  } else {
    availability = '待交接/待确认';
    availabilityTone = 'warning';
  }

  const tradeImpact = hasBlocking ? '会影响交易（可能被系统拦截）' : '不影响当前交易执行';
  const modelImpact = readyForFrontend && !hasBlocking && !permissionBlocked ? '模型展示可按正式状态显示' : '模型展示建议降级，仅作参考';

  let nextAction = '持续观察即可';
  if (hasBlocking) {
    nextAction = '先修复阻断组件，再恢复前端信任链路。';
  } else if (!readyForFrontend) {
    nextAction = '等待后端交接完成后再作为生产依据。';
  } else if (permissionBlocked) {
    nextAction = '先处理权限审计异常，再继续依赖该指标。';
  } else if (observationCount > 0) {
    nextAction = '当前可继续运行；观察项上升时进入系统治理复核。';
  }

  return {
    availability,
    availabilityTone,
    tradeImpact,
    modelImpact,
    nextAction,
  };
}

function formatHighLoadPermissionStatus(allowed = false, profile = 'disabled') {
  if (allowed) {
    return `现在能跑训练：是（策略 ${humanizeHealthComponent(profile)}）`;
  }
  return `现在能跑训练：否（策略 ${humanizeHealthComponent(profile)}）`;
}

function formatLatestHighLoadAuditSummary(latestAudit = {}) {
  const status = String(latestAudit.status || latestAudit.session_status || '--');
  const statusText = humanizeHealthStatus(status);
  const job = String(latestAudit.job_name || latestAudit.jobName || '--');
  const time = formatDateTime(latestAudit.finished_at || latestAudit.finishedAt || latestAudit.started_at || latestAudit.startedAt || 0);
  if (!status || status === '--') {
    return '暂无可用审计任务历史。';
  }
  return `${job} · ${statusText === '未标注' ? status : statusText} · ${time}`;
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

function summarizePolicy(policy = null, systemHealth = null) {
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
  const tradingBlocked = !!(systemHealth && systemHealth.trading_blocked);
  const impactStatus = systemHealth && systemHealth.impact_status ? systemHealth.impact_status : 'ok';
  const blockingComponents = Array.isArray(systemHealth && systemHealth.blocking_components)
    ? systemHealth.blocking_components
    : [];
  const currentTone = tradingBlocked ? 'negative' : impactStatus === 'observe' ? 'warning' : 'positive';
  const currentStatus = tradingBlocked ? '现在不能开仓' : '现在可以开仓';
  const currentReason = tradingBlocked
    ? ((systemHealth && systemHealth.impact_summary) || '当前有硬风控项会直接阻断开仓。')
    : ((systemHealth && systemHealth.impact_summary) || '当前没有会直接阻断开仓的硬风控项。');
  const currentDetail = tradingBlocked
    ? `直接阻断项：${blockingComponents.length ? blockingComponents.map(humanizeHealthComponent).join(' / ') : '未提供'}`
    : '最近的拦截记录只代表历史，不代表现在这一刻。';
  return {
    blocked,
    allowed,
    total: Number(policy.total || items.length || 0),
    currentTone,
    currentStatus,
    currentReason,
    currentDetail,
    latestAction: latest ? humanizeRiskAction(latest.action || latest.event_type || '--') : '--',
    latestReason: latest ? humanizeRiskReason(latest.reason || '--') : '--',
    latestTone: latest ? (latest.allowed ? 'positive' : 'negative') : 'neutral',
    latestTitle: latest
      ? (latest.allowed ? '最近一次历史放行' : '最近一次历史拦截')
      : '最近一次历史裁决',
    latestTime: latest && latest.decision_ts ? formatDateTime(latest.decision_ts) : '--',
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
  const effectiveCritical = critical.filter((key) => !advisoryCritical.includes(key));
  const components = systemHealth.components && typeof systemHealth.components === 'object' ? systemHealth.components : {};
  const componentRows = Object.keys(components)
    .slice(0, 6)
    .map((key) => {
      const rawStatus = components[key] && components[key].status ? components[key].status : 'unknown';
      const statusText = advisoryCritical.includes(key)
        ? 'observe-only'
        : blocking.includes(key)
          ? 'blocking'
          : rawStatus;
      return {
        key: humanizeHealthComponent(key),
        status: statusText,
      };
    });
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
    criticalText: effectiveCritical.length ? effectiveCritical.map(humanizeHealthComponent).join(' / ') : '无',
    degradedText: degraded.length ? degraded.map(humanizeHealthComponent).join(' / ') : '无',
    blockingText: blocking.length ? blocking.map(humanizeHealthComponent).join(' / ') : '无',
    advisoryText: advisoryCritical.length ? advisoryCritical.map(humanizeHealthComponent).join(' / ') : '无',
    scoreText: Number(systemHealth.overall_score || 0).toFixed(2),
    componentRows,
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

function humanizeOutcome(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'good_win') return '高质量盈利';
  if (key === 'lucky_win') return '幸运盈利';
  if (key === 'good_loss') return '可接受亏损';
  if (key === 'bad_loss') return '无效亏损';
  if (key === 'win') return '盈利';
  if (key === 'loss') return '亏损';
  return value || '进行中';
}

function humanizeTraceResponsibility(value = '') {
  return humanizeResponsibility(value || '');
}

function matchesRecentTraceFilter(item = {}, filter = 'all') {
  const key = String(filter || 'all').toLowerCase();
  const governanceStageKey = normalizeGovernanceStageKey(item.parameter_governance_stage || '');
  if (key === 'all') return true;
  if (key === 'parameter_only') return !!item.parameter_governance_factor;
  if (key === 'exit_only') return String(item.primary_responsibility || '') === 'exit';
  if (key === 'timing_only') return String(item.primary_responsibility || '') === 'timing';
  if (key === 'governance_hint') return !!item.parameter_governance_factor || !!item.parameter_candidate_status;
  if (key === 'candidate_review') return String(item.parameter_governance_entry_type || '') === 'candidate';
  if (key === 'recommendation_only') return String(item.parameter_governance_entry_type || '') === 'recommendation';
  if (key === 'online_light_only') return governanceStageKey === 'online_light';
  if (key === 'offline_deep_only') return governanceStageKey === 'offline_deep';
  if (key === 'deployed_watch') return governanceStageKey === 'deployed';
  return true;
}

function matchesRecentTraceSearch(item = {}, keyword = '') {
  const text = String(keyword || '').trim().toLowerCase();
  if (!text) return true;
  const haystack = [
    item.position_id,
    item.trade_id,
    item.entry_decision_id,
    item.exit_decision_id,
    item.summary_text,
    item.parameter_governance_factor,
    item.symbol,
    item.primary_responsibility,
  ]
    .map((value) => String(value || '').toLowerCase())
    .join(' ');
  return haystack.includes(text);
}

function mapOverviewHintCard(item = null) {
  if (!item) return null;
  return {
    factorId: String(item.factor_id || ''),
    candidateId: String(item.candidate_id || ''),
    recommendationId: String(item.recommendation_id || ''),
    title: String(item.title || ''),
    stageText: String(item.stage_tag || ''),
    note: String(item.summary || ''),
    actionLabel: String(item.action_label || ''),
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
    recentTradeTraceItems: [],
    totalRecentTradeTraceCount: 0,
    recentTraceFilter: 'all',
    recentTraceSearch: '',
    tracePositionId: '',
    traceDecisionId: '',
    recentTraceQueries: [],
    pendingTemplateCandidateCount: 0,
    pendingTemplateRecommendationCount: 0,
    onlineLightRecommendationCount: 0,
    offlineDeepRecommendationCount: 0,
    pendingCandidateCard: null,
    pendingOnlineRecommendationCard: null,
    pendingOfflineRecommendationCard: null,
    pendingGovernanceTodoCard: null,
    recentTraceTodoCard: null,
    backendReadinessStatus: 'idle',
    backendReadinessError: '',
    backendReadinessReadyForFrontend: false,
    backendReadinessReadyForFrontendText: 'unknown',
    backendReadinessOverall: '--',
    backendReadinessDisplayOverall: '--',
    backendReadinessTone: 'neutral',
    backendReadinessConclusionAvailability: '待确认',
    backendReadinessConclusionAvailabilityTone: 'neutral',
    backendReadinessTradeImpactText: '暂未评估',
    backendReadinessModelImpactText: '暂未评估',
    backendReadinessNextActionText: '持续观察即可',
    backendReadinessMarketSessionStatus: '--',
    backendReadinessBlockers: [],
    backendReadinessObservations: [],
    backendReadinessBlockingCount: 0,
    backendReadinessObservationCount: 0,
    backendReadinessPendingGovernance: 0,
    backendReadinessPermissionOk: true,
    backendReadinessPermissionAuditTone: 'neutral',
    backendReadinessPermissionAuditStatus: '--',
    backendReadinessPermissionAuditAt: '--',
    backendReadinessPermissionAuditId: '',
    backendReadinessHighLoadAllowed: false,
    backendReadinessHighLoadProfile: 'disabled',
    backendReadinessHighLoadAuditStatus: '--',
    backendReadinessHighLoadAuditTone: 'neutral',
    backendReadinessHighLoadAuditTime: '--',
    backendReadinessHighLoadAuditJob: '--',
    backendReadinessHighLoadAuditError: '',
    backendReadinessHighLoadPermissionText: '现在能跑训练：未知',
    backendReadinessHighLoadLatestSummaryText: '暂无可用审计任务历史。',
    offmarketHighLoadAuditsStatus: 'idle',
    offmarketHighLoadAuditsError: '',
    offmarketHighLoadAuditsCount: 0,
    offmarketHighLoadAuditsStatusRows: [],
    offmarketHighLoadAuditsLatest: null,
    offmarketHighLoadAuditsLatestText: '--',
    offmarketHighLoadAuditsLatestStatus: '--',
    offmarketHighLoadAuditsLatestTime: '--',
    offmarketHighLoadAuditsRows: [],
  },

  onLoad() {
    this._unsubSystem = systemStore.subscribe(() => this.syncView());
    this._unsubLearning = learningStore.subscribe(() => this.syncView());
    this._unsubOps = opsStore.subscribe(() => this.syncView());
    this.syncView();
    this.refreshReadinessData();
  },

  onShow() {
    this.refreshReadinessData();
  },

  onUnload() {
    this._unsubSystem && this._unsubSystem();
    this._unsubLearning && this._unsubLearning();
    this._unsubOps && this._unsubOps();
  },

  refreshReadinessData() {
    opsService.refreshOpsDomain && opsService.refreshOpsDomain();
    if (typeof learningService.refreshLearning === 'function') learningService.refreshLearning();
    if (typeof opsService.refreshBackendReadiness === 'function') opsService.refreshBackendReadiness();
    if (typeof learningService.refreshOffmarketHighLoadAudits === 'function') learningService.refreshOffmarketHighLoadAudits();
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
    const policySummary = summarizePolicy(riskSummary && riskSummary.policy, riskSummary && riskSummary.system_health);
    const recentTradeTraceItems = (state.recentTradeTraces || []).map((item) => {
      const governanceStageTag = String(item.parameter_governance_stage || '');
      const governanceStageSummary = String(item.parameter_governance_stage_summary || '');
      const governanceNextStepText = String(item.parameter_governance_next_step || '');
      const governanceTargetTypeText = String(item.parameter_governance_target_type || '');
      const governanceEntryHintText = String(item.parameter_governance_entry_hint_text || '');
      const governanceActionLabel = String(item.parameter_governance_action_label || '');
      const governancePriority = {
        score: Number(item.parameter_governance_priority_score || 0),
        label: String(item.parameter_governance_priority_label || ''),
        summary: String(item.parameter_governance_priority_summary || ''),
      };
      return {
        ...item,
      id: item.review_id || `${item.position_id || '--'}_${item.created_at || 0}`,
      title: item.position_id ? `position ${item.position_id}` : `trade ${item.trade_id || '--'}`,
      outcomeText: humanizeOutcome(item.outcome_label || ''),
      responsibilityText: humanizeTraceResponsibility(item.primary_responsibility || ''),
      governanceText: item.parameter_governance_factor
        ? `${item.parameter_governance_factor} · ${item.parameter_candidate_status || '待评估'}`
        : '',
      governanceStageText: governanceStageTag,
      governanceStageTag,
      governanceStageSummary,
      governanceNextStepText,
      governanceActionLabel,
      governanceTargetTypeText,
      governanceEntryTypeText: governanceEntryHintText,
      governanceCandidateId: String(item.parameter_candidate_id || ''),
      governanceRecommendationId: String(item.parameter_recommendation_id || ''),
      governancePriorityScore: governancePriority.score,
      governancePriorityLabel: governancePriority.label,
      governancePrioritySummary: governancePriority.summary,
      timeText: formatDateTime(item.created_at || 0),
      };
    });
    const recentTraceFilter = this.data.recentTraceFilter || 'all';
    const recentTraceSearch = this.data.recentTraceSearch || '';
    const learning = learningStore.getState();
    const learningSummary = learning.summary || {};
    const candidateCounts = learningSummary.parameter_template_candidates || {};
    const recommendationCounts = learningSummary.parameter_template_recommendations || {};
    const summaryOverview = learningSummary.parameter_template_overview || {};
    const pendingCandidateCard = mapOverviewHintCard(summaryOverview.pending_candidate_hint);
    const pendingOnlineRecommendationCard = mapOverviewHintCard(summaryOverview.online_light_hint);
    const pendingOfflineRecommendationCard = mapOverviewHintCard(summaryOverview.offline_deep_hint);
    const filteredRecentTradeTraceItems = recentTradeTraceItems.filter(
      (item) => matchesRecentTraceFilter(item, recentTraceFilter) && matchesRecentTraceSearch(item, recentTraceSearch)
    );
    const sortedRecentTradeTraceItems = sortGovernanceItemsByPriority(filteredRecentTradeTraceItems);
    const recentTraceTodoCard = buildGovernanceTodoCard(sortedRecentTradeTraceItems);
    const summaryGovernanceTodo = learningSummary.parameter_template_todo || null;
    const opsState = opsStore.getState();
    const backendReadinessRaw = opsState.backendReadiness || {};
    const readinessView = opsState.backendReadinessView || null;
    const readinessStatus = String(opsState.backendReadinessStatus || 'idle');
    const readinessError = String(opsState.backendReadinessError || '');
    const readinessBlockingComponents = readinessView && Array.isArray(readinessView.blockingComponents)
      ? readinessView.blockingComponents
      : [];
    const readinessKnownObservations = readinessView && Array.isArray(readinessView.knownObservations)
      ? readinessView.knownObservations
      : [];
    const readinessGovernance = readinessView && readinessView.governance ? readinessView.governance : {};
    const readinessModelPermissions = readinessView && readinessView.modelPermissions ? readinessView.modelPermissions : {};
    const readinessHighLoad = readinessView && readinessView.highLoad ? readinessView.highLoad : {};
    const readinessMarketSession = readinessView && readinessView.marketSession ? readinessView.marketSession : {};
    const readinessBlockersSummary = readinessView && readinessView.blockersSummary ? readinessView.blockersSummary : {};
    const readinessReadyForFrontend = readinessView ? !!readinessView.readyForFrontend : false;
    const readinessOverall = readinessView && readinessView.overall ? readinessView.overall : '--';
    const readinessDisplayOverall = readinessView && readinessView.displayOverall ? readinessView.displayOverall : '--';
    const blockingRows = buildComponentRows([
      ...(Array.isArray(backendReadinessRaw.blockers) ? backendReadinessRaw.blockers : []),
      ...readinessBlockingComponents,
    ]);
    const observationRows = buildComponentRows(
      Array.isArray(backendReadinessRaw.known_observations) ? backendReadinessRaw.known_observations : readinessKnownObservations
    );
    const governance = readinessGovernance;
    const modelPermissions = readinessModelPermissions;
    const highLoad = readinessHighLoad;
    const marketSession = readinessMarketSession;
    const latestPermissionAudit = modelPermissions.latestPermissionAudit || {};
    const permissionAuditOk = !!modelPermissions.permissionOk;
    const permissionAuditStatus = String(latestPermissionAudit.status || (permissionAuditOk ? 'ok' : 'blocked'));
    const highLoadLatestAudit = highLoad.latestAudit || {};
    const readinessBlockingCount = Number(
      Array.isArray(backendReadinessRaw.blockers)
        ? backendReadinessRaw.blockers.length
        : readinessBlockersSummary && Number.isFinite(Number(readinessBlockersSummary.blockingCount))
          ? Number(readinessBlockersSummary.blockingCount)
          : blockingRows.length
    );
    const readinessObservationCount = Number(
      Array.isArray(backendReadinessRaw.known_observations)
        ? backendReadinessRaw.known_observations.length
        : readinessBlockersSummary && Number.isFinite(Number(readinessBlockersSummary.knownObservationCount))
          ? Number(readinessBlockersSummary.knownObservationCount)
          : observationRows.length
    );
    const readinessTradingBlocked = !!(readinessView && readinessView.systemHealth && readinessView.systemHealth.trading_blocked);
    const readinessConclusion = deriveReadinessConclusion({
      readyForFrontend: readinessReadyForFrontend,
      blockingCount: readinessBlockingCount,
      observationCount: readinessObservationCount,
      permissionOk: permissionAuditOk,
      permissionAuditStatus: permissionAuditStatus,
      tradingBlocked: readinessTradingBlocked,
    });
    const highLoadPermissionText = formatHighLoadPermissionStatus(!!highLoad.allowedNow, String(highLoad.profile || 'disabled'));
    const highLoadLatestText = formatLatestHighLoadAuditSummary(highLoadLatestAudit);
    const offmarketView = learning.offmarketHighLoadAuditsView || null;
    const offmarketStatusCounts = offmarketView && offmarketView.statusCount ? offmarketView.statusCount : {};
    const offmarketRows = buildOffmarketRows((offmarketView && offmarketView.items) || []);
    const offmarketLatest = offmarketView ? offmarketView.latest || null : null;
    const offmarketLatestStatus = offmarketLatest ? String(offmarketLatest.status || '--') : '--';
    const offmarketLatestTime = offmarketLatest
      ? formatDateTime(offmarketLatest.finishedAt || offmarketLatest.finished_at || offmarketLatest.startedAt || offmarketLatest.started_at || 0)
      : '--';
    const offmarketLatestText = offmarketLatest
      ? `${offmarketLatest.job_name || offmarketLatest.jobName || '--'} · ${offmarketLatestStatus} · ${offmarketLatest.session_status || offmarketLatest.sessionStatus || '--'}`
      : '--';
    const pendingGovernanceTodoCard = summaryGovernanceTodo
      ? {
          factorId: String(summaryGovernanceTodo.factor_id || ''),
          candidateId: String(summaryGovernanceTodo.candidate_id || ''),
          recommendationId: String(summaryGovernanceTodo.recommendation_id || ''),
          title: String(summaryGovernanceTodo.title || ''),
          stageText: String(summaryGovernanceTodo.stage_tag || ''),
          note: String(summaryGovernanceTodo.priority_summary || summaryGovernanceTodo.summary || ''),
          actionLabel: String(summaryGovernanceTodo.action_label || ''),
          targetTypeText: String(summaryGovernanceTodo.target_type || ''),
          priorityLabel: String(summaryGovernanceTodo.priority_label || ''),
          queueHint: String(summaryGovernanceTodo.queue_hint || ''),
        }
      : null;
    const recentTraceQueries = (state.recentTradeTraceQueries || []).map((item) => ({
      ...item,
      id: `${item.positionId || '--'}_${item.decisionId || '--'}_${item.ts || 0}`,
      label: item.positionId
        ? `position ${item.positionId}`
        : `decision ${item.decisionId || '--'}`,
      sublabel: item.positionId && item.decisionId
        ? `decision ${item.decisionId}`
        : item.positionId
          ? 'position 查询'
          : 'decision 查询',
      timeText: formatDateTime(item.ts || 0),
    }));
    this.setData({
      backendReadinessStatus: readinessStatus,
      backendReadinessError: readinessError,
      backendReadinessReadyForFrontend: readinessReadyForFrontend,
      backendReadinessConclusionAvailability: readinessConclusion.availability,
      backendReadinessConclusionAvailabilityTone: readinessConclusion.availabilityTone,
      backendReadinessTradeImpactText: readinessConclusion.tradeImpact,
      backendReadinessModelImpactText: readinessConclusion.modelImpact,
      backendReadinessNextActionText: readinessConclusion.nextAction,
      backendReadinessReadyForFrontendText: readinessReadyForFrontend ? '可用' : (readinessView ? '不可用' : '未知'),
      backendReadinessOverall: String(readinessOverall),
      backendReadinessDisplayOverall: String(readinessDisplayOverall),
      backendReadinessTone: toneFromReadinessLevel(readinessDisplayOverall && readinessDisplayOverall !== '--' ? readinessDisplayOverall : readinessOverall),
      backendReadinessMarketSessionStatus: String(marketSession.status || marketSession.session_status || '--'),
      backendReadinessBlockers: blockingRows,
      backendReadinessObservations: observationRows,
      backendReadinessBlockingCount: readinessBlockingCount,
      backendReadinessObservationCount: readinessObservationCount,
      backendReadinessPendingGovernance: Number(governance.pendingReviewCount || 0),
      backendReadinessPermissionOk: permissionAuditOk,
      backendReadinessPermissionAuditTone: permissionAuditOk ? 'positive' : 'negative',
      backendReadinessPermissionAuditStatus: permissionAuditStatus || 'pending',
      backendReadinessPermissionAuditAt: formatDateTime(latestPermissionAudit.created_at || latestPermissionAudit.createdAt || latestPermissionAudit.created_at_ms || 0),
      backendReadinessPermissionAuditId: String(latestPermissionAudit.audit_id || latestPermissionAudit.permissionAuditId || ''),
      backendReadinessHighLoadAllowed: !!highLoad.allowedNow,
      backendReadinessHighLoadProfile: String(highLoad.profile || '--'),
      backendReadinessHighLoadAuditStatus: String(highLoadLatestAudit.status || '--'),
      backendReadinessHighLoadAuditTone: toneFromReadinessLevel(highLoadLatestAudit.status || 'unknown'),
      backendReadinessHighLoadAuditTime: formatDateTime(highLoadLatestAudit.finishedAt || highLoadLatestAudit.finished_at || highLoadLatestAudit.startedAt || highLoadLatestAudit.started_at || 0),
      backendReadinessHighLoadAuditJob: String(highLoadLatestAudit.job_name || highLoadLatestAudit.jobName || '--'),
      backendReadinessHighLoadAuditError: String(highLoadLatestAudit.error || ''),
      backendReadinessHighLoadPermissionText: highLoadPermissionText,
      backendReadinessHighLoadLatestSummaryText: highLoadLatestText,
      offmarketHighLoadAuditsStatus: String(learning.offmarketHighLoadAuditsStatus || 'idle'),
      offmarketHighLoadAuditsError: String(learning.offmarketHighLoadAuditsError || ''),
      offmarketHighLoadAuditsCount: Number(offmarketView ? offmarketView.count || offmarketRows.length : 0),
    offmarketHighLoadAuditsStatusRows: Object.keys(offmarketStatusCounts).map((status) => ({
        label: `${status}:${offmarketStatusCounts[status]}`,
        tone: toneFromReadinessLevel(status),
      })),
      offmarketHighLoadAuditsLatest: offmarketView ? offmarketView.latest || null : null,
      offmarketHighLoadAuditsLatestText: offmarketLatestText,
      offmarketHighLoadAuditsLatestStatus: offmarketLatestStatus,
      offmarketHighLoadAuditsLatestTime: offmarketLatestTime,
      offmarketHighLoadAuditsRows: offmarketRows,
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
      recentTradeTraceItems: sortedRecentTradeTraceItems,
      totalRecentTradeTraceCount: recentTradeTraceItems.length,
      recentTraceQueries,
      pendingTemplateCandidateCount: Number(candidateCounts.pending_review || 0),
      pendingTemplateRecommendationCount: Number(recommendationCounts.total || 0),
      onlineLightRecommendationCount: Number(recommendationCounts.online_light || 0),
      offlineDeepRecommendationCount: Number(recommendationCounts.offline_deep || 0),
      pendingCandidateCard,
      pendingOnlineRecommendationCard,
      pendingOfflineRecommendationCard,
      pendingGovernanceTodoCard,
      recentTraceTodoCard,
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

  onTracePositionInput(e) {
    this.setData({ tracePositionId: e.detail.value || '' });
  },

  onTraceDecisionInput(e) {
    this.setData({ traceDecisionId: e.detail.value || '' });
  },

  async onQueryTradeTrace() {
    const positionId = String(this.data.tracePositionId || '').trim();
    const decisionId = String(this.data.traceDecisionId || '').trim();
    if (!positionId && !decisionId) {
      wx.showToast({
        title: '请输入 position_id 或 decision_id',
        icon: 'none',
        duration: 1800,
      });
      return;
    }
    opsService.openTradeTracePage({ positionId, decisionId });
  },

  replayRecentTrace(e) {
    const positionId = String((e.currentTarget.dataset && e.currentTarget.dataset.positionId) || '').trim();
    const decisionId = String((e.currentTarget.dataset && e.currentTarget.dataset.decisionId) || '').trim();
    opsService.openTradeTracePage({ positionId, decisionId });
  },

  openRecentTradeTrace(e) {
    const positionId = String((e.currentTarget.dataset && e.currentTarget.dataset.positionId) || '').trim();
    const decisionId = String((e.currentTarget.dataset && e.currentTarget.dataset.decisionId) || '').trim();
    opsService.openTradeTracePage({ positionId, decisionId });
  },

  openRecentGovernance(e) {
    const candidateId = String((e.currentTarget.dataset && e.currentTarget.dataset.candidateId) || '').trim();
    const recommendationId = String((e.currentTarget.dataset && e.currentTarget.dataset.recommendationId) || '').trim();
    const factorId = String((e.currentTarget.dataset && e.currentTarget.dataset.factorId) || '').trim();
    if (candidateId) {
      learningService.openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId,
        factorId,
      });
      return;
    }
    if (recommendationId) {
      learningService.openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId,
        factorId,
      });
    }
  },

  openPendingCandidate() {
    const item = this.data.pendingCandidateCard || null;
    if (!item || !item.candidateId) return;
    learningService.openLearningGovernancePage({
      type: 'offline_candidate',
      candidateId: item.candidateId,
      factorId: item.factorId,
    });
  },

  openPendingRecommendation() {
    const item = this.data.pendingOnlineRecommendationCard || null;
    if (!item || !item.recommendationId) return;
    learningService.openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: item.recommendationId,
      factorId: item.factorId,
    });
  },

  openOfflineRecommendation() {
    const item = this.data.pendingOfflineRecommendationCard || null;
    if (!item || !item.recommendationId) return;
    learningService.openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: item.recommendationId,
      factorId: item.factorId,
    });
  },

  switchRecentTraceFilter(e) {
    const filter = String((e.currentTarget.dataset && e.currentTarget.dataset.filter) || 'all');
    this.setData({ recentTraceFilter: filter }, () => this.syncView());
  },

  onRecentTraceSearchInput(e) {
    this.setData({ recentTraceSearch: e.detail.value || '' }, () => this.syncView());
  },

  async onRefresh() {
    const tasks = [
      opsService.refreshOpsDomain && opsService.refreshOpsDomain(),
      opsService.refreshBackendReadiness && opsService.refreshBackendReadiness({ force: true }),
      learningService.refreshLearning && learningService.refreshLearning(),
      learningService.refreshOffmarketHighLoadAudits && learningService.refreshOffmarketHighLoadAudits({ force: true }),
    ].filter(Boolean);
    await Promise.all(tasks);
  },
});
