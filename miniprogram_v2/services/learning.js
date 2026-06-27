import { get, post } from './client';
import learningStore from '../stores/learning';
import {
  buildMetaShadowReportSnapshotsView,
  buildMetaShadowReportView,
  buildOffmarketHighLoadAuditsView,
} from '../utils/backendReadiness';

const LEARNING_REFRESH_TTL = 30000;
let refreshInFlight = null;
let lastRefreshAt = 0;

const MODEL_SHADOW_REPORT_TTL = 30000;
const MODEL_SHADOW_REPORT_SNAPSHOTS_TTL = 30000;
const OFFMARKET_HIGH_LOAD_AUDITS_TTL = 30000;

let modelShadowReportRefreshInFlight = null;
let modelShadowReportLastRefreshAt = 0;
let modelShadowReportLastQuery = '';

let modelShadowReportSnapshotsRefreshInFlight = null;
let modelShadowReportSnapshotsLastRefreshAt = 0;
let modelShadowReportSnapshotsLastQuery = '';

let offmarketHighLoadAuditsRefreshInFlight = null;
let offmarketHighLoadAuditsLastRefreshAt = 0;
let offmarketHighLoadAuditsLastQuery = '';

function buildLearningQueryPairs(params = {}) {
  const pairs = [];
  const pushIf = (key, value) => {
    if (value === undefined || value === null || value === '') return;
    pairs.push(`${encodeURIComponent(key)}=${encodeURIComponent(String(value))}`);
  };
  pushIf('limit', params.limit);
  pushIf('posture', params.posture);
  if (params.include_samples !== undefined) {
    pushIf('include_samples', !!params.include_samples);
  }
  if (params.job_name !== undefined && params.job_name !== null && params.job_name !== '') {
    pushIf('job_name', params.job_name);
  }
  return pairs.length ? `?${pairs.join('&')}` : '';
}

function buildQueryKey(params = {}) {
  return JSON.stringify({
    limit: params.limit === undefined || params.limit === null ? 0 : params.limit,
    posture: String(params.posture || ''),
    include_samples: params.include_samples === false ? 0 : 1,
    job_name: String(params.job_name || ''),
  });
}

async function fetchLearningState() {
  let summaryStatus = 'ok';
  let summaryError = '';
  const summaryPromise = get('/api/learning/summary').catch((err) => {
    summaryStatus = 'error';
    summaryError = String((err && err.errMsg) || (err && err.message) || '学习摘要拉取失败');
    return null;
  });
  const [summary, suggestionsRes, applicationsRes, reviewsRes, lifecycleRes, offlineCandidatesRes, recommendationsRes] = await Promise.all([
    summaryPromise,
    get('/api/learning/suggestions?limit=50').catch(() => null),
    get('/api/learning/applications?limit=50').catch(() => null),
    get('/api/learning/reviews?limit=20').catch(() => null),
    get('/api/learning/lifecycle?limit=60').catch(() => null),
    get('/api/learning/parameter-templates/offline-candidates?limit=20').catch(() => null),
    get('/api/learning/parameter-templates/recommendations?limit=20').catch(() => null),
  ]);

  learningStore.setState({
    summary: summary || {
      suggestions: {},
      reviews: {},
      applications: 0,
      latest_review: null,
      parameter_template_candidates: {},
      latest_parameter_template_candidate: null,
      latest_parameter_template_candidate_trace: null,
      parameter_template_recommendations: { total: 0, online_light: 0, offline_deep: 0 },
      parameter_template_ops_summary: '',
      latest_parameter_template_recommendation: null,
    },
    summaryStatus,
    summaryError,
    suggestions: (suggestionsRes && suggestionsRes.items) || [],
    applications: (applicationsRes && applicationsRes.items) || [],
    reviews: (reviewsRes && reviewsRes.items) || [],
    lifecycle: (lifecycleRes && lifecycleRes.items) || [],
    offlineCandidates: (offlineCandidatesRes && offlineCandidatesRes.items) || [],
    templateRecommendations: (recommendationsRes && recommendationsRes.items) || [],
    updatedAt: Date.now(),
  });
  return learningStore.getState();
}

export async function fetchMetaLightgbmShadowReport(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 200,
    posture: options.posture,
    include_samples: options.includeSamples !== false,
  });
  return get(`/api/learning/model/meta-lightgbm/shadow-report${query}`);
}

export async function fetchMetaLightgbmShadowReportSnapshots(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 20,
  });
  return get(`/api/learning/model/meta-lightgbm/shadow-report/snapshots${query}`);
}

export async function fetchOffmarketHighLoadAudits(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 50,
    job_name: options.jobName,
  });
  return get(`/api/learning/model/offmarket-high-load/audits${query}`);
}

async function fetchModelShadowReportState(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 200,
    posture: options.posture,
    include_samples: options.includeSamples !== false,
  });
  learningStore.setState({
    metaLightgbmShadowReportStatus: 'loading',
    metaLightgbmShadowReportError: '',
  });
  let status = 'ok';
  let error = '';
  let raw = null;
  try {
    raw = await get(`/api/learning/model/meta-lightgbm/shadow-report${query}`);
  } catch (err) {
    status = 'error';
    error = String((err && err.errMsg) || (err && err.message) || '模型 shadow 报告拉取失败');
  }
  const state = buildMetaShadowReportView(raw || {});
  learningStore.setState({
    metaLightgbmShadowReport: raw,
    metaLightgbmShadowReportView: state,
    metaLightgbmShadowReportStatus: status,
    metaLightgbmShadowReportError: error,
    updatedAt: Date.now(),
  });
  return learningStore.getState();
}

async function fetchModelShadowReportSnapshotsState(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 20,
  });
  learningStore.setState({
    metaLightgbmShadowReportSnapshotsStatus: 'loading',
    metaLightgbmShadowReportSnapshotsError: '',
  });
  let status = 'ok';
  let error = '';
  let raw = null;
  try {
    raw = await get(`/api/learning/model/meta-lightgbm/shadow-report/snapshots${query}`);
  } catch (err) {
    status = 'error';
    error = String((err && err.errMsg) || (err && err.message) || '模型 shadow 快照拉取失败');
  }
  const state = buildMetaShadowReportSnapshotsView(raw || {});
  learningStore.setState({
    metaLightgbmShadowReportSnapshots: (raw && raw.items) || [],
    metaLightgbmShadowReportSnapshotsView: state,
    metaLightgbmShadowReportSnapshotsStatus: status,
    metaLightgbmShadowReportSnapshotsError: error,
    updatedAt: Date.now(),
  });
  return learningStore.getState();
}

async function fetchOffmarketHighLoadAuditsState(options = {}) {
  const query = buildLearningQueryPairs({
    limit: options.limit || 50,
    job_name: options.jobName,
  });
  learningStore.setState({
    offmarketHighLoadAuditsStatus: 'loading',
    offmarketHighLoadAuditsError: '',
  });
  let status = 'ok';
  let error = '';
  let raw = null;
  try {
    raw = await get(`/api/learning/model/offmarket-high-load/audits${query}`);
  } catch (err) {
    status = 'error';
    error = String((err && err.errMsg) || (err && err.message) || '高负载审计拉取失败');
  }
  const state = buildOffmarketHighLoadAuditsView(raw || {});
  learningStore.setState({
    offmarketHighLoadAudits: (raw && raw.items) || [],
    offmarketHighLoadAuditsView: state,
    offmarketHighLoadAuditsStatus: status,
    offmarketHighLoadAuditsError: error,
    updatedAt: Date.now(),
  });
  return learningStore.getState();
}

export async function refreshMetaLightgbmShadowReport(options = {}) {
  const force = !!options.force;
  const now = Date.now();
  const key = buildQueryKey({
    limit: options.limit || 200,
    posture: options.posture,
    include_samples: options.includeSamples !== false,
    job_name: '',
  });
  if (!force && modelShadowReportRefreshInFlight && modelShadowReportLastQuery === key) {
    return modelShadowReportRefreshInFlight;
  }
  if (!force && modelShadowReportLastRefreshAt > 0 && modelShadowReportLastQuery === key && (now - modelShadowReportLastRefreshAt) < MODEL_SHADOW_REPORT_TTL) {
    return learningStore.getState();
  }

  modelShadowReportRefreshInFlight = (async () => {
    try {
      const nextState = await fetchModelShadowReportState(options);
      modelShadowReportLastRefreshAt = Date.now();
      modelShadowReportLastQuery = key;
      return nextState;
    } finally {
      modelShadowReportRefreshInFlight = null;
    }
  })();
  return modelShadowReportRefreshInFlight;
}

export async function refreshMetaLightgbmShadowReportSnapshots(options = {}) {
  const force = !!options.force;
  const now = Date.now();
  const key = buildQueryKey({
    limit: options.limit || 20,
    posture: '',
    include_samples: false,
    job_name: '',
  });
  if (!force && modelShadowReportSnapshotsRefreshInFlight && modelShadowReportSnapshotsLastQuery === key) {
    return modelShadowReportSnapshotsRefreshInFlight;
  }
  if (!force && modelShadowReportSnapshotsLastRefreshAt > 0 && modelShadowReportSnapshotsLastQuery === key && (now - modelShadowReportSnapshotsLastRefreshAt) < MODEL_SHADOW_REPORT_SNAPSHOTS_TTL) {
    return learningStore.getState();
  }

  modelShadowReportSnapshotsRefreshInFlight = (async () => {
    try {
      const nextState = await fetchModelShadowReportSnapshotsState(options);
      modelShadowReportSnapshotsLastRefreshAt = Date.now();
      modelShadowReportSnapshotsLastQuery = key;
      return nextState;
    } finally {
      modelShadowReportSnapshotsRefreshInFlight = null;
    }
  })();
  return modelShadowReportSnapshotsRefreshInFlight;
}

export async function refreshOffmarketHighLoadAudits(options = {}) {
  const force = !!options.force;
  const now = Date.now();
  const key = buildQueryKey({
    limit: options.limit || 50,
    posture: '',
    include_samples: true,
    job_name: options.jobName,
  });
  if (!force && offmarketHighLoadAuditsRefreshInFlight && offmarketHighLoadAuditsLastQuery === key) {
    return offmarketHighLoadAuditsRefreshInFlight;
  }
  if (!force && offmarketHighLoadAuditsLastRefreshAt > 0 && offmarketHighLoadAuditsLastQuery === key && (now - offmarketHighLoadAuditsLastRefreshAt) < OFFMARKET_HIGH_LOAD_AUDITS_TTL) {
    return learningStore.getState();
  }

  offmarketHighLoadAuditsRefreshInFlight = (async () => {
    try {
      const nextState = await fetchOffmarketHighLoadAuditsState(options);
      offmarketHighLoadAuditsLastRefreshAt = Date.now();
      offmarketHighLoadAuditsLastQuery = key;
      return nextState;
    } finally {
      offmarketHighLoadAuditsRefreshInFlight = null;
    }
  })();
  return offmarketHighLoadAuditsRefreshInFlight;
}

export async function refreshLearning(options = {}) {
  const force = !!options.force;
  const now = Date.now();
  if (!force && refreshInFlight) {
    return refreshInFlight;
  }
  if (!force && lastRefreshAt > 0 && (now - lastRefreshAt) < LEARNING_REFRESH_TTL) {
    return learningStore.getState();
  }
  refreshInFlight = (async () => {
    try {
      const nextState = await fetchLearningState();
      lastRefreshAt = Date.now();
      return nextState;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function runLearningGovernance() {
  const result = await post('/api/learning/govern/run', {});
  await refreshLearning({ force: true });
  return result;
}

export async function reviewSuggestion(suggestionId, status, note = '') {
  const result = await post('/api/learning/review', {
    suggestion_id: suggestionId,
    status,
    note,
  });
  await refreshLearning({ force: true });
  return result;
}

export async function materializeTemplateRecommendation(recommendationId, note = '') {
  const result = await post('/api/learning/parameter-templates/recommendations/materialize', {
    recommendation_id: recommendationId,
    note,
  });
  await refreshLearning({ force: true });
  return result;
}

export async function reviewOfflineCandidate(candidateId, status, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/review', {
    candidate_id: candidateId,
    status,
    note,
  });
  await refreshLearning({ force: true });
  return result;
}

export async function releaseOfflineCandidate(candidateId, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/release', {
    candidate_id: candidateId,
    note,
  });
  await refreshLearning({ force: true });
  return result;
}

export async function rollbackOfflineCandidate(candidateId, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/rollback', {
    candidate_id: candidateId,
    note,
  });
  await refreshLearning({ force: true });
  return result;
}

export function stageLearningGovernanceFocus(focus = {}) {
  const normalized = {
    type: String(focus.type || ''),
    suggestionId: String(focus.suggestionId || ''),
    recommendationId: String(focus.recommendationId || ''),
    candidateId: String(focus.candidateId || ''),
    lifecycleEventId: String(focus.lifecycleEventId || ''),
    factorId: String(focus.factorId || ''),
    source: String(focus.source || ''),
    ts: Date.now(),
  };
  if (!normalized.type) return false;
  learningStore.setState({
    pendingGovernanceFocus: normalized,
  });
  return true;
}

export function consumeLearningGovernanceFocus() {
  const state = learningStore.getState();
  const pending = state.pendingGovernanceFocus || null;
  if (pending) {
    learningStore.setState({ pendingGovernanceFocus: null });
  }
  return pending;
}

export function openLearningGovernancePage(focus = {}) {
  const staged = stageLearningGovernanceFocus(focus);
  if (!staged) return false;
  wx.switchTab({ url: '/pages/learning/index' });
  return true;
}
