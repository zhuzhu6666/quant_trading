import CONFIG from '../../utils/config';

Page({
  data: {
    chartUrl: '',
  },

  onLoad() {
    const token = wx.getStorageSync('jwt_token') || '';
    const url = `${CONFIG.SERVER}/mobile/pnl-chart/?token=${encodeURIComponent(token)}`;
    this.setData({ chartUrl: url });
  },
});
