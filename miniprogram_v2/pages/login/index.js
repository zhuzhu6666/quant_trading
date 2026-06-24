import { login } from '../../services/auth';
import sessionStore from '../../stores/session';

Page({
  data: {
    username: 'zhu',
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
    if (!this.data.username || !this.data.password) {
      this.setData({ error: '请输入用户名和密码' });
      return;
    }
    this.setData({ loading: true, error: '' });
    const ok = await login(this.data.username, this.data.password);
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
