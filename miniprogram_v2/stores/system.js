import { createStore } from '../utils/store';

const store = createStore({
  scheduler: null,
  evolution: null,
  factorHealth: null,
  factorStats: null,
  factorWeights: [],
  dbHealth: null,
  apiHealth: null,
  riskSummary: null,
  recentTradeTraces: [],
  pendingTradeTraceQuery: null,
  recentTradeTraceQueries: [],
  updatedAt: 0,
});

export default store;
