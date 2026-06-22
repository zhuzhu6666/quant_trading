import CONFIG from './config';

let token = '';

function setToken(t) {
  token = t;
  wx.setStorageSync('jwt_token', t);
}

function loadToken() {
  const t = wx.getStorageSync('jwt_token');
  if (t) token = t;
  return token;
}

// 用回调形式包装 wx.request，兼容各种网络状况
function request(options) {
  return new Promise((resolve, reject) => {
    wx.request({
      ...options,
      success: (res) => resolve(res),
      fail: (err) => reject(err),
      complete: () => {},
    });
  });
}

async function login(username, password) {
  const u = username || 'zhu';
  const p = password || '1994';
  try {
    console.log('[API] 登录 ' + CONFIG.SERVER + '/api/auth/login');
    const res = await request({
      url: CONFIG.SERVER + '/api/auth/login',
      method: 'POST',
      data: { username: u, password: p },
      timeout: 30000, // 增大到 30s
    });

    // 安全地检查响应
    if (!res) {
      console.error('[API] 登录: 返回为空');
      return null;
    }
    console.log('[API] 登录状态:', res.statusCode, typeof res.data);

    if (res.statusCode === 200 && res.data) {
      const t = res.data.token || res.data.access_token;
      if (t) {
        setToken(t);
        console.log('[API] 登录成功');
        return t;
      }
    }
    console.error('[API] 登录失败:', res.statusCode, JSON.stringify(res.data).slice(0, 200));
    return null;
  } catch (e) {
    console.error('[API] 登录异常:', e.errMsg || e.message || e);
    return null;
  }
}

async function get(endpoint) {
  await _ensureToken();
  try {
    const headers = {};
    if (token) headers.Authorization = 'Bearer ' + token;
    const res = await request({
      url: CONFIG.SERVER + endpoint,
      header: headers,
      timeout: 15000,
    });
    return res && res.data;
  } catch (e) {
    console.error('[API] GET', endpoint, (e.errMsg || e.message || '').slice(0, 100));
    return null;
  }
}

async function post(endpoint, data = {}) {
  await _ensureToken();
  try {
    const headers = { 'Content-Type': 'application/json' };
    if (token) headers.Authorization = 'Bearer ' + token;
    const res = await request({
      url: CONFIG.SERVER + endpoint,
      method: 'POST',
      header: headers,
      data,
      timeout: 20000,
    });
    return res && res.data;
  } catch (e) {
    console.error('[API] POST', endpoint, (e.errMsg || e.message || '').slice(0, 100));
    return null;
  }
}

async function _ensureToken() {
  if (token) return;
  const saved = loadToken();
  if (saved) { token = saved; return; }
  await login();
  // login() 内部调用 setToken 已设置 token, 二次确认
  if (!token) {
    const reloaded = loadToken();
    if (reloaded) token = reloaded;
  }
}

export default { login, get, post, setToken, loadToken };
