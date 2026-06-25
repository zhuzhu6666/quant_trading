import { get, post } from './client';
import learningStore from '../stores/learning';

export async function refreshLearning() {
  const [summary, suggestionsRes, applicationsRes, reviewsRes, lifecycleRes, offlineCandidatesRes, recommendationsRes] = await Promise.all([
    get('/api/learning/summary').catch(() => null),
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

export async function runLearningGovernance() {
  const result = await post('/api/learning/govern/run', {});
  await refreshLearning();
  return result;
}

export async function reviewSuggestion(suggestionId, status, note = '') {
  const result = await post('/api/learning/review', {
    suggestion_id: suggestionId,
    status,
    note,
  });
  await refreshLearning();
  return result;
}

export async function materializeTemplateRecommendation(recommendationId, note = '') {
  const result = await post('/api/learning/parameter-templates/recommendations/materialize', {
    recommendation_id: recommendationId,
    note,
  });
  await refreshLearning();
  return result;
}

export async function reviewOfflineCandidate(candidateId, status, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/review', {
    candidate_id: candidateId,
    status,
    note,
  });
  await refreshLearning();
  return result;
}

export async function releaseOfflineCandidate(candidateId, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/release', {
    candidate_id: candidateId,
    note,
  });
  await refreshLearning();
  return result;
}

export async function rollbackOfflineCandidate(candidateId, note = '') {
  const result = await post('/api/learning/parameter-templates/offline-candidates/rollback', {
    candidate_id: candidateId,
    note,
  });
  await refreshLearning();
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
