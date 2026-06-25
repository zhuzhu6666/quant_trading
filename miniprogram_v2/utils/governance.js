const STAGE_META = {
  online_light: {
    label: '在线轻调',
    summary: '当前可以继续生成建议并走受控审批切换。',
  },
  offline_deep: {
    label: '离线深调',
    summary: '当前不能直接上线，必须先走离线验证。',
  },
  pending_review: {
    label: '待审候选',
    summary: '离线证据已经形成，当前重点是人工审核。',
  },
  approved: {
    label: '等待发布',
    summary: '候选已通过审核，下一步应推进灰度发布。',
  },
  deployed: {
    label: '发布观察',
    summary: '模板已进运行态，当前重点是观察效果与回滚信号。',
  },
  rolled_back: {
    label: '已回滚',
    summary: '这条参数治理链已经回滚，当前应回到离线复核。',
  },
  rejected: {
    label: '已拒绝',
    summary: '候选未通过审核，当前以保留证据继续观察为主。',
  },
  processing: {
    label: '候选处理中',
    summary: '当前参数治理对象已经进入候选链，继续围绕候选状态推进。',
  },
  no_chain: {
    label: '未进入治理链',
    summary: '这笔交易还没有形成明确的参数治理对象，下一步仍以继续观察和补样本为主。',
  },
  pending_evidence: {
    label: '参数问题待收敛',
    summary: '这笔交易已经暴露出参数问题线索，但还没有形成可执行的模板推荐或候选。',
  },
};

export function humanizeBoundaryScope(scope = '') {
  return String(scope || '').toLowerCase() === 'offline_deep' ? '离线深调' : '在线轻调';
}

export function normalizeGovernanceStageKey(value = '') {
  const normalized = String(value || '').trim().toLowerCase();
  if (!normalized) return '';
  if (['在线轻调', '在线轻调推荐', 'online_light'].includes(normalized)) return 'online_light';
  if (['离线深调', '离线深调推荐', 'offline_deep'].includes(normalized)) return 'offline_deep';
  if (['待审候选', '候选待审', 'pending_review'].includes(normalized)) return 'pending_review';
  if (['等待发布', '等待灰度发布', 'approved'].includes(normalized)) return 'approved';
  if (['发布观察', '发布后观察', 'deployed'].includes(normalized)) return 'deployed';
  if (['已回滚', '已回滚待复核', 'rolled_back'].includes(normalized)) return 'rolled_back';
  if (['已拒绝', '候选已拒绝', 'rejected'].includes(normalized)) return 'rejected';
  if (['候选处理中', 'processing'].includes(normalized)) return 'processing';
  if (['未进入治理链', 'no_chain'].includes(normalized)) return 'no_chain';
  if (['参数问题待收敛', 'pending_evidence'].includes(normalized)) return 'pending_evidence';
  return 'other';
}

export function describeGovernanceStage(stage = '') {
  const key = normalizeGovernanceStageKey(stage);
  return (STAGE_META[key] && STAGE_META[key].label) || String(stage || '').trim();
}

export function describeGovernanceStageSummary(stage = '') {
  const key = normalizeGovernanceStageKey(stage);
  return (STAGE_META[key] && STAGE_META[key].summary) || '';
}

export function describeCandidateStage(item = {}) {
  const status = String(item.status || '').toLowerCase();
  if (status === 'pending_review') return STAGE_META.pending_review.label;
  if (status === 'approved') return STAGE_META.approved.label;
  if (status === 'deployed') return STAGE_META.deployed.label;
  if (status === 'rolled_back') return STAGE_META.rolled_back.label;
  if (status === 'rejected') return STAGE_META.rejected.label;
  return STAGE_META.processing.label;
}

export function describeRecommendationStage(item = {}) {
  return String((((item.boundary || {}).recommended_scope) || '')).toLowerCase() === 'offline_deep'
    ? STAGE_META.offline_deep.label
    : STAGE_META.online_light.label;
}

export function describeCandidateNextStep(item = {}) {
  const status = String(item.status || '').toLowerCase();
  if (status === 'pending_review') return '下一步等待人工审核，通过后才允许灰度发布。';
  if (status === 'approved') return '下一步执行灰度发布，并继续观察后验效果。';
  if (status === 'deployed') return '下一步观察发布后的 reward 和胜率表现。';
  if (status === 'rolled_back') return '下一步复核回滚原因，再决定是否重新离线调参。';
  if (status === 'rejected') return '下一步保留证据观察，等待更多样本后再发起候选。';
  return '下一步继续积累更多治理证据。';
}

export function describeRecommendationNextStep(item = {}) {
  const scope = String((((item.boundary || {}).recommended_scope) || '')).toLowerCase();
  if (scope === 'offline_deep') {
    return '下一步先做离线验证，验证通过后再登记灰度候选。';
  }
  return '下一步生成治理建议，走 governor 审批后受控切到运行态。';
}

export function describeGovernanceTargetType(type = '') {
  const normalized = String(type || '').toLowerCase();
  if (normalized === 'candidate') return '模板候选';
  if (normalized === 'recommendation') return '参数推荐';
  if (normalized === 'suggestion') return '治理建议';
  if (normalized === 'parameter_lifecycle') return '治理轨迹';
  return '';
}

export function describeGovernanceActionLabel({ entryType = '', stage = '' } = {}) {
  const normalizedType = String(entryType || '').toLowerCase();
  const stageKey = normalizeGovernanceStageKey(stage);
  if (normalizedType === 'candidate') {
    if (stageKey === 'pending_review') return '去审候选';
    if (stageKey === 'approved') return '去发布';
    if (stageKey === 'deployed') return '看观察';
    if (stageKey === 'rolled_back') return '看回滚';
    return '看候选';
  }
  if (normalizedType === 'recommendation') {
    if (stageKey === 'online_light') return '去审建议';
    if (stageKey === 'offline_deep') return '去做验证';
    return '看建议';
  }
  if (normalizedType === 'suggestion') return '看建议';
  if (normalizedType === 'parameter_lifecycle') return '看轨迹';
  return '打开治理';
}

export function describeGovernancePriority({ entryType = '', stage = '', hasGovernanceFactor = false } = {}) {
  const normalizedType = String(entryType || '').toLowerCase();
  const normalizedStage = describeGovernanceStage(stage || '');
  if (normalizedType === 'candidate' && normalizedStage === '待审候选') {
    return {
      score: 100,
      label: '优先审核',
      summary: '这条治理对象已经形成候选，优先把人工审核处理掉。',
    };
  }
  if (normalizedType === 'candidate' && normalizedStage === '等待发布') {
    return {
      score: 90,
      label: '优先发布',
      summary: '这条治理对象已经批准，下一步应推进灰度发布。',
    };
  }
  if (normalizedType === 'recommendation' && normalizedStage === '离线深调') {
    return {
      score: 80,
      label: '优先验证',
      summary: '这条治理对象当前应先走离线验证，不能直接切线上。',
    };
  }
  if (normalizedType === 'recommendation' && normalizedStage === '在线轻调') {
    return {
      score: 70,
      label: '优先审建议',
      summary: '这条治理对象已满足在线轻调边界，可继续生成或审批治理建议。',
    };
  }
  if (normalizedType === 'candidate' && normalizedStage === '发布观察') {
    return {
      score: 60,
      label: '优先观察',
      summary: '这条治理对象已经上线，当前重点是观察效果与回滚信号。',
    };
  }
  if (normalizedType === 'candidate' && normalizedStage === '已回滚') {
    return {
      score: 50,
      label: '优先复核',
      summary: '这条治理对象已经回滚，当前应先回到离线复核。',
    };
  }
  if (hasGovernanceFactor) {
    return {
      score: 40,
      label: '继续收敛',
      summary: '这条样本已经露出参数问题线索，但还没有形成更具体的治理对象。',
    };
  }
  return {
    score: 0,
    label: '',
    summary: '',
  };
}

export function sortGovernanceItemsByPriority(items = [], {
  scoreField = 'governancePriorityScore',
  tsField = 'created_at',
} = {}) {
  return [...items].sort((a, b) => {
    const scoreDiff = Number(b[scoreField] || 0) - Number(a[scoreField] || 0);
    if (scoreDiff !== 0) return scoreDiff;
    return Number(b[tsField] || 0) - Number(a[tsField] || 0);
  });
}

export function buildGovernanceTodoCard(items = [], {
  scoreField = 'governancePriorityScore',
  titleField = 'title',
  factorIdField = 'parameter_governance_factor',
  candidateIdField = 'governanceCandidateId',
  recommendationIdField = 'governanceRecommendationId',
  priorityLabelField = 'governancePriorityLabel',
  stageTagField = 'governanceStageTag',
  targetTypeField = 'governanceTargetTypeText',
  actionLabelField = 'governanceActionLabel',
  summaryField = 'governancePrioritySummary',
  fallbackSummaryField = 'governanceStageSummary',
} = {}) {
  const governanceItems = items.filter((item) => Number(item[scoreField] || 0) > 0);
  if (!governanceItems.length) return null;
  const sorted = sortGovernanceItemsByPriority(governanceItems, { scoreField });
  const primary = sorted[0] || null;
  if (!primary) return null;
  return {
    title: primary[titleField] || '',
    factorId: String(primary[factorIdField] || ''),
    candidateId: String(primary[candidateIdField] || ''),
    recommendationId: String(primary[recommendationIdField] || ''),
    priorityLabel: primary[priorityLabelField] || '',
    stageTag: primary[stageTagField] || '',
    targetTypeText: primary[targetTypeField] || '',
    actionLabel: primary[actionLabelField] || '打开治理',
    summary: primary[summaryField] || primary[fallbackSummaryField] || '',
    queueHint: sorted.length > 1 ? `除此之外还有 ${sorted.length - 1} 条带治理优先级的对象可继续处理。` : '当前没有更多更高优先级的治理对象。',
  };
}
