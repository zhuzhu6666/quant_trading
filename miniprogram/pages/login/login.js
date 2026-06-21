import api from '../../utils/api';

Page({
  data: {
    username: '',
    password: '',
    loading: false,
    error: '',
  },

  onLoad() {
    const token = wx.getStorageSync('jwt_token');
    if (token) {
      api.loadToken();
      getApp().startChannels();
      this._goMain();
    }
  },

  onUsernameInput(e) { this.setData({ username: e.detail.value }); },
  onPasswordInput(e) { this.setData({ password: e.detail.value }); },

  async doLogin() {
    const { username, password } = this.data;
    if (!username || !password) {
      this.setData({ error: '请输入用户名和密码' });
      return;
    }
    this.setData({ loading: true, error: '' });
    try {
      const token = await api.login(username, password);
      if (token) {
        getApp().startChannels();
        this._goMain();
      } else {
        this.setData({ error: '登录失败，请检查用户名和密码' });
      }
    } catch (e) {
      this.setData({ error: '网络错误，请检查连接' });
    }
    this.setData({ loading: false });
  },

  _goMain() {
    wx.switchTab({ url: '/pages/index/index' });
  },
});
