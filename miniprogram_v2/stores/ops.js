import { createStore } from '../utils/store';

const store = createStore({
  backendReadiness: null,
  backendReadinessView: null,
  backendReadinessStatus: 'idle',
  backendReadinessError: '',
  updatedAt: 0,
});

export default store;
