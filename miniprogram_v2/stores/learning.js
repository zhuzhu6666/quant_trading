import { createStore } from '../utils/store';

const store = createStore({
  summary: null,
  suggestions: [],
  applications: [],
  reviews: [],
  lifecycle: [],
  updatedAt: 0,
});

export default store;
