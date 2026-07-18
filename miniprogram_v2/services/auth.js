import { clearToken, loadToken, post, get, setAuthTokens } from './client';
import sessionStore from '../stores/session';
import { authStateAfterMeFailure } from './authState';

export async function bootstrapAuth() {
  const token = loadToken();
  if (!token) return false;
  try {
    const user = await get('/api/auth/me');
    sessionStore.setState({ token, user, isAuthenticated: true, busy: false });
    return true;
  } catch (err) {
    const result = authStateAfterMeFailure(sessionStore.getState(), token, err && err.statusCode);
    if (result.clearToken) clearToken();
    sessionStore.setState(result.statePatch);
    return result.authenticated;
  }
}

export async function login(username, password) {
  sessionStore.setState({ busy: true });
  let token = '';
  try {
    const result = await post('/api/auth/login', { username, password }, { skipAuth: true, timeout: 20000 });
    token = result.access_token || result.token || '';
    if (!token) throw new Error('missing_token');
    setAuthTokens(token, result.refresh_token || '');
  } catch (err) {
    clearToken();
    sessionStore.setState({ busy: false, isAuthenticated: false });
    return false;
  }

  try {
    const user = await get('/api/auth/me');
    sessionStore.setState({ token, user, isAuthenticated: true, busy: false });
    return true;
  } catch (err) {
    const result = authStateAfterMeFailure(sessionStore.getState(), token, err && err.statusCode);
    if (result.clearToken) clearToken();
    sessionStore.setState(result.statePatch);
    return result.authenticated;
  }
}

export async function logout() {
  try {
    await post('/api/auth/logout', {
      refresh_token: wx.getStorageSync('refresh_token') || '',
    });
  } catch (err) {
    // Local revocation must still complete if the server is unavailable.
  }
  clearToken();
  sessionStore.setState({ token: '', user: null, isAuthenticated: false, busy: false });
}
