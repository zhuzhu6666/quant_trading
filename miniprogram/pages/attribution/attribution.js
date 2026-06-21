import api from '../../utils/api';

Page({
  data: {
    nFactors: '—', totalTrades: '—',
    winRate: '—', winRateCls: 'text-gray',
    sharpe: '—', sharpeCls: 'text-gray',
    factors: [],
    weights: [],
    updated: '',
  },
  _timer: null,

  onLoad() { this._fetch(); this._timer = setInterval(() => this._fetch(), 30000); },
  onUnload() { if (this._timer) clearInterval(this._timer); },
  onShow() { this._fetch(); },

  async _fetch() {
    await Promise.all([this._fetchAttribution(), this._fetchWeights()]);
    const now = new Date();
    this.setData({ updated: `${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}:${String(now.getSeconds()).padStart(2,'0')}` });
  },

  async _fetchAttribution() {
    const d = await api.get('/api/v4/stats');
    if (!d || d.status !== 'ok') return;
    const s = d.summary || {};
    const pf = d.per_factor || {};

    const sorted = Object.entries(pf)
      .sort((a, b) => Math.abs(b[1].avg_mc) - Math.abs(a[1].avg_mc))
      .slice(0, 15);
    const wr = (s.overall_win_rate || 0) * 100;
    const sharpe = s.avg_sharpe_across_factors;

    this.setData({
      nFactors: s.n_factors_attributed || 0,
      totalTrades: s.total_trades || 0,
      winRate: wr.toFixed(1) + '%',
      winRateCls: wr > 50 ? 'text-green' : wr < 40 ? 'text-red' : 'text-orange',
      sharpe: sharpe != null ? Number(sharpe).toFixed(4) : '—',
      sharpeCls: sharpe > 0 ? 'text-green' : sharpe < 0 ? 'text-red' : 'text-gray',
      factors: sorted.map(function(entry) {
        var name = entry[0], st = entry[1];
        const mc = st.avg_mc || 0;
        const wr2 = (st.win_rate || 0) * 100;
        return {
          name: name, label: name.replace(/_/g, ' '),
          mcText: (mc >= 0 ? '+' : '') + mc.toFixed(4),
          mcCls: mc > 0 ? 'text-green' : mc < 0 ? 'text-red' : 'text-gray',
          wrText: wr2.toFixed(1) + '%',
          wrBadge: wr2 > 50 ? 'badge-green' : wr2 < 40 ? 'badge-red' : 'badge-orange',
        };
      }),
    });
  },

  async _fetchWeights() {
    const d = await api.get('/api/v4/weights');
    if (!Array.isArray(d) || !d.length) return;
    var vals = d.map(function(w) { return w.new || 0; });
    vals.push(0.01);
    var maxW = Math.max.apply(null, vals);
    this.setData({
      weights: d.slice(0, 20).map(w => ({
        factor: w.factor.replace(/_/g, ' '),
        weight: (w.new || 0).toFixed(2),
        barPct: Math.round((w.new || 0) / maxW * 100),
      })),
    });
  },
});
