import api from '../../utils/api';

Page({
  data: {
    nFactors: '—', totalTrades: '—',
    winRate: '—', winRateCls: 'text-gray',
    sharpe: '—', sharpeCls: 'text-gray',
    factors: [],
    weights: [],
    updated: '',
    // ── 真实 PnL 总览 ──
    totalGross: '—', totalSwap: '—', totalComm: '—', totalNet: '—',
    totalNetCls: 'text-gray',
  },
  _timer: null,

  onLoad() { this._fetch(); this._timer = setInterval(() => this._fetch(), 30000); },
  onUnload() { if (this._timer) { clearInterval(this._timer); this._timer = null; } },
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

    var pf = d.per_factor || {};
    var sm = d.summary || {};
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

    // 按 |avg_net| 排序 (真实盈亏优先)
    var sorted = names.map(function(name) {
      return { name: name, data: pf[name] };
    }).sort(function(a, b) {
      // 有真实盈亏按 |avg_net|, 否则按 |avg_mc|
      var ma = Math.abs(a.data.avg_net) || Math.abs(a.data.avg_mc) || 0;
      var mb = Math.abs(b.data.avg_net) || Math.abs(b.data.avg_mc) || 0;
      return mb - ma;
    }).slice(0, 15);

    // 真实 PnL 汇总
    var tg = sm.total_gross || 0;
    var ts = sm.total_swap || 0;
    var tc = sm.total_commission || 0;
    var tn = sm.total_net_pnl || 0;

    this.setData({
      nFactors: nAttributed,
      totalTrades: totalTrades,
      winRate: wr.toFixed(1) + '%',
      winRateCls: wr > 50 ? 'text-green' : wr < 40 ? 'text-red' : 'text-orange',
      sharpe: avgSharpe.toFixed(4),
      sharpeCls: avgSharpe > 0 ? 'text-green' : avgSharpe < 0 ? 'text-red' : 'text-gray',
      totalGross: tg.toFixed(2),
      totalSwap: ts.toFixed(2),
      totalComm: tc.toFixed(2),
      totalNet: tn.toFixed(2),
      totalNetCls: tn > 0 ? 'text-green' : tn < 0 ? 'text-red' : 'text-gray',
      factors: sorted.map(function(entry) {
        var st = entry.data;
        var mc = st.avg_mc || 0;
        var wr2 = (st.win_rate || 0) * 100;
        // 显示真实 avg_net (如有) 否则 fallback avg_mc
        var displayMc = (st.avg_net != null && st.avg_net !== 0) ? st.avg_net : mc;
        return {
          name: entry.name,
          label: entry.name.replace(/_/g, ' '),
          mcText: (displayMc >= 0 ? '+' : '') + displayMc.toFixed(4),
          mcCls: displayMc > 0 ? 'text-green' : displayMc < 0 ? 'text-red' : 'text-gray',
          wrText: wr2.toFixed(1) + '%',
          wrBadge: wr2 > 50 ? 'badge-green' : wr2 < 40 ? 'badge-red' : 'badge-orange',
          nTrades: st.n_trades || 0,
          irShort: st.ir_short != null ? st.ir_short.toFixed(4) : '—',
          // 真实 PnL
          avgGross: st.avg_gross != null ? (st.avg_gross >= 0 ? '+' : '') + Number(st.avg_gross).toFixed(4) : '—',
          avgNet: st.avg_net != null ? (st.avg_net >= 0 ? '+' : '') + Number(st.avg_net).toFixed(4) : '—',
          totalGross: st.total_gross != null ? Number(st.total_gross).toFixed(2) : '—',
          totalNet: st.total_net_pnl != null ? Number(st.total_net_pnl).toFixed(2) : '—',
          totalNetCls: (st.total_net_pnl || 0) > 0 ? 'text-green' : (st.total_net_pnl || 0) < 0 ? 'text-red' : 'text-gray',
          avgGrossCls: (st.avg_gross || 0) > 0 ? 'text-green' : (st.avg_gross || 0) < 0 ? 'text-red' : 'text-gray',
          avgNetCls: (st.avg_net || 0) > 0 ? 'text-green' : (st.avg_net || 0) < 0 ? 'text-red' : 'text-gray',
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
