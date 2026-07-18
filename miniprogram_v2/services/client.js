import CONFIG from '../utils/config';
import sessionStore from '../stores/session';

let tokenCache = '';
let unauthorizedRedirecting = false;
let unauthorizedInFlight = null;
let refreshInFlight = null;

function getToken() {
  return tokenCache || wx.getStorageSync('jwt_token') || '';
}

export function setToken(token) {
  tokenCache = token || '';
  if (tokenCache) {
    wx.setStorageSync('jwt_token', tokenCache);
    sessionStore.setState({ token: tokenCache });
    return;
  }
  wx.removeStorageSync('jwt_token');
  sessionStore.setState({ token: '', user: null, isAuthenticated: false });
}

export function setRefreshToken(token) {
  if (token) {
    wx.setStorageSync('refresh_token', token);
  } else {
    wx.removeStorageSync('refresh_token');
  }
}

export function setAuthTokens(accessToken, refreshToken = '') {
  setToken(accessToken);
  if (refreshToken) setRefreshToken(refreshToken);
  unauthorizedInFlight = null;
  unauthorizedRedirecting = false;
}

export function clearToken() {
  tokenCache = '';
  wx.removeStorageSync('jwt_token');
  wx.removeStorageSync('refresh_token');
  sessionStore.setState({ token: '', user: null, isAuthenticated: false });
}

function redirectToLogin() {
  if (unauthorizedRedirecting) return;
  unauthorizedRedirecting = true;
  try {
    const app = getApp && getApp();
    if (app && typeof app.beforeLogout === 'function') {
      app.beforeLogout();
    }
  } catch (err) {
    // Ignore app lifecycle lookup failures during forced logout.
  }
  wx.showToast({
    title: '登录已失效，请重新登录',
    icon: 'none',
    duration: 2200,
  });
  setTimeout(() => {
    wx.reLaunch({
      url: '/pages/login/index',
      complete: () => {
        unauthorizedRedirecting = false;
      },
    });
  }, 80);
}

async function handleUnauthorizedOnce() {
  if (!unauthorizedInFlight) {
    unauthorizedInFlight = Promise.resolve().then(() => {
      clearToken();
      redirectToLogin();
    });
  }
  await unauthorizedInFlight;
}

export function loadToken() {
  tokenCache = wx.getStorageSync('jwt_token') || '';
  if (tokenCache) {
    sessionStore.setState({ token: tokenCache });
  }
  return tokenCache;
}

function rawRequest(options) {
  return new Promise((resolve, reject) => {
    wx.request({
      ...options,
      success: resolve,
      fail: reject,
    });
  });
}

async function request(method, endpoint, data, options = {}) {
  const headers = { ...(options.headers || {}) };
  const token = getToken();
  if (!options.skipAuth && token) {
    headers.Authorization = 'Bearer ' + token;
  }

  const response = await rawRequest({
    url: CONFIG.SERVER + endpoint,
    method,
    data,
    timeout: options.timeout || 15000,
    header: headers,
  });

  if (!response || response.statusCode < 200 || response.statusCode >= 300) {
    const payload = response && response.data;
    const detail = payload && (payload.detail || payload.message || payload.result_summary || payload.error);
    const error = new Error(detail ? String(detail) : 'request_failed');
    error.statusCode = response && response.statusCode;
    error.payload = payload;
    if (error.statusCode === 401 && !options.skipAuth) {
      if (!options.authRetried && endpoint !== '/api/auth/refresh') {
        const refreshed = await refreshAccessToken();
        if (refreshed) {
          return request(method, endpoint, data, { ...options, authRetried: true });
        }
      }
      await handleUnauthorizedOnce();
    }
    throw error;
  }
  return response.data;
}

async function refreshAccessToken() {
  if (refreshInFlight) return refreshInFlight;
  const refreshToken = wx.getStorageSync('refresh_token') || '';
  if (!refreshToken) return false;
  refreshInFlight = (async () => {
    try {
      const response = await rawRequest({
        url: CONFIG.SERVER + '/api/auth/refresh',
        method: 'POST',
        data: { refresh_token: refreshToken },
        timeout: 15000,
        header: { 'Content-Type': 'application/json' },
      });
      if (!response || response.statusCode < 200 || response.statusCode >= 300) return false;
      const payload = response.data || {};
      const accessToken = payload.access_token || payload.token || '';
      if (!accessToken || !payload.refresh_token) return false;
      setAuthTokens(accessToken, payload.refresh_token);
      return true;
    } catch (err) {
      return false;
    } finally {
      refreshInFlight = null;
    }
  })();
  return refreshInFlight;
}

export async function get(endpoint, options = {}) {
  return request('GET', endpoint, undefined, options);
}

export async function post(endpoint, data = {}, options = {}) {
  return request('POST', endpoint, data, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...(options.headers || {}),
    },
  });
}
