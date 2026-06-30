function safeNumber(value, fallback = 0) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return num;
}

function safeString(value, fallback = '') {
  return String(value == null ? fallback : value).trim();
}

function safeBoolean(value, fallback = false) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (['1', 'true', 'yes', 'ok', 'allowed', 'enabled'].includes(normalized)) return true;
    if (['0', 'false', 'no', 'blocked', 'disabled', 'deny', 'forbidden'].includes(normalized)) return false;
  }
  return fallback;
}

function parseArray(values, fallback = []) {
  return Array.isArray(values) ? values : fallback;
}

function toPercentText(value, fallback = '--') {
  const num = safeNumber(value, Number.NaN);
  if (!Number.isFinite(num)) return fallback;
  if (num >= 0 && num <= 1) return `${(num * 100).toFixed(2)}%`;
  if (num > 1 && num <= 100) return `${num.toFixed(2)}%`;
  return String(num.toFixed(2));
}

function toneByPositiveRatio(ratio, neutralFloor = 0.5) {
  if (!Number.isFinite(ratio)) return 'neutral';
  const normalized = ratio > 1 ? ratio / 100 : ratio;
  if (normalized >= 0.8) return 'positive';
  if (normalized >= neutralFloor) return 'warning';
  return 'negative';
}

function formatCount(value, fallback = '0') {
  const num = safeNumber(value, Number.NaN);
  if (!Number.isFinite(num)) return fallback;
  return String(Math.max(0, Math.floor(num)));
}

function readToneFromStatusLevel(level) {
  const normalized = safeString(level).toLowerCase();
  if (['critical', 'error', 'blocked', 'fail', 'failed', 'down', 'offline'].includes(normalized)) return 'negative';
  if (['degraded', 'warning', 'observe', 'observed', 'mixed', 'pending', 'stale'].includes(normalized)) return 'warning';
  if (['ok', 'healthy', 'connected', 'running', 'good', 'active', 'allowed', 'ready', 'positive'].includes(normalized)) return 'positive';
  return 'neutral';
}

function toMetricCard(label, value, tone, source) {
  return {
    label: String(label || ''),
    value: String(value == null ? '--' : value),
    tone: String(tone || 'neutral'),
    source: safeString(source),
  };
}

function normalizeHealthComponentName(key) {
  const mapping = {
    ctrader_bridge: 'ctrader_bridge',
    live_loop: 'live_loop',
    bar_m1: 'bar_m1',
    bar_m5: 'bar_m5',
    tick_data: 'tick_data',
    db_ctrader_data: 'db_ctrader_data',
    db_ticks: 'db_ticks',
    db_l2: 'db_l2',
    disk_space: 'disk_space',
    l2_depth: 'l2_depth',
  };
  return mapping[safeString(key)] || safeString(key, 'unknown');
}

function normalizeHealthItem(item = {}) {
  if (typeof item === 'string') {
    return {
      component: normalizeHealthComponentName(item),
      status: 'unknown',
      source: '',
      classification: '',
      raw: item,
    };
  }
  return {
    component: normalizeHealthComponentName(item.component || item.key || item.name),
    status: safeString(item.status, 'unknown').toLowerCase(),
    source: safeString(item.source, ''),
    classification: safeString(item.classification, ''),
    raw: item,
  };
}

function normalizeSnapshots(items = []) {
  return parseArray(items).map((item = {}) => ({
    ...item,
    accuracy: safeNumber(item.accuracy),
    evaluatedCount: Number(item.evaluated_count || 0),
    auditCount: Number(item.audit_count || 0),
    createdAt: safeNumber(item.created_at),
  }));
}

function normalizeAudits(items = []) {
  return parseArray(items).map((item = {}) => ({
    ...item,
    startedAt: safeNumber(item.started_at),
    finishedAt: safeNumber(item.finished_at),
    status: safeString(item.status),
    sessionStatus: safeString(item.session_status),
    result: item.result || {},
    payload: item.payload || {},
  }));
}

function buildMetaShadowReportInterpretation(payload = {}) {
  const postureDistribution = payload.posture_distribution || {};
  const promotionGate = payload.promotion_gate || {};
  const capabilities = payload.capabilities || {};
  const accuracy = safeNumber(payload.accuracy);
  const holdoutAccuracy = safeNumber(payload.holdout_accuracy, accuracy);
  const evaluatedCount = safeNumber(payload.evaluated_count, 0);
  const auditCount = safeNumber(payload.audit_count, 0);
  const minEvaluatedCount = safeNumber(promotionGate.min_evaluated_count, 0);
  const minHoldoutAccuracy = safeNumber(promotionGate.min_holdout_accuracy, 0);
  const isShadowOnly = safeBoolean(capabilities.shadow_only, false) || safeBoolean(capabilities.advisory_only, false);
  const canTrade = safeBoolean(capabilities.live_trading, true);
  const eligibleForLive = promotionGate.eligible_for_live !== undefined ? safeBoolean(promotionGate.eligible_for_live, false) : safeBoolean(capabilities.eligible_for_live, false);
  const sampleSufficient = minEvaluatedCount <= 0 || evaluatedCount >= minEvaluatedCount;
  const holdoutSufficient = minHoldoutAccuracy <= 0 || holdoutAccuracy >= minHoldoutAccuracy;

  let stateText = '';
  let tone = 'neutral';
  let actionText = '';
  if (!sampleSufficient) {
    stateText = `样本不足：已评估 ${formatCount(evaluatedCount)}，建议至少到达 ${formatCount(minEvaluatedCount)}。`;
    tone = 'warning';
    actionText = '继续观察样本，待样本量充足后再进入复核；不要把当前结论当作交易依据。';
  } else if (!canTrade || isShadowOnly || !eligibleForLive) {
    stateText = '不可实盘（旁路只读）';
    tone = 'negative';
    actionText = '仅用于治理复核和报告展示，不要直接用于上线决策。';
  } else if (holdoutSufficient) {
    stateText = '可进入治理复核';
    tone = 'warning';
    actionText = '表现与样本已达门禁条件，可提交治理流程进行规则复核。';
  } else {
    stateText = '只读观察中';
    tone = 'warning';
    actionText = '命中率仍有波动，继续观察并积累证据；暂缓任何上线动作。';
  }

  const postureRows = Object.entries(postureDistribution)
    .filter((entry) => entry[0] && entry[1] !== undefined && entry[1] !== null)
    .map(([posture, value]) => {
      const key = safeString(posture).toLowerCase();
      if (key === 'contract') {
        return {
          key: 'contract',
          keyLabel: '收缩',
          value,
          meaning: '风险偏高时收紧动作；更偏谨慎，减少参与度。',
          tone: 'negative',
        };
      }
      if (key === 'observe') {
        return {
          key: 'observe',
          keyLabel: '观察',
          value,
          meaning: '暂不执行动作偏差，仅继续追踪指标与误差。',
          tone: 'warning',
        };
      }
      if (key === 'recover') {
        return {
          key: 'recover',
          keyLabel: '恢复',
          value,
          meaning: '可逐步恢复常规行为，减少旁路约束。',
          tone: 'positive',
        };
      }
      return {
        key,
        keyLabel: key || '未知姿态',
        value,
        meaning: '当前姿态未定义，作为补充统计展示。',
        tone: 'neutral',
      };
    });

  return {
    displayName: '元模型旁路评估',
    purposeText: '用于说明当前 meta LightGBM 的旁路评估状态，只能做人类可读复核，不直接发起交易。',
    stateText,
    tone,
    actionText,
    metricCards: [
      toMetricCard('命中率', toPercentText(accuracy), toneByPositiveRatio(accuracy, 0.45), 'accuracy'),
      toMetricCard('已评估样本', formatCount(evaluatedCount), sampleSufficient ? 'positive' : 'warning', 'evaluated_count'),
      toMetricCard('旁路记录', formatCount(auditCount), auditCount > 0 ? 'neutral' : 'warning', 'audit_count'),
    ],
    postureRows,
    evidence: {
      accuracy,
      holdoutAccuracy,
      evaluatedCount,
      minEvaluatedCount,
      auditCount,
      eligibleForLive,
      canTrade,
      isShadowOnly,
      postureDistribution,
    },
  };
}

function buildBackendReadinessInterpretation({
  blockingCount = 0,
  knownObservationCount = 0,
  readyForFrontend = false,
  permissionOk = true,
  permissionAuditStatus = '',
}) {
  const permissionBlocked =
    !permissionOk || safeString(permissionAuditStatus).toLowerCase() === 'blocked' || safeString(permissionAuditStatus).toLowerCase() === 'failed';
  let stateText = '';
  let tone = 'neutral';
  let actionText = '';

  if (blockingCount > 0) {
    stateText = '有阻断项，不能直接用于前端交易可见性';
    tone = 'negative';
    actionText = '先消化阻断项，确认后端恢复后再继续信任汇总状态。';
  } else if (!readyForFrontend) {
    stateText = '后端交接未完成，谨慎消费';
    tone = 'warning';
    actionText = '以待后端交接完成后再作为可用依据。';
  } else if (permissionBlocked) {
    stateText = '权限审计未通过，需要运维确认';
    tone = 'negative';
    actionText = '先处理权限审计异常，再放开治理与交易相关展示。';
  } else if (knownObservationCount > 0) {
    stateText = '阻断项清零，存在观察项，前端可参考但需提示警惕';
    tone = 'warning';
    actionText = '可继续运行；发现观察项上升时触发系统治理复核。';
  } else {
    stateText = '后端交接正常，可作为当前可信状态';
    tone = 'positive';
    actionText = '持续监控阻断项与观察项变化，不需要临时降级处理。';
  }

  return {
    stateText,
    tone,
    actionText,
  };
}

function buildBackendReadinessMetricCards({
  blockingCount = 0,
  knownObservationCount = 0,
  pendingGovernance = 0,
  highLoadAllowed = false,
  highLoadProfile = 'disabled',
}) {
  return [
    toMetricCard('阻断项', formatCount(blockingCount), blockingCount > 0 ? 'negative' : 'positive', 'blocking_components'),
    toMetricCard('观察项', formatCount(knownObservationCount), knownObservationCount > 0 ? 'warning' : 'positive', 'known_observations'),
    toMetricCard('系统治理待处理', formatCount(pendingGovernance), pendingGovernance > 0 ? 'warning' : 'positive', 'pending_review_count'),
    toMetricCard('高负载状态', `${highLoadAllowed ? '可执行' : '不可执行'} / ${highLoadProfile}`, highLoadAllowed ? 'positive' : 'warning', 'high_load'),
  ];
}

function buildHighLoadAuditInterpretation(payload = {}, items = [], latestAudit = null) {
  const allowedNow = safeBoolean(payload.allowed_now, false);
  const profile = safeString(payload.profile, 'disabled');
  const latestStatus = safeString(latestAudit && (latestAudit.status || latestAudit.session_status || latestAudit.statusText || latestAudit.jobStatus || '--'));
  const latestStatusLower = latestStatus.toLowerCase();

  let stateText = '';
  let tone = 'neutral';
  let actionText = '';

  if (items.length === 0 && !allowedNow) {
    stateText = `当前无可执行高负载审计记录，窗口策略为 ${profile}，暂不可执行`;
    tone = 'neutral';
    actionText = '等待窗口放行后再安排训练/审计。';
  } else if (!allowedNow) {
    stateText = `高负载窗口当前不可执行（${profile}）`;
    tone = 'warning';
    actionText = '当前仅观察高负载任务；避免新增训练与离线审计任务。';
  } else if (['running', 'queued', 'scheduled', 'pending'].includes(latestStatusLower)) {
    stateText = `高负载窗口可执行，最新任务状态 ${latestStatus}`;
    tone = 'warning';
    actionText = '任务仍在进行中，关注完成时间与是否阻塞下一窗口。';
  } else if (['failed', 'error', 'blocked'].includes(latestStatusLower)) {
    stateText = `高负载窗口可执行，但最近审计异常（${latestStatus}）`;
    tone = 'negative';
    actionText = '先处理失败原因，再尝试重跑审计任务。';
  } else if (latestStatus) {
    stateText = `高负载窗口可执行，最新审计状态 ${latestStatus}`;
    tone = 'positive';
    actionText = '可按策略继续调度下一次离线窗口。';
  } else {
    stateText = `高负载窗口可执行（${profile}）`;
    tone = 'positive';
    actionText = '当前可运行训练与审计窗口。';
  }

  return {
    displayName: '离线重任务窗口',
    purposeText: '控制训练和审计这类高负载任务的执行时机，避免与交易窗口冲突。',
    stateText,
    tone,
    actionText,
    allowedNow,
    profile,
    latestStatus,
    latestAudit,
  };
}

export function buildBackendReadinessView(payload = {}) {
  const systemHealth = payload.system_health || {};
  const models = payload.models || {};
  const metaModel = models.meta_lightgbm || {};
  const promotionGate = metaModel.promotion_gate || {};
  const latestPermissionAudit = models.latest_permission_audit || metaModel.latest_permission_audit || {};
  const permissionOk = models.permission_ok !== undefined
    ? !!models.permission_ok
    : metaModel.permission_ok !== undefined
      ? !!metaModel.permission_ok
      : true;
  const governance = payload.governance || {};
  const highLoad = payload.high_load || {};
  const blockers = normalizeHealthItem;

  const blockingComponents = parseArray(systemHealth.blocking_components).map((item) => blockers(item));
  const knownObservations = parseArray(systemHealth.known_observations).map((item) => blockers(item));
  const overall = safeString(systemHealth.overall, 'unknown');
  const displayOverall = safeString(systemHealth.display_overall, overall);
  const readiness = {
    raw: payload,
    ok: !!payload.ok,
    displayName: '后端交接状态',
    purposeText: '判断前端是否可以信任后端状态，并区分阻断项、观察项与权限审计的影响。',
    schemaVersion: safeString(payload.schema_version),
    generatedAt: safeNumber(payload.generated_at),
    readyForFrontend: !!payload.ready_for_frontend,
    blocked: blockingComponents.length > 0,
    displayOverall,
    overall,
    score: safeNumber(systemHealth.score),
    systemHealth,
    blockingComponents,
    knownObservations,
    blockersSummary: {
      blockingCount: blockingComponents.length,
      knownObservationCount: knownObservations.length,
      blockingText:
        blockingComponents.length > 0
          ? blockingComponents.map((item) => item.component).join('、')
          : '无',
      observationText:
        knownObservations.length > 0
          ? knownObservations.map((item) => item.component).join('、')
          : '无',
    },
    promotionGate: {
      modelType: safeString(metaModel.model_type, 'meta_model_lightgbm'),
      eligibleForLive: !!promotionGate.eligible_for_live,
      eligibleForGovernorReview: !!promotionGate.eligible_for_governor_review,
      computedWouldBeLive: !!promotionGate.computed_live_eligibility_would_be,
      reason: safeString(promotionGate.reason),
      minHoldoutAccuracy: safeNumber(promotionGate.min_holdout_accuracy),
      holdoutAccuracy: safeNumber(promotionGate.holdout_accuracy),
      minEvaluatedCount: Number(promotionGate.min_evaluated_count || 0),
      evaluatedCount: Number(promotionGate.evaluated_count || 0),
      // 固定 false；前端仅展示这个 gate，不要自己推导上线。
      eligibleForLiveLocked: false,
    },
    modelPermissions: {
      permissionOk,
      latestPermissionAudit,
    },
    governance: {
      automaticExecutionEnabled: !!governance.automatic_execution_enabled,
      pendingReviewCount: Number(governance.pending_review_count || 0),
      policySuggestionCounts: governance.policy_suggestion_counts || {},
      metaShadowReportSnapshots: governance.meta_shadow_report_snapshots || {},
      autonomyMode: safeString(governance.autonomy_mode),
      autonomyDemoAutoApply: !!governance.autonomy_demo_auto_apply,
      // false 表示当前没有自动治理应用权限，并不等同于交易需要用户接管。
      requiresHumanReview: governance.automatic_execution_enabled === false,
    },
    highLoad: {
      allowedNow: !!highLoad.allowed_now,
      profile: safeString(highLoad.profile, 'disabled'),
      sessionStatus: safeString(highLoad.session_status),
      canRunTrainingWithPositions: !!highLoad.can_run_training_with_positions,
      requiresClosedConfirmation: !!highLoad.requires_closed_confirmation,
      latestAudit: highLoad.latest_audit || {},
    },
    marketSession: payload.market_session || {},
    live: payload.live || {},
  };

  const interpretation = buildBackendReadinessInterpretation({
    blockingCount: readiness.blockersSummary.blockingCount,
    knownObservationCount: readiness.blockersSummary.knownObservationCount,
    readyForFrontend: readiness.readyForFrontend,
    permissionOk: readiness.modelPermissions.permissionOk,
    permissionAuditStatus: readiness.modelPermissions.latestPermissionAudit && readiness.modelPermissions.latestPermissionAudit.status,
  });
  readiness.stateText = interpretation.stateText;
  readiness.tone = interpretation.tone;
  readiness.actionText = interpretation.actionText;
  readiness.metricCards = buildBackendReadinessMetricCards({
    blockingCount: readiness.blockersSummary.blockingCount,
    knownObservationCount: readiness.blockersSummary.knownObservationCount,
    pendingGovernance: readiness.governance.pendingReviewCount,
    highLoadAllowed: readiness.highLoad.allowedNow,
    highLoadProfile: readiness.highLoad.profile,
  });
  readiness.explanation = {
    ...interpretation,
    metricCards: readiness.metricCards,
  };

  return readiness;
}

export function buildMetaShadowReportView(payload = {}) {
  const postureDistribution = payload.posture_distribution || {};
  const confusionMatrix = payload.confusion_matrix || {};
  const ruleComparison = payload.rule_comparison || {};
  const artifact = payload.artifact_summary || {};
  const metrics = artifact.metrics || {};
  const holdout = metrics.holdout || {};
  const interpretation = buildMetaShadowReportInterpretation(payload);

  const reportView = {
    raw: payload,
    ok: !!payload.ok,
    displayName: interpretation.displayName,
    purposeText: interpretation.purposeText,
    schemaVersion: safeString(payload.schema_version),
    modelType: safeString(payload.model_type),
    generatedAt: safeNumber(payload.generated_at),
    evaluatedCount: Number(payload.evaluated_count || 0),
    auditCount: Number(payload.audit_count || 0),
    accuracy: safeNumber(payload.accuracy),
    holdoutAccuracy: safeNumber(holdout.accuracy),
    holdoutCount: Number(holdout.count || 0),
    postureDistribution,
    labelDistribution: payload.label_distribution || {},
    averageScores: payload.average_scores || {},
    ruleComparison,
    confusionMatrix,
    capabilities: payload.capabilities || {},
    artifactPath: safeString(artifact.artifact_path),
    artifactVersion: safeString(artifact.model_version),
    sampleCount: Number((artifact.sample_window && artifact.sample_window.sample_count) || 0),
    samples: parseArray(payload.samples).map((item) => ({
      ...item,
      postureScore: safeNumber(item.posture_score),
      targetPnl: safeNumber(item.target_pnl),
      createdAt: safeNumber(item.created_at),
    })),
  };

  reportView.stateText = interpretation.stateText;
  reportView.tone = interpretation.tone;
  reportView.actionText = interpretation.actionText;
  reportView.metricCards = interpretation.metricCards;
  reportView.postureRows = interpretation.postureRows;
  reportView.explanation = {
    ...interpretation,
    metricCards: interpretation.metricCards,
    postureRows: interpretation.postureRows,
  };

  return reportView;
}

export function buildMetaShadowReportSnapshotsView(payload = {}) {
  const items = normalizeSnapshots(payload.items || []);
  return {
    raw: payload,
    count: Number(payload.count || items.length),
    items,
    latest: items[0] || null,
  };
}

export function buildOffmarketHighLoadAuditsView(payload = {}) {
  const items = normalizeAudits(payload.items || []);
  const latest = items[0] || null;
  const interpretation = buildHighLoadAuditInterpretation(payload, items, latest);
  return {
    raw: payload,
    count: Number(payload.count || items.length),
    items,
    latest,
    statusCount: items.reduce((acc, item) => {
      const key = safeString(item.status || 'unknown');
      acc[key] = Number(acc[key] || 0) + 1;
      return acc;
    }, {}),
    displayName: interpretation.displayName,
    purposeText: interpretation.purposeText,
    stateText: interpretation.stateText,
    tone: interpretation.tone,
    actionText: interpretation.actionText,
    metricCards: [
      toMetricCard('窗口许可', interpretation.allowedNow ? '可执行' : '不可执行', interpretation.allowedNow ? 'positive' : 'warning', 'allowed_now'),
      toMetricCard('窗口策略', interpretation.profile, interpretation.allowedNow ? 'positive' : 'warning', 'profile'),
      toMetricCard('最新审计状态', interpretation.latestStatus || '--', readToneFromStatusLevel(interpretation.latestStatus), 'latest_audit_status'),
    ],
    explanation: interpretation,
  };
}
