import CONFIG from '../utils/config';
import sessionStore from '../stores/session';

let tokenCache = '';

function getToken() {
  return tokenCache || wx.getStorageSync('jwt_token') || '';
}

export function setToken(token) {
  tokenCache = token || '';
  wx.setStorageSync('jwt_token', tokenCache);
  sessionStore.setState({ token: tokenCache, isAuthenticated: !!tokenCache });
}

export function clearToken() {
  tokenCache = '';
  wx.removeStorageSync('jwt_token');
  sessionStore.setState({ token: '', user: null, isAuthenticated: false });
}

export function loadToken() {
  tokenCache = wx.getStorageSync('jwt_token') || '';
  if (tokenCache) {
    sessionStore.setState({ token: tokenCache, isAuthenticated: true });
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
    const error = new Error('request_failed');
    error.statusCode = response && response.statusCode;
    error.payload = response && response.data;
    if (error.statusCode === 401) {
      clearToken();
    }
    throw error;
  }
  return response.data;
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
