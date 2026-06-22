import CONFIG from './config';
import api from './api';

let socketTask = null;
let reconnectTimer = null;
let isConnected = false;
let onMessageCallback = null;

function connect() {
  if (socketTask) return;

  const token = wx.getStorageSync('jwt_token') || '';
  if (!token) {
    console.log('[WS] 无 token，跳过连接');
    return;
  }

  const url = CONFIG.WS_URL + '?token=' + encodeURIComponent(token);
  console.log('[WS] connecting...');

  socketTask = wx.connectSocket({ url, timeout: 5000 });

  socketTask.onOpen(() => {
    console.log('[WS] connected');
    isConnected = true;
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
  });

  socketTask.onMessage((res) => {
    try {
      const data = JSON.parse(res.data);
      if (onMessageCallback) onMessageCallback(data);
    } catch (e) {
      console.warn('[WS] parse error:', e);
    }
  });

  socketTask.onClose(() => {
    console.log('[WS] closed');
    isConnected = false;
    socketTask = null;
    // 只有有 token 才重连（避免登录失败时的空循环）
    const stored = wx.getStorageSync('jwt_token') || '';
    if (stored) {
      reconnectTimer = setTimeout(() => {
        console.log('[WS] reconnecting...');
        connect();
      }, 5000);
    }
  });

  socketTask.onError((err) => {
    console.warn('[WS] error (falling back to HTTP polling):', err.errMsg || err);
    socketTask = null;
    isConnected = false;
    // WS 失败不阻塞，HTTP 轮询兜底
    // 如果是认证失败 (token 过期)，标记需要重新登录
    const errMsg = (err && err.errMsg) || '';
    if (errMsg.includes('401') || errMsg.includes('status') || errMsg.includes('Invalid')) {
      console.warn('[WS] token may be expired, will retry on next login');
    }
  });
}

function disconnect() {
  if (socketTask) {
    socketTask.close();
    socketTask = null;
  }
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  isConnected = false;
}

function onMessage(callback) {
  onMessageCallback = callback;
}

function getConnectionStatus() {
  return isConnected;
}

export default { connect, disconnect, onMessage, getConnectionStatus };
