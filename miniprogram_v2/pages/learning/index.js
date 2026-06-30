import learningStore from '../../stores/learning';
import {
  consumeLearningGovernanceFocus,
  refreshLearning,
} from '../../services/learning';
import * as learningService from '../../services/learning';
import { openTradeTracePage } from '../../services/ops';
import { formatDateTime, toneFromStatus } from '../../utils/format';
import { sortGovernanceItemsByPriority } from '../../utils/governance';

function isRecord(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function toEpochMs(value) {
  if (value === null || value === undefined || value === '') return 0;
  const n = Number(value);
  if (Number.isFinite(n)) {
    return n < 1_000_000_000_000 ? n * 1000 : n;
  }
  const t = new Date(value).getTime();
  return Number.isFinite(t) ? t : 0;
}

function safeToNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

function formatAccuracy(value) {
  const n = safeToNumber(value);
  if (n === null) return '--';
  if (n >= 0 && n <= 1) return `${(n * 100).toFixed(2)}%`;
  if (n > 1 && n <= 100) return `${n.toFixed(2)}%`;
  return n.toFixed(2);
}

function formatIntText(value) {
  const n = safeToNumber(value);
  if (n === null) return '--';
  return String(Math.round(n));
}

function formatDistributionValue(value) {
  const n = safeToNumber(value);
  if (n === null) return String(value || '--');
  if (n > 0 && n <= 1) return `${(n * 100).toFixed(2)}%`;
  if (Number.isInteger(n)) return String(n);
  return n.toFixed(3);
}

function toPercentLikeNumber(value) {
  const n = safeToNumber(value);
  if (n === null) return null;
  if (n > 0 && n <= 1) return n * 100;
  return n;
}

function humanizePostureItem(code) {
  const key = String(code || '').trim().toLowerCase();
  if (key === 'contract') {
    return {
      label: '收缩交易',
      explanation: '模型建议减少开仓节奏，优先控制仓位扩张。',
    };
  }
  if (key === 'observe') {
    return {
      label: '继续观察',
      explanation: '样本不足或边界不清，模型建议先继续观察，不急于改规则。',
    };
  }
  if (key === 'recover') {
    return {
      label: '恢复节奏',
      explanation: '近期判断偏正向，系统建议恢复到常规节奏。',
    };
  }
  return {
    label: `动作 ${code}`,
    explanation: `模型输出的动作 ${String(code || '').toUpperCase() || '未知'}。`,
  };
}

function normalizeConfusionMatrixKey(label) {
  const text = String(label || '').trim().toLowerCase();
  if (text === 'tp' || text === 'true_positive') return '真实为正，模型也预测为正';
  if (text === 'tn' || text === 'true_negative') return '真实为负，模型也预测为负';
  if (text === 'fp' || text === 'false_positive') return '真实为负，模型预测为正';
  if (text === 'fn' || text === 'false_negative') return '真实为正，模型预测为负';
  return String(label).replace(/_/g, ' ') || '矩阵项';
}

function normalizeConfusionRowValue(value) {
  if (isRecord(value)) {
    return Object.entries(value)
      .map(([subKey, subValue]) => `${String(subKey)}:${formatDistributionValue(subValue)}`)
      .join(' · ');
  }
  if (Array.isArray(value)) return value.join(' / ');
  return String(value);
}

function decideMetaModelNextStep({
  canTrade = false,
  isWorking = false,
  accuracyPercent = null,
  evaluatedCount = null,
}) {
  if (!isWorking) {
    return {
      text: '继续观察',
      tone: 'neutral',
      detail: '当前样本不足，先看后续记录再复核。',
    };
  }
  if (!canTrade) {
    return {
      text: '不要当上线信号',
      tone: 'warning',
      detail: '当前仅作旁路建议，先做交易后验复核，不作为直接执行依据。',
    };
  }
  if ((accuracyPercent || 0) >= 58 && (evaluatedCount || 0) >= 100) {
    return {
      text: '治理复核',
      tone: 'warning',
      detail: '样本和命中率都较充分，可进入系统治理复核；运行态仍由风控门禁约束。',
    };
  }
  return {
    text: '继续观察',
    tone: 'neutral',
    detail: '继续补充样本后再决定是否进入下一步流程。',
  };
}

function normalizeList(value) {
  if (!value) return [];
  if (Array.isArray(value)) return value;
  if (typeof value === 'string') return [value];
  if (typeof value === 'object') {
    return Object.entries(value).map(([k, v]) => {
      if (v && typeof v === 'object') return `${k}: ${JSON.stringify(v)}`;
      return `${k}: ${v}`;
    });
  }
  return [String(value)];
}

function compactJsonText(value) {
  if (value === null || value === undefined || value === '') return '';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch (error) {
    return String(value);
  }
}

function humanizeDiagnosticKey(key, category = 'rule') {
  const text = String(key || '').trim();
  const normalized = text.toLowerCase();
  const ruleLabels = {
    compared_count: '比对样本数',
    agreement_rate: '规则/模型一致率',
    rule_accuracy: '规则命中率',
    model_accuracy_on_compared: '模型命中率',
    rule_distribution: '规则动作分布',
    disagreements: '不一致明细',
  };
  const artifactLabels = {
    snapshot_count: '快照数',
    artifact_count: '证据材料数',
    report_count: '报告数',
    latest_snapshot: '最新快照',
    latest_artifact: '最新证据',
    model_version: '模型版本',
    created_at: '生成时间',
    updated_at: '更新时间',
  };
  const dict = category === 'artifact' ? artifactLabels : ruleLabels;
  return dict[normalized] || text.replace(/_/g, ' ');
}

function formatDiagnosticValue(key, value) {
  const normalized = String(key || '').toLowerCase();
  if (Array.isArray(value)) return `共 ${value.length} 条`;
  if (typeof value === 'boolean') return value ? '是' : '否';
  if (isRecord(value)) {
    return Object.entries(value)
      .map(([subKey, subValue]) => {
        const posture = humanizePostureItem(subKey);
        const label = posture.label.startsWith('动作 ') ? String(subKey).replace(/_/g, ' ') : posture.label;
        return `${label} ${formatDistributionValue(subValue)}`;
      })
      .join('，') || '--';
  }
  if (normalized.includes('rate') || normalized.includes('accuracy') || normalized.includes('ratio')) {
    return formatAccuracy(value);
  }
  const n = safeToNumber(value);
  if (n !== null && (normalized.includes('count') || Number.isInteger(n))) return formatIntText(n);
  if (n !== null) return formatDistributionValue(n);
  return String(value || '--');
}

function formatDiagnosticListItem(item) {
  if (!isRecord(item)) return String(item || '--');
  return String(
    item.title ||
    item.label ||
    item.name ||
    item.id ||
    item.snapshot_id ||
    item.artifact_id ||
    item.summary ||
    compactJsonText(item).slice(0, 80) ||
    '--'
  );
}

function normalizeDiagnosticBlock(value, category = 'rule') {
  if (!value) return { summaryRows: [], rawText: '' };
  if (!isRecord(value)) {
    return {
      summaryRows: normalizeList(value).slice(0, 6).map((item, index) => ({
        key: `item_${index}`,
        label: `${category === 'artifact' ? '证据项' : '比对项'} ${index + 1}`,
        valueText: formatDiagnosticListItem(item),
      })),
      rawText: compactJsonText(value),
    };
  }
  const summaryRows = Object.entries(value)
    .filter(([, rowValue]) => {
      if (Array.isArray(rowValue)) return true;
      const text = typeof rowValue === 'string' ? rowValue : '';
      return text.length <= 80;
    })
    .slice(0, 8)
    .map(([key, rowValue]) => ({
      key: String(key),
      label: humanizeDiagnosticKey(key, category),
      valueText: formatDiagnosticValue(key, rowValue),
    }));
  return {
    summaryRows,
    rawText: compactJsonText(value),
  };
}

function toSnapshotRows(snapshotItems) {
  const items = Array.isArray(snapshotItems) ? snapshotItems : [];
  return items
    .map((item) => {
      const source = item || {};
      const ts = source.created_at || source.snapshot_at || source.ts || source.timestamp || source.time;
      const epoch = toEpochMs(ts);
      return { ...source, __epoch: epoch };
    })
    .sort((a, b) => (b.__epoch || 0) - (a.__epoch || 0))
    .map((item, index) => {
      const source = item || {};
      const accuracy = formatAccuracy(source.accuracy ?? source.accuracy_score ?? source.accuracyRate);
      const evaluated = formatIntText(source.evaluated_count ?? source.evaluatedCount);
      const audit = formatIntText(source.audit_count ?? source.auditCount);
      const ts = source.created_at || source.snapshot_at || source.snapshotAt || source.ts || source.timestamp || source.time;
      const summary = source.summary || source.note || source.description || '';
      const sourceName = source.snapshot_id || source.snapshotId || source.id || `${index + 1}`;
      return {
        id: String(sourceName),
        title: String(source.label || source.model_version || source.modelVersion || `snapshot-${index + 1}`),
        createdAtText: formatDateTime(ts),
        accuracyText: accuracy,
        evaluatedCountText: evaluated,
        auditCountText: audit,
        summaryText: String(summary || '--'),
        tone: toneFromStatus(source.status || source.mode || source.stage || ''),
      };
    })
    .slice(0, 6);
}

function toReportRows(raw = {}) {
  if (!isRecord(raw)) return {};
  const posture = raw.posture_distribution || raw.postureDistribution || {};
  const postureRows = Object.entries(isRecord(posture) ? posture : {}).map(([k, v]) => {
    const postureMeta = humanizePostureItem(k);
    return {
      key: String(k),
      label: postureMeta.label,
      valueText: formatDistributionValue(v),
      explanationText: postureMeta.explanation,
    };
  });

  const confusion = raw.confusion_matrix || raw.confusionMatrix || {};
  const confusionRows = Array.isArray(confusion)
    ? confusion.map((item, idx) => ({
      key: `row_${idx + 1}`,
      label: `矩阵行 ${idx + 1}`,
      valueText: normalizeConfusionRowValue(item),
    }))
    : isRecord(confusion)
      ? Object.entries(confusion).map(([k, v]) => ({
        key: String(k),
        label: normalizeConfusionMatrixKey(k),
        valueText: normalizeConfusionRowValue(v),
      }))
      : [];

  const evaluatedCount = safeToNumber(raw.evaluated_count ?? raw.evaluatedCount);
  const auditCount = safeToNumber(raw.audit_count ?? raw.auditCount);
  const evaluatedCountText = formatIntText(evaluatedCount);
  const auditCountText = formatIntText(auditCount);
  const accuracyValue = toPercentLikeNumber(raw.accuracy ?? raw.accuracy_score ?? raw.accuracyRate);
  const accuracyText = accuracyValue === null ? '--' : `${accuracyValue.toFixed(2)}%`;

  const ruleComparisonBlock = normalizeDiagnosticBlock(
    raw.rule_comparison || raw.ruleComparison || raw.rules,
    'rule'
  );
  const artifactSummaryBlock = normalizeDiagnosticBlock(
    raw.artifact_summary || raw.artifactSummary || raw.artifacts,
    'artifact'
  );

  const promotionGate = raw.promotion_gate || raw.promotionGate;
  const capabilities = raw.capabilities || {};
  const promotionGateText = isRecord(promotionGate)
    ? [
      promotionGate.eligible_for_live === false ? '上线门禁：关闭' : '',
      promotionGate.eligible_for_live === true ? '上线门禁：开启（待确认)' : '',
      promotionGate.eligible_for_governor_review === true ? '允许进入治理复核' : '',
      promotionGate.status || promotionGate.decision || '',
      promotionGate.reason || promotionGate.note || '',
      promotionGate.holdout_accuracy !== undefined ? `留出样本准确率 ${formatAccuracy(promotionGate.holdout_accuracy)}` : '',
    ].filter(Boolean).join(' · ')
    : String(promotionGate || '--');

  const isCapabilityShadowOnly = capabilities.shadow_only === true || capabilities.advisory_only === true || capabilities.live_trading === false;
  const deploymentMode = String(
    raw.model_mode ||
    raw.modelMode ||
    raw.mode ||
    raw.deployment_mode ||
    raw.deploymentMode ||
    raw.stage ||
    (isCapabilityShadowOnly ? 'shadow/advisory-only' : 'unknown')
  );
  const deploymentModeLower = deploymentMode.toLowerCase();
  const isShadowOnly = ['shadow', 'advisory', 'shadow_only'].includes(deploymentModeLower)
    || deploymentModeLower.includes('shadow') || deploymentModeLower.includes('advisory') || isCapabilityShadowOnly;
  const canTrade = isRecord(promotionGate) && normalizeBooleanLike(promotionGate.can_trade) === true;
  const canTradeFromMode = isShadowOnly ? false : canTrade;
  const eligibleForLive = isRecord(promotionGate) && promotionGate.eligible_for_live !== undefined
    ? normalizeBooleanLike(promotionGate.eligible_for_live)
    : capabilities.live_trading === false
      ? false
      : normalizeBooleanLike(raw.eligible_for_live);
  const eligibleForLiveText = formatBooleanText(eligibleForLive);
  const isWorking = (evaluatedCount !== null && evaluatedCount > 0) || (auditCount !== null && auditCount > 0);
  const canTradeFinal = canTradeFromMode || eligibleForLive === true;
  const liveTone = canTradeFinal ? 'neutral' : 'warning';
  const liveSummaryText = canTradeFinal
    ? '当前不直接实盘，可先通过治理复核。'
    : '当前不可直接实盘，仅作为旁路建议参考。';
  const workingText = isWorking
    ? `是（旁路记录 ${auditCountText}，已评估样本 ${evaluatedCountText}）`
    : '否（暂无旁路记录/样本）';
  const workingTone = isWorking ? 'positive' : 'neutral';
  const executionText = canTradeFinal ? '可作为治理复核参考' : '当前仅旁路建议';
  const tradingText = '不能直接实盘';
  const tradingTone = 'warning';
  const nextStep = decideMetaModelNextStep({
    canTrade: canTradeFinal,
    isWorking,
    accuracyPercent: accuracyValue,
    evaluatedCount: evaluatedCount || 0,
  });

  return {
    deploymentModeText: canTradeFinal ? '正常旁路评估' : '旁路建议模式',
    deploymentTone: liveTone,
    deploymentSummaryText: liveSummaryText,
    workingText,
    workingTone,
    tradingText,
    tradingTone,
    executionText,
    nextStepText: nextStep.text,
    nextStepTone: nextStep.tone,
    nextStepDetailText: nextStep.detail,
    accuracyText,
    evaluatedCountText,
    auditCountText,
    postureRows,
    confusionRows,
    ruleComparisonRows: ruleComparisonBlock.summaryRows,
    ruleComparisonRawText: ruleComparisonBlock.rawText,
    artifactSummaryRows: artifactSummaryBlock.summaryRows,
    artifactSummaryRawText: artifactSummaryBlock.rawText,
    promotionGateText,
    eligibleForLiveText,
    liveSummaryText,
  };
}

function normalizeBooleanLike(value) {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') {
    const normalized = value.trim().toLowerCase();
    if (normalized === 'false' || normalized === '0' || normalized === 'no' || normalized === 'off' || normalized === '否') {
      return false;
    }
    if (normalized === 'true' || normalized === '1' || normalized === 'yes' || normalized === 'on' || normalized === '是') {
      return true;
    }
  }
  return null;
}

function formatBooleanText(value) {
  if (value === undefined || value === null) return '--';
  return value ? '是' : '否';
}

function normalizeSnapshotPayload(raw = null) {
  if (!raw) return [];
  if (Array.isArray(raw)) return raw;
  if (isRecord(raw)) {
    if (Array.isArray(raw.items)) return raw.items;
    if (Array.isArray(raw.snapshots)) return raw.snapshots;
    if (Array.isArray(raw.data)) return raw.data;
  }
  return [];
}

function pickShadowReportPayloadFromState(state = {}, servicePayload = null) {
  const summary = state.summary || {};
  const candidates = [
    servicePayload,
    state.shadowReport,
    state.metaLightgbmShadowReport,
    state.meta_lightgbm_shadow_report,
    summary.meta_lightgbm_shadow_report,
    summary.shadow_report,
    summary.model_shadow_report,
    (summary.meta_lightgbm && summary.meta_lightgbm.shadow_report) || null,
    (summary.model_report && summary.model_report.shadow_report) || null,
  ];
  return candidates.find((entry) => isRecord(entry) && Object.keys(entry).length > 0);
}

function pickSnapshotPayloadFromState(state = {}, servicePayload = null) {
  const summary = state.summary || {};
  const candidates = [
    servicePayload,
    state.snapshots,
    state.metaLightgbmSnapshots,
    state.reportSnapshots,
    summary.snapshots,
    summary.report_snapshots,
    summary.meta_lightgbm_snapshots,
    (summary.meta_lightgbm && summary.meta_lightgbm.snapshots) || null,
    (summary.model_report && summary.model_report.snapshots) || null,
  ];
  for (const candidate of candidates) {
    const list = normalizeSnapshotPayload(candidate);
    if (list.length > 0) return list;
  }
  return [];
}

async function resolveOptionalLearningService(payloadReaders) {
  for (const reader of payloadReaders) {
    const fn = learningService[reader];
    if (typeof fn !== 'function') continue;
    try {
      const res = await fn();
      if (res) return res;
    } catch (err) {
      // Service API may not be ready yet; keep UI read-only and fallback to learning summary.
    }
  }
  return null;
}

async function refreshMetaShadowStoreData() {
  await Promise.all([
    typeof learningService.refreshMetaLightgbmShadowReport === 'function'
      ? learningService.refreshMetaLightgbmShadowReport()
      : Promise.resolve(null),
    typeof learningService.refreshMetaLightgbmShadowReportSnapshots === 'function'
      ? learningService.refreshMetaLightgbmShadowReportSnapshots()
      : Promise.resolve(null),
  ]);
}

function humanizeScopeKey(scopeKey = '') {
  if (!scopeKey) return '未命名因子';
  if (String(scopeKey).includes(':')) {
    const [factorId, regimeKey] = String(scopeKey).split(':');
    return regimeKey && regimeKey !== 'default'
      ? `${humanizeFactorId(factorId)} / ${regimeKey}`
      : `${humanizeFactorId(factorId)} / 默认模板`;
  }
  return String(scopeKey)
    .replace(/^dsl_auto_/, 'DSL 自动因子 ')
    .replace(/_/g, ' ');
}

function humanizeBoundaryScope(scope = '') {
  return String(scope || '').toLowerCase() === 'offline_deep' ? '离线深调' : '在线轻调';
}

function humanizeBoundaryReason(reason = '') {
  const key = String(reason || '').toLowerCase();
  if (key === 'fits_runtime_guardrail') return '满足当前运行态护栏';
  if (key === 'factor_not_runtime_tunable') return '该因子暂不支持运行时直接改参数';
  if (key === 'formula_version_changed') return '模板公式版本发生变化';
  if (key === 'factor_family_changed') return '模板所属因子家族发生变化';
  if (key === 'parameter_delta_too_large') return '参数跳变幅度超过在线护栏';
  if (key === 'unsupported_template_role') return '模板角色不在在线护栏允许范围内';
  return reason || '未分类边界原因';
}

function describeSuggestion(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const evidence = item.evidence || {};
  const templateDisplay = item.parameter_template_display || {};
  const confidence = Number(item.confidence || 0);
  let actionLabel = '观察';
  let impactText = '暂时不改权重，继续积累样本。';
  let reasonText = item.reason || '系统正在积累证据。';

  if (action.includes('downweight')) {
    actionLabel = '降低权重';
    impactText = '下一轮开仓会更谨慎地使用这个因子。';
  } else if (action.includes('boost')) {
    actionLabel = '提高权重';
    impactText = '下一轮开仓会更重视这个因子。';
  } else if (action.includes('quarantine')) {
    actionLabel = '隔离观察';
    impactText = '短期内减少这个因子的直接影响。';
  } else if (action.includes('retire')) {
    actionLabel = '候选退役';
    impactText = '系统倾向于让这个因子退出主要决策。';
  } else if (action.includes('tighten_gate')) {
    actionLabel = '收紧闸门';
    impactText = '后续开仓会更严格通过风险条件。';
  } else if (action.includes('watch')) {
    actionLabel = '继续观察';
    impactText = '暂时不自动改权重，只记录进经验池。';
  } else if (action.includes('switch_parameter_template')) {
    actionLabel = '切换参数模板';
  }

  if (reasonText.includes('still accumulating evidence')) {
    reasonText = '最近有表现，但证据还不够强，系统先继续观察。';
  }

  const boundary = evidence.boundary || {};
  const isTemplateSwitch = action.includes('switch_parameter_template');
  const evidenceText = isTemplateSwitch
    ? String(templateDisplay.evidence_text || '')
    : evidence.sample_count
      ? `样本 ${evidence.sample_count} 条，平均反馈 ${evidence.avg_reward ?? '--'}。`
      : '当前样本还不多，结论以观察为主。';

  let boundaryScopeLabel = '';
  let boundaryReasonText = '';
  let approvalPathText = '';
  if (isTemplateSwitch) {
    boundaryScopeLabel = String(templateDisplay.boundary_scope_label || '');
    boundaryReasonText = String(templateDisplay.boundary_reason_text || '');
    approvalPathText = String(templateDisplay.approval_path_text || '');
    impactText = String(templateDisplay.impact_text || impactText);
  }

  return {
    actionLabel,
    reasonText,
    impactText,
    evidenceText,
    confidenceText: `${Math.round(confidence * 100)}%`,
    boundaryScopeLabel,
    boundaryReasonText,
    approvalPathText,
    actionStateLabel: '',
    actionStateSummary: '',
    actionStateTargetType: '',
    actionStateTargetId: '',
    actionStateButtonText: '',
  };
}

function resolveSuggestionProgress(item = {}) {
  const backendProgress = item.progress || {};
  return {
    actionStateLabel: backendProgress.state_label || '',
    actionStateSummary: backendProgress.state_summary || '',
    actionStateTargetType: backendProgress.target_type || '',
    actionStateTargetId: backendProgress.target_id || '',
    actionStateButtonText: backendProgress.button_text || '',
  };
}

function describeReview(item = {}) {
  const outcome = String(item.outcome_label || '').toLowerCase();
  const review = item.review || {};
  let outcomeLabel = '中性结果';
  let meaningText = '这笔交易还没有形成明显的经验结论。';

  if (outcome === 'lucky_win') {
    outcomeLabel = '幸运盈利';
    meaningText = '赚到了钱，但系统认为这次更多是运气，不应该立刻放大信心。';
  } else if (outcome === 'good_win') {
    outcomeLabel = '高质量盈利';
    meaningText = '这次盈利和系统判断一致，适合作为正向经验。';
  } else if (outcome === 'good_loss') {
    outcomeLabel = '可接受亏损';
    meaningText = '虽然亏损，但执行过程仍符合规则，不一定要否定策略。';
  } else if (outcome === 'bad_loss') {
    outcomeLabel = '无效亏损';
    meaningText = '这次亏损说明策略或执行环节可能有明显问题。';
  }

  return {
    outcomeLabel,
    meaningText,
    primaryFactorText: humanizeScopeKey(review.top_factor || review.top_weight_factor || ''),
    worstFactorText: humanizeScopeKey(review.worst_factor || ''),
    primaryResponsibilityText: humanizeResponsibility(item.primary_responsibility || review.primary_responsibility || ''),
    responsibilityLabelsText: (item.responsibility_labels || review.responsibility_labels || []).map(humanizeResponsibilityLabel),
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

function humanizeResponsibilityLabel(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'entry_good_exit_bad') return '入场对但退出差';
  if (key === 'alpha_correct_but_capture_failed') return '方向对但利润捕获差';
  if (key === 'holding_too_long') return '持仓过久';
  if (key === 'regime_changed_during_hold') return '持仓期间市场切换';
  if (key === 'factor_logic_ok_but_param_suspect') return '逻辑可用但参数可疑';
  if (key === 'thesis_broken') return '交易 thesis 已失效';
  if (key === 'holding_inefficient') return '持仓效率偏低';
  return value || '未分类';
}

function describeApplication(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const status = String(item.status || 'observing').toLowerCase();
  let actionLabel = '观察生效';
  if (action.includes('downweight')) actionLabel = '降低权重已生效';
  else if (action.includes('boost')) actionLabel = '提高权重已生效';
  else if (action.includes('watch')) actionLabel = '观察策略已记录';
  else if (action.includes('quarantine')) actionLabel = '隔离策略已生效';

  let effectLabel = '观察中';
  let effectText = '系统正在等待更多样本来判断这次调整是否真的有帮助。';
  if (status === 'effective') {
    effectLabel = '已见效果';
    effectText = '应用后表现优于历史基线，这次调整正在起正向作用。';
  } else if (status === 'reinforced') {
    effectLabel = '已增强';
    effectText = '应用后持续有效，系统已经自动追加了一次增强。';
  } else if (status === 'ineffective') {
    effectLabel = '已回退';
    effectText = '应用后效果变差，系统已经自动回滚相关建议。';
  } else if (status === 'mixed') {
    effectLabel = '效果混合';
    effectText = '应用后有变化，但还不足以下明确结论。';
  }

  const observed = Number(item.observed_trade_count || 0);
  const baseline = Number(item.baseline_trade_count || 0);
  const delta = Number(item.delta_avg_reward || 0);
  const postWinRate = Number(item.post_win_rate || 0);
  const baselineWinRate = Number(item.baseline_win_rate || 0);
  const deltaText = `${delta >= 0 ? '+' : ''}${delta.toFixed(3)}`;
  const postWinText = `${Math.round(postWinRate * 100)}%`;
  const baselineWinText = `${Math.round(baselineWinRate * 100)}%`;

  return {
    actionLabel,
    scopeLabel: humanizeScopeKey(item.scope_key),
    impactText: `${item.old_weight} -> ${item.new_weight}，bias ${item.bias_multiplier}`,
    effectLabel,
    effectText,
    effectTone: toneFromStatus(status),
    effectSummary: `后验 ${observed} 笔 / 基线 ${baseline} 笔，reward Δ ${deltaText}`,
    effectStatsText: `胜率 ${postWinText}，基线 ${baselineWinText}`,
    isObservationOnly: action === 'watch',
  };
}

function isMeaningfulApplication(item = {}) {
  const action = String(item.action || '').toLowerCase();
  const bias = Number(item.bias_multiplier || 1);
  const oldWeight = Number(item.old_weight || 0);
  const newWeight = Number(item.new_weight || 0);
  if (action === 'watch') return false;
  if (Math.abs(bias - 1) < 0.000001 && Math.abs(newWeight - oldWeight) < 0.000001) return false;
  return true;
}

function humanizeCandidateStatus(status = '') {
  const key = String(status || '').toLowerCase();
  if (key === 'pending_review') return '待审候选';
  if (key === 'approved') return '已批准';
  if (key === 'rejected') return '已拒绝';
  if (key === 'deployed') return '已发布';
  if (key === 'rolled_back') return '已回滚';
  return status || '未知状态';
}

function humanizeFactorId(value = '') {
  if (!value) return '未命名模板';
  return String(value).replace(/_/g, ' ');
}

function buildLearningGovernanceTodoCard(todo = null) {
  if (!todo) return null;
  const entryType = String(todo.entry_type || '').toLowerCase();
  const recommendationId = String(todo.recommendation_id || '');
  const candidateId = String(todo.candidate_id || '');
  return {
    title: todo.title || '参数治理待办',
    stageTag: todo.stage_tag || '',
    stageTone: todo.stage_tone || 'neutral',
    actionLabel: todo.action_label || '继续推进',
    priorityLabel: todo.priority_label || '',
    prioritySummary: todo.priority_summary || '',
    summary: todo.summary || '继续积累更多治理证据。',
    queueHint: todo.queue_hint || '',
    targetType: todo.target_type || '',
    entryType,
    recommendationId,
    candidateId,
    buttonText:
      entryType === 'candidate'
        ? '查看候选'
        : recommendationId
          ? '查看建议'
          : '',
  };
}

function describeOfflineCandidate(item = {}) {
  const status = String(item.status || '').toLowerCase();
  const governance = item.governance || {};
  let stageText = '等待系统治理决定是否发布。';
  if (status === 'approved') stageText = '离线证据已通过，等待系统发布。';
  else if (status === 'deployed') stageText = '模板已经进入运行态，继续观察后验效果。';
  else if (status === 'rolled_back') stageText = '候选模板已回滚到发布前版本。';
  else if (status === 'rejected') stageText = '候选模板被拒绝，暂不进入运行态。';
  const priority = governance.priority_label
    ? {
      score: Number(governance.priority_score || 0),
      label: governance.priority_label || '',
      summary: governance.priority_summary || '',
    }
    : { score: 0, label: '', summary: '' };
  return {
    statusLabel: governance.status_label || '',
    statusTone: toneFromStatus(governance.stage_tone || ''),
    factorLabel: humanizeFactorId(item.factor_id),
    templateLabel: String(item.template_id || '').split(':')[1] || item.template_id || '未命名版本',
    evidenceText: governance.evidence_display || '',
    stageText,
    recommendationText: governance.source_summary || '',
    approvalPathText: governance.approval_path_text || '',
    reviewText: governance.review_display || '',
    deploymentText: governance.deployment_display || '',
    rollbackText: governance.rollback_display || '',
    stageText: governance.stage_summary || stageText,
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governancePriorityScore: priority.score,
    governancePriorityLabel: priority.label,
    governancePrioritySummary: priority.summary,
    governanceActionLabel: governance.action_label || '',
    governanceStageLabel: governance.stage_label || governance.status_label || '',
  };
}

function describeTemplateRecommendation(item = {}) {
  const responsibility = item.responsibility || {};
  const governance = item.governance || {};
  const labels = (responsibility.responsibility_labels || []).map(humanizeResponsibilityLabel);
  const priority = governance.priority_label
    ? {
      score: Number(governance.priority_score || 0),
      label: governance.priority_label || '',
      summary: governance.priority_summary || '',
    }
    : { score: 0, label: '', summary: '' };
  const actionButtonText = governance.action_button_text || '';
  return {
    factorLabel: humanizeFactorId(item.factor_id),
    templateLabel: String(item.target_template_version || item.target_template_id || '未命名版本'),
    roleLabel: String(item.template_role || 'default'),
    statusLabel: governance.status_label || governance.stage_label || '',
    statusTone: toneFromStatus(governance.stage_tone || ''),
    reasonText: item.reason || '系统识别到参数可疑，建议评估替代模板。',
    responsibilityText: humanizeResponsibility(responsibility.primary_responsibility || ''),
    labelText: labels.join(' / '),
    stageSummary: governance.stage_summary || '',
    actionText: governance.action_summary || '',
    actionButtonText,
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governancePriorityScore: priority.score,
    governancePriorityLabel: priority.label,
    governancePrioritySummary: priority.summary,
    governanceActionLabel: governance.action_label || '',
    actionDoneText: governance.followup_hint || '',
    actionStateLabel: '',
    actionStateSummary: '',
    actionStateDone: false,
    actionStateTargetType: '',
    actionStateTargetId: '',
    actionStateButtonText: '',
  };
}

function resolveRecommendationProgress(item = {}) {
  const backendProgress = item.progress || {};
  return {
    actionStateLabel: backendProgress.state_label || '',
    actionStateSummary: backendProgress.state_summary || '',
    actionStateDone: !!backendProgress.state_done,
    actionStateTargetType: backendProgress.target_type || '',
    actionStateTargetId: backendProgress.target_id || '',
    actionStateButtonText: backendProgress.button_text || '',
  };
}

function showGovernanceFocusMissToast(type = '', pending = {}) {
  const source = String(pending.source || '');
  const sourceText = source === 'trade_trace_timeline' ? '证据链入口' : '治理入口';
  const typeText = type === 'offline_candidate'
    ? '候选'
    : type === 'template_recommendation'
      ? '推荐'
      : type === 'parameter_lifecycle'
        ? '轨迹'
        : type === 'suggestion'
          ? '建议'
          : '对象';
  wx.showToast({
    title: `${sourceText}未找到对应${typeText}`,
    icon: 'none',
    duration: 1800,
  });
}

function findByFactor(items = [], factorId = '') {
  const targetFactor = String(factorId || '');
  if (!targetFactor) return null;
  return (items || []).find((item) => String(item.factor_id || item.factor || '') === targetFactor) || null;
}

function describeLifecycleEvent(item = {}) {
  const trace = ((item.metrics || {}).candidate_trace || {});
  const governance = item.governance || {};
  const candidateId = String(governance.candidate_id || trace.candidate_id || '');
  const recommendationId = String(governance.recommendation_id || trace.recommendation_id || '');
  return {
    factorLabel: humanizeFactorId(item.factor || ''),
    eventLabel: governance.status_label || governance.stage_label || '',
    eventTone: toneFromStatus(governance.stage_tone || ''),
    createdText: formatDateTime(item.ts),
    reasonText: governance.stage_summary || item.reason || item.description || '治理轨迹已记录。',
    recommendationText: governance.source_summary || '',
    approvalPathText: governance.approval_path_text || '',
    nextStepLabel: governance.next_step_label || '',
    nextStepSummary: governance.next_step_summary || '',
    governanceActionLabel: governance.action_label || '',
    governanceTargetTypeText: governance.target_type || '',
    linkedCandidateId: candidateId,
    linkedRecommendationId: recommendationId,
    linkedActionText: governance.button_text || governance.action_label || '',
  };
}

Page({
  data: {
    summary: {},
    summaryStatus: 'idle',
    summaryError: '',
    metaLightgbmHasReport: false,
    metaLightgbmReport: null,
    metaPostureDistributionRows: [],
    metaConfusionMatrixRows: [],
    metaRuleComparisonRows: [],
    metaRuleComparisonRawText: '',
    metaArtifactSummaryRows: [],
    metaArtifactSummaryRawText: '',
    learningPanel: '',
    learningPanelTitle: '',
    learningPanelSubtitle: '',
    metaLightgbmAccuracyText: '--',
    metaLightgbmEvaluatedCountText: '--',
    metaLightgbmAuditCountText: '--',
    metaLightgbmDeploymentSummaryText: '',
    metaLightgbmDeploymentModeText: 'unknown',
    metaLightgbmDeploymentTone: 'warning',
    metaLightgbmWorkingText: '否（暂无旁路记录/样本）',
    metaLightgbmWorkingTone: 'neutral',
    metaLightgbmTradingText: '不能直接实盘',
    metaLightgbmTradingTone: 'warning',
    metaLightgbmExecutionText: '不能直接执行',
    metaLightgbmExecutionTone: 'warning',
    metaLightgbmNextStepText: '继续观察',
    metaLightgbmNextStepTone: 'neutral',
    metaLightgbmNextStepDetailText: '先观察最近快照样本后再做更深复核。',
    metaLightgbmEligibleForLiveText: '--',
    metaLightgbmPromotionGateText: '--',
    metaLightgbmLiveSummaryText: '',
    metaLightgbmSnapshots: [],
    metaLightgbmServiceReportPayload: null,
    metaLightgbmServiceSnapshotPayload: null,
    templateOpsSummary: '',
    pendingGovernanceTodoCard: null,
    suggestions: [],
    proposedSuggestions: [],
    approvedSuggestions: [],
    rolledBackSuggestions: [],
    suggestionTab: 'proposed',
    visibleSuggestions: [],
    previewSuggestions: [],
    selectedSuggestion: null,
    reviews: [],
    previewReviews: [],
    selectedReview: null,
    allApplications: [],
    applications: [],
    previewApplications: [],
    applicationCountDisplay: 0,
    observationApplicationCount: 0,
    selectedApplication: null,
    offlineCandidates: [],
    previewOfflineCandidates: [],
    templateRecommendations: [],
    previewTemplateRecommendations: [],
    selectedTemplateRecommendation: null,
    selectedOfflineCandidate: null,
    offlineCandidateCountDisplay: 0,
    recommendationCountDisplay: 0,
    parameterTemplateEmptyStates: {
      offline_candidates: '还没有参数模板候选',
      lifecycle: '还没有参数治理轨迹',
      recommendations: '还没有参数模板建议',
    },
    parameterTemplateTaskCards: [],
    latestApplicationExpanded: false,
    closureSteps: [],
    latestApplication: null,
    lifecycleEvents: [],
    previewLifecycleEvents: [],
    selectedLifecycleEvent: null,
    lifecycleCountDisplay: 0,
    updatedAt: '--',
  },

  onLoad() {
    this._unsub = learningStore.subscribe(() => this.syncView());
    this.syncView();
    refreshLearning();
    refreshMetaShadowStoreData();
    this.refreshMetaShadowData();
  },

  async onShow() {
    await Promise.all([
      refreshLearning(),
      this.refreshMetaShadowData(),
    ]);
    this.consumeGovernanceFocus();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  async refreshMetaShadowData() {
    await refreshMetaShadowStoreData();
    const reportPayload = await resolveOptionalLearningService([
      'getMetaLightGBMShadowReport',
      'getMetaLightgbmShadowReport',
      'getLightgbmShadowReport',
      'getShadowReport',
    ]);
    const snapshotsPayload = await resolveOptionalLearningService([
      'getMetaLightGBMSnapshots',
      'getMetaLightgbmSnapshots',
      'getLearningReportSnapshots',
      'listLearningReportSnapshots',
      'getReportSnapshots',
    ]);
    const nextServiceReport = reportPayload && (
      reportPayload.shadow_report ||
      reportPayload.meta_lightgbm_shadow_report ||
      reportPayload.report ||
      (reportPayload.data && (
        reportPayload.data.shadow_report ||
        reportPayload.data.meta_lightgbm_shadow_report ||
        reportPayload.data.metaLightgbmShadowReport ||
        reportPayload.data
      )) ||
      reportPayload
    );
    const nextServiceSnapshots = snapshotsPayload && (
      snapshotsPayload.snapshots ||
      snapshotsPayload.snapshot_list ||
      snapshotsPayload.items ||
      snapshotsPayload.data ||
      snapshotsPayload
    );
    this.setData({
      metaLightgbmServiceReportPayload: nextServiceReport,
      metaLightgbmServiceSnapshotPayload: nextServiceSnapshots,
    });
    this.syncView();
  },

  syncView() {
    const state = learningStore.getState();
    const summary = state.summary || {};
    const summaryStatus = String(state.summaryStatus || 'idle');
    const summaryError = String(state.summaryError || '');
    const shadowReport = pickShadowReportPayloadFromState(state, this.data.metaLightgbmServiceReportPayload);
    const snapshotPayload = pickSnapshotPayloadFromState(state, this.data.metaLightgbmServiceSnapshotPayload);
    const hasMetaLightgbmReport = !!(shadowReport && Object.keys(shadowReport).length);
    const metaReportRows = shadowReport ? toReportRows(shadowReport) : {};
    const metaSnapshots = toSnapshotRows(snapshotPayload);
    const parameterTemplateEmptyStates = summary.parameter_template_empty_states || {};
    const parameterTemplateTaskCards = (summary.parameter_template_task_cards || []).map((item) => ({
      id: String(item.id || ''),
      index: String(item.index || ''),
      title: String(item.title || ''),
      note: String(item.note || ''),
      tone: String(item.tone || 'neutral'),
    })).filter((item) => item.id && item.title);
    const rawSuggestions = (state.suggestions || []).map((item) => ({
      ...item,
      tone: toneFromStatus(item.status),
      createdText: formatDateTime(item.created_at),
      scopeLabel: humanizeScopeKey(item.scope_key),
      ...describeSuggestion(item),
    }));
    const reviews = (state.reviews || []).map((item) => ({
      ...item,
      tone: toneFromStatus(item.outcome_label),
      createdText: formatDateTime(item.created_at),
      ...describeReview(item),
    }));
    const allApplications = (state.applications || []).map((item) => ({
      ...item,
      createdText: formatDateTime(item.created_at),
      reviewAtText: formatDateTime(item.last_review_at),
      ...describeApplication(item),
    }));
    const offlineCandidates = sortGovernanceItemsByPriority((state.offlineCandidates || []).map((item) => ({
      ...item,
      createdText: formatDateTime(item.created_at),
      updatedText: formatDateTime(item.updated_at),
      ...describeOfflineCandidate(item),
    })));
    const lifecycleEvents = (state.lifecycle || [])
      .filter((item) => item && item.source === 'parameter_template')
      .map((item) => ({
        ...item,
        ...describeLifecycleEvent(item),
      }));
    const suggestions = rawSuggestions.map((item) => ({
      ...item,
      ...resolveSuggestionProgress(item),
    }));
    const templateRecommendations = sortGovernanceItemsByPriority((state.templateRecommendations || []).map((item) => ({
      ...item,
      ...describeTemplateRecommendation(item),
      ...resolveRecommendationProgress(item),
    })));
    const applications = allApplications.filter((item) => isMeaningfulApplication(item));
    const observationApplicationCount = Math.max(0, allApplications.length - applications.length);
    const pendingGovernanceTodoCard = buildLearningGovernanceTodoCard(summary.parameter_template_todo || null);
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
      summaryStatus,
      summaryError,
      metaLightgbmHasReport: hasMetaLightgbmReport,
      metaLightgbmReport: shadowReport || {},
      metaPostureDistributionRows: metaReportRows.postureRows || [],
      metaConfusionMatrixRows: metaReportRows.confusionRows || [],
      metaRuleComparisonRows: metaReportRows.ruleComparisonRows || [],
      metaRuleComparisonRawText: metaReportRows.ruleComparisonRawText || '',
      metaArtifactSummaryRows: metaReportRows.artifactSummaryRows || [],
      metaArtifactSummaryRawText: metaReportRows.artifactSummaryRawText || '',
      metaLightgbmAccuracyText: metaReportRows.accuracyText || '--',
      metaLightgbmEvaluatedCountText: metaReportRows.evaluatedCountText || '--',
      metaLightgbmAuditCountText: metaReportRows.auditCountText || '--',
      metaLightgbmDeploymentSummaryText: metaReportRows.deploymentSummaryText || '',
      metaLightgbmDeploymentModeText: metaReportRows.deploymentModeText || 'unknown',
      metaLightgbmDeploymentTone: metaReportRows.deploymentTone || 'warning',
      metaLightgbmWorkingText: metaReportRows.workingText || '否（暂无旁路记录/样本）',
      metaLightgbmWorkingTone: metaReportRows.workingTone || 'neutral',
      metaLightgbmTradingText: metaReportRows.tradingText || '不能直接实盘',
      metaLightgbmTradingTone: metaReportRows.tradingTone || 'warning',
      metaLightgbmExecutionText: metaReportRows.executionText || '不能直接执行',
      metaLightgbmExecutionTone: metaReportRows.tradingTone || 'warning',
      metaLightgbmNextStepText: metaReportRows.nextStepText || '继续观察',
      metaLightgbmNextStepTone: metaReportRows.nextStepTone || 'neutral',
      metaLightgbmNextStepDetailText: metaReportRows.nextStepDetailText || '先观察最近快照样本后再做更深复核。',
      metaLightgbmEligibleForLiveText: metaReportRows.eligibleForLiveText || '--',
      metaLightgbmPromotionGateText: metaReportRows.promotionGateText || '--',
      metaLightgbmLiveSummaryText: metaReportRows.liveSummaryText || '',
      metaLightgbmSnapshots: metaSnapshots,
      templateOpsSummary: String(summary.parameter_template_ops_summary || ''),
      pendingGovernanceTodoCard,
      suggestions,
      proposedSuggestions,
      approvedSuggestions,
      rolledBackSuggestions,
      visibleSuggestions,
      previewSuggestions: visibleSuggestions.slice(0, 1),
      reviews,
      previewReviews: reviews.slice(0, 1),
      allApplications,
      applications,
      previewApplications: applications.slice(0, 1),
      applicationCountDisplay: applications.length,
      observationApplicationCount,
      offlineCandidates,
      previewOfflineCandidates: offlineCandidates.slice(0, 1),
      lifecycleEvents,
      previewLifecycleEvents: lifecycleEvents.slice(0, 1),
      templateRecommendations,
      previewTemplateRecommendations: templateRecommendations.slice(0, 1),
      parameterTemplateEmptyStates,
      parameterTemplateTaskCards,
      offlineCandidateCountDisplay: offlineCandidates.length,
      lifecycleCountDisplay: lifecycleEvents.length,
      recommendationCountDisplay: templateRecommendations.length,
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
          title: '系统治理',
          note: proposedSuggestions.length ? `${proposedSuggestions.length} 条待系统治理` : approvedSuggestions.length ? `${approvedSuggestions.length} 条已通过` : '暂无治理动作',
          tone: proposedSuggestions.length ? 'warning' : approvedSuggestions.length ? 'positive' : 'neutral',
        },
        {
          id: 'apply',
          index: '4',
          title: '权重应用',
          note: applications.length ? `${applications.length} 次权重应用` : '还未影响运行权重',
          tone: applications.length ? 'positive' : 'neutral',
        },
        {
          id: 'effect',
          index: '5',
          title: '效果追踪',
          note: applications.length
            ? applications[0].effectLabel
            : allApplications.length
              ? '当前以观察记录为主'
              : '等待应用后样本',
          tone: applications.length ? applications[0].effectTone : 'neutral',
        },
        ...parameterTemplateTaskCards,
      ],
      latestApplication: applications[0] || allApplications[0] || null,
      updatedAt: formatDateTime(state.updatedAt),
    });
  },

  openPendingGovernanceTodo() {
    const todo = this.data.pendingGovernanceTodoCard || null;
    if (!todo) return;
    if (todo.entryType === 'candidate' && todo.candidateId) {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(todo.candidateId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (todo.recommendationId) {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(todo.recommendationId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
    }
  },

  switchSuggestionTab(e) {
    const tab = e.currentTarget.dataset.tab;
    this.setData({ suggestionTab: tab }, () => this.syncView());
  },

  openLearningPanel(e) {
    const panel = String((e.currentTarget.dataset && e.currentTarget.dataset.panel) || '');
    const titles = {
      meta: ['模型诊断', '旁路评估、自检、原始日志与快照'],
      suggestions: ['策略建议', '按当前分段查看完整建议列表'],
      reviews: ['复盘记录', '平仓后系统对结果的解释'],
      applications: ['应用记录', '哪些建议真的影响了权重'],
      offlineCandidates: ['参数模板候选', '离线验证后的待审、已发布与已回滚模板'],
      lifecycle: ['参数治理轨迹', '推荐、候选、审批和发布的生命周期事件'],
      recommendations: ['参数模板建议', '从参数可疑证据直接长出的推荐项'],
    };
    if (!titles[panel]) return;
    this.setData({
      learningPanel: panel,
      learningPanelTitle: titles[panel][0],
      learningPanelSubtitle: titles[panel][1],
    });
  },

  closeLearningPanel() {
    this.setData({
      learningPanel: '',
      learningPanelTitle: '',
      learningPanelSubtitle: '',
    });
  },

  noop() {},

  openSuggestionDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.suggestions || []).find((x) => x.suggestion_id === id) || null;
    this.setData({ selectedSuggestion: item });
  },

  openSuggestionProgressTarget() {
    const item = this.data.selectedSuggestion || null;
    if (!item || !item.actionStateTargetType || !item.actionStateTargetId) return;
    if (item.actionStateTargetType === 'recommendation') {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(item.actionStateTargetId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
      return;
    }
    if (item.actionStateTargetType === 'candidate') {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.actionStateTargetId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
    }
  },

  openReviewDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.reviews || []).find((x) => x.review_id === id) || null;
    this.setData({ selectedReview: item });
  },

  openApplicationDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.allApplications || []).find((x) => x.application_id === id) || null;
    this.setData({ selectedApplication: item });
  },

  openOfflineCandidateDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.offlineCandidates || []).find((x) => x.candidate_id === id) || null;
    this.setData({ selectedOfflineCandidate: item });
  },

  openTemplateRecommendationDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.templateRecommendations || []).find((x) => x.recommendation_id === id) || null;
    this.setData({ selectedTemplateRecommendation: item });
  },

  openLifecycleDetail(e) {
    const id = e.currentTarget.dataset.id;
    const item = (this.data.lifecycleEvents || []).find((x) => String(x.id) === String(id)) || null;
    this.setData({ selectedLifecycleEvent: item });
  },

  openRecommendationProgressTarget() {
    const item = this.data.selectedTemplateRecommendation || null;
    if (!item || !item.actionStateTargetType || !item.actionStateTargetId) return;
    if (item.actionStateTargetType === 'candidate') {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.actionStateTargetId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (item.actionStateTargetType === 'suggestion') {
      const suggestion = (this.data.suggestions || []).find(
        (entry) => String(entry.suggestion_id) === String(item.actionStateTargetId)
      ) || null;
      if (suggestion) this.setData({ selectedSuggestion: suggestion });
      return;
    }
    if (item.actionStateTargetType === 'lifecycle') {
      const lifecycle = (this.data.lifecycleEvents || []).find(
        (entry) => String(entry.id) === String(item.actionStateTargetId)
      ) || null;
      if (lifecycle) this.setData({ selectedLifecycleEvent: lifecycle });
    }
  },

  openLifecycleProgressTarget() {
    const item = this.data.selectedLifecycleEvent || null;
    if (!item) return;
    if (item.linkedCandidateId) {
      const candidate = (this.data.offlineCandidates || []).find(
        (entry) => String(entry.candidate_id) === String(item.linkedCandidateId)
      ) || null;
      if (candidate) this.setData({ selectedOfflineCandidate: candidate });
      return;
    }
    if (item.linkedRecommendationId) {
      const recommendation = (this.data.templateRecommendations || []).find(
        (entry) => String(entry.recommendation_id) === String(item.linkedRecommendationId)
      ) || null;
      if (recommendation) this.setData({ selectedTemplateRecommendation: recommendation });
    }
  },

  toggleLatestApplicationDetail() {
    this.setData({ latestApplicationExpanded: !this.data.latestApplicationExpanded });
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

  closeOfflineCandidateDetail() {
    this.setData({ selectedOfflineCandidate: null });
  },

  closeTemplateRecommendationDetail() {
    this.setData({ selectedTemplateRecommendation: null });
  },

  closeLifecycleDetail() {
    this.setData({ selectedLifecycleEvent: null });
  },

  consumeGovernanceFocus() {
    const pending = consumeLearningGovernanceFocus();
    if (!pending) return;
    const type = String(pending.type || '');
    if (type === 'suggestion') {
      const item = (this.data.suggestions || []).find(
        (x) => String(x.suggestion_id) === String(pending.suggestionId || '')
      ) || findByFactor(this.data.suggestions || [], pending.factorId);
      if (item) {
        this.setData({ selectedSuggestion: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'template_recommendation') {
      const item = (this.data.templateRecommendations || []).find(
        (x) => String(x.recommendation_id) === String(pending.recommendationId || '')
      ) || findByFactor(this.data.templateRecommendations || [], pending.factorId);
      if (item) {
        this.setData({ selectedTemplateRecommendation: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'offline_candidate') {
      const item = (this.data.offlineCandidates || []).find(
        (x) => String(x.candidate_id) === String(pending.candidateId || '')
      ) || findByFactor(this.data.offlineCandidates || [], pending.factorId);
      if (item) {
        this.setData({ selectedOfflineCandidate: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
      return;
    }
    if (type === 'parameter_lifecycle') {
      const item = (this.data.lifecycleEvents || []).find(
        (x) => String(x.id) === String(pending.lifecycleEventId || '')
      ) || findByFactor(this.data.lifecycleEvents || [], pending.factorId);
      if (item) {
        this.setData({ selectedLifecycleEvent: item });
      } else {
        showGovernanceFocusMissToast(type, pending);
      }
    }
  },

  openTraceFromReview() {
    const review = this.data.selectedReview || null;
    const locator = (review && review.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || review.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromLifecycle() {
    const event = this.data.selectedLifecycleEvent || null;
    const locator = (((event || {}).metrics || {}).candidate_trace || {}).trace_locator || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromTemplateRecommendation() {
    const item = this.data.selectedTemplateRecommendation || null;
    const locator = (item && item.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },

  openTraceFromOfflineCandidate() {
    const item = this.data.selectedOfflineCandidate || null;
    const locator = (item && item.trace_locator) || {};
    openTradeTracePage({
      positionId: locator.position_id || '',
      decisionId: locator.exit_decision_id || locator.entry_decision_id || '',
    });
  },
});
