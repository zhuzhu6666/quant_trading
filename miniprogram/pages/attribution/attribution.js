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
    var now = new Date();
    var pad = function(n) { return String(n).padStart(2, '0'); };
    this.setData({ updated: pad(now.getHours()) + ':' + pad(now.getMinutes()) + ':' + pad(now.getSeconds()) });
  },

  async _fetchAttribution() {
    var d = await api.get('/api/v4/stats');
    if (!d || d.status !== 'ok') return;

    // v5: 不再有 d.summary，从 per_factor 聚合计算
    var pf = d.per_factor || {};
    var names = Object.keys(pf);
    if (!names.length) return;

    var totalTrades = 0;
    var totalWins = 0;
    var totalMc = 0;
    var nAttributed = 0;
    var sharpeSum = 0;
    var sharpeCount = 0;

    var factorList = [];
    names.forEach(function(name) {
      var st = pf[name];
      totalTrades += st.n_trades || 0;
      totalWins += st.wins || 0;
      totalMc += st.total_mc || 0;
      nAttributed++;
      if (st.composite_sharpe_score != null) {
        sharpeSum += st.composite_sharpe_score;
        sharpeCount++;
      }
    });

    var wr = totalTrades > 0 ? (totalWins / totalTrades * 100) : 0;
    var avgSharpe = sharpeCount > 0 ? (sharpeSum / sharpeCount) : 0;

    // 按 |avg_mc| 排序
    var sorted = names.map(function(name) {
      return { name: name, data: pf[name] };
    }).sort(function(a, b) {
      return Math.abs(b.data.avg_mc) - Math.abs(a.data.avg_mc);
    }).slice(0, 15);

    this.setData({
      nFactors: nAttributed,
      totalTrades: totalTrades,
      winRate: wr.toFixed(1) + '%',
      winRateCls: wr > 50 ? 'text-green' : wr < 40 ? 'text-red' : 'text-orange',
      sharpe: avgSharpe.toFixed(4),
      sharpeCls: avgSharpe > 0 ? 'text-green' : avgSharpe < 0 ? 'text-red' : 'text-gray',
      factors: sorted.map(function(entry) {
        var st = entry.data;
        var mc = st.avg_mc || 0;
        var wr2 = (st.win_rate || 0) * 100;
        return {
          name: entry.name,
          label: entry.name.replace(/_/g, ' '),
          mcText: (mc >= 0 ? '+' : '') + mc.toFixed(4),
          mcCls: mc > 0 ? 'text-green' : mc < 0 ? 'text-red' : 'text-gray',
          wrText: wr2.toFixed(1) + '%',
          wrBadge: wr2 > 50 ? 'badge-green' : wr2 < 40 ? 'badge-red' : 'badge-orange',
          // v5 额外字段
          nTrades: st.n_trades || 0,
          irShort: st.ir_short != null ? st.ir_short.toFixed(4) : '—',
        };
      }),
    });
  },

  async _fetchWeights() {
    var d = await api.get('/api/v4/weights');
    if (!Array.isArray(d) || !d.length) return;
    var vals = d.map(function(w) { return w.new || 0; });
    vals.push(0.01);
    var maxW = Math.max.apply(null, vals);
    this.setData({
      weights: d.slice(0, 20).map(function(w) {
        return {
          factor: w.factor.replace(/_/g, ' '),
          weight: (w.new || 0).toFixed(2),
          barPct: Math.round((w.new || 0) / maxW * 100),
        };
      }),
    });
  },
});
