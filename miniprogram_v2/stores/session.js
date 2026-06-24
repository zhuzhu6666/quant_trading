import { createStore } from '../utils/store';

const store = createStore({
  token: '',
  user: null,
  isAuthenticated: false,
  busy: false,
});

export default store;
