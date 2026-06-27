import { login } from '../../services/auth';
import sessionStore from '../../stores/session';

Page({
  data: {
    username: '',
    password: '',
    loading: false,
    error: '',
  },

  onLoad() {
    this._unsub = sessionStore.subscribe((state) => {
      if (state.isAuthenticated) {
        wx.switchTab({ url: '/pages/overview/index' });
      }
    });
    this.checkAuth();
  },

  onShow() {
    this.checkAuth();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  checkAuth() {
    const state = sessionStore.getState();
    if (state.isAuthenticated) {
      wx.switchTab({ url: '/pages/overview/index' });
    }
  },

  onUsernameInput(e) {
    this.setData({ username: e.detail.value });
  },

  onPasswordInput(e) {
    this.setData({ password: e.detail.value });
  },

  async onSubmit() {
    const username = (this.data.username || '').trim();
    const password = this.data.password || '';
    if (!username || !password) {
      this.setData({ error: '请输入用户名和密码' });
      return;
    }
    this.setData({ username, loading: true, error: '' });
    const ok = await login(username, password);
    this.setData({ loading: false });
    if (!ok) {
      this.setData({ error: '登录失败，请检查账号或网络' });
      return;
    }
    const app = getApp();
    if (app && app.afterLogin) {
      await app.afterLogin();
    }
    wx.switchTab({ url: '/pages/overview/index' });
  },
});
