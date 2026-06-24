import { get, post } from './client';
import learningStore from '../stores/learning';

export async function refreshLearning() {
  const [summary, suggestionsRes, applicationsRes, reviewsRes, lifecycleRes] = await Promise.all([
    get('/api/learning/summary').catch(() => null),
    get('/api/learning/suggestions?limit=50').catch(() => null),
    get('/api/learning/applications?limit=50').catch(() => null),
    get('/api/learning/reviews?limit=20').catch(() => null),
    get('/api/learning/lifecycle?limit=60').catch(() => null),
  ]);

  learningStore.setState({
    summary: summary || { suggestions: {}, reviews: {}, applications: 0, latest_review: null },
    suggestions: (suggestionsRes && suggestionsRes.items) || [],
    applications: (applicationsRes && applicationsRes.items) || [],
    reviews: (reviewsRes && reviewsRes.items) || [],
    lifecycle: (lifecycleRes && lifecycleRes.items) || [],
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
