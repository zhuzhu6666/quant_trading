import { createStore } from '../utils/store';

const store = createStore({
  summary: null,
  summaryStatus: 'idle',
  summaryError: '',
  suggestions: [],
  applications: [],
  reviews: [],
  lifecycle: [],
  offlineCandidates: [],
  templateRecommendations: [],
  pendingGovernanceFocus: null,
  updatedAt: 0,
});

export default store;
