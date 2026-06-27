import { clearToken, loadToken, post, get, setToken } from './client';
import sessionStore from '../stores/session';

export async function bootstrapAuth() {
  const token = loadToken();
  if (!token) return false;
  try {
    const user = await get('/api/auth/me');
    sessionStore.setState({ token, user, isAuthenticated: true, busy: false });
    return true;
  } catch (err) {
    clearToken();
    return false;
  }
}

export async function login(username, password) {
  sessionStore.setState({ busy: true });
  try {
    const result = await post('/api/auth/login', { username, password }, { skipAuth: true, timeout: 20000 });
    const token = result.token || result.access_token || '';
    if (!token) throw new Error('missing_token');
    setToken(token);
    const user = await get('/api/auth/me');
    sessionStore.setState({ token, user, isAuthenticated: true, busy: false });
    return true;
  } catch (err) {
    clearToken();
    sessionStore.setState({ busy: false, isAuthenticated: false });
    return false;
  }
}

export function logout() {
  clearToken();
}
