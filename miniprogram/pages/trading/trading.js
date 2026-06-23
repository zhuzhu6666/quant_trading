import api from '../../utils/api';

const app = getApp();

// 闸门原因中文映射
var GATE_LABELS = {
  'passed': '通过',
  'cooldown_1': '冷却 1',
  'cooldown_2': '冷却 2',
  'cooldown': '冷却中',
  'signal_below_threshold': '信号不足',
  'nfp_skip': 'NFP 事件',
  'var_gate': 'VaR 超限',
  'risk_gate': '风控拦截',
  'macd_reverse': 'MACD 反向',
  'event_filter': '事件过滤',
  'gvz': 'GVZ 闸门',
};

function _isInternalFactorName(name) {
  return /^dsl_/i.test(name) || /^pca_/i.test(name);
}

Page({
  data: {
    connected: false,
    connLabel: '等待数据',
    source: '',
    // 账户 — WS 实时推送, HTTP 兜底
    equity: '—', balance: '—', pnl: '—', pnlCls: 'text-gray',
    margin: '—', marginFree: '—', leverage: '—',
    currency: '',
    contractOz: '100', pointSize: '0.01', pointValuePerContract: '1.00 USD/点',
    contractNotional: '—', slTpExample: '',
    // 持仓 — WS 实时推送 / HTTP 兜底
    positions: [],
    // 最近信号 — 来自 /api/live/strategy-status
    signals: [],
    signalReason: '',
    // 统计
    trades: 0, wins: 0, losses: 0,
    winRate: '—', winRateCls: 'text-gray', winRateBar: 0, winRateBarCls: 'progress-green',
    drawdown: '—',
    price: '—',
    // 风控
    circuitBreaker: false, consecLoss: 0,
    // 因子投票
    voteList: [], voteLong: 0, voteShort: 0, voteFlat: 0,
    compDir: '—', compDirCls: 'text-gray', compScore: '—',
    compPassed: false, compGate: '—', compGateCls: 'text-gray',
    compGateDetail: '', compGateBadge: 'badge-gray', compDecisionReason: '',
    compTactical: '—', compMacro: '—', hasVotes: false,
    // 执行链路
    execEvents: [], execStage: '—', execStageCls: 'text-gray', execReason: '',
    execAttemptCount: 0, execWireSendCount: 0, execSkipCount: 0, execSuccessCount: 0, execFailCount: 0,
  },

  _fallbackTimer: null,

  onLoad() {
    this._update();
    this._fallbackTimer = setInterval(() => this._fallbackFetch(), 60000);
  },

  onShow() {
    this._update();
    this._fetchSignals();
  },

  onHide() {
    this._clearFallback();
  },

  onUnload() {
    this._clearFallback();
  },

  _clearFallback() {
    if (this._fallbackTimer) { clearInterval(this._fallbackTimer); this._fallbackTimer = null; }
  },

  onGlobalStateUpdate() { this._update(); },

  // ── 主数据源: WS 推送 → global state → 实时更新 ──
  _update() {
    const g = app.globalData;
    const t = g.trading || {};
    const pos = t.position || {};
    const daily = t.daily || {};
    const risk = t.risk || {};

    const pnl = daily.pnl || 0;
    const trades = daily.trades || 0;
    const wins = daily.win || 0;
    const losses = daily.loss || 0;
    const wr = trades > 0 ? (wins / trades * 100) : 0;
    const dd = daily.drawdown_pct || 0;
    const connected = !!(t.source && t.source !== 'none');

    let connLabel = '等待数据';
    if (t.source === 'live') connLabel = 'cTrader 实盘 · 实时';
    else if (t.source === 'frozen') connLabel = '数据冻结 · 已停止';
    else if (t.source === 'none') connLabel = '等待连接';

    // 账户 — 来自 WS (global state)
    const eq = t.equity || 0;
    const bal = t.balance || 0;
    const currentPrice = Number(t.current_price || 0);

    // 持仓 — 来自 WS (单笔) 或 HTTP 兜底 (多笔)
    const hasPos = t.n_positions > 0;

    // 信号数据来自 strategyStatus 缓存
    var ss = g.strategyStatus;
    var signalsRaw = (ss && ss.recent_signals) || [];
    var signals = [];
    for (var si = 0; si < signalsRaw.length && si < 6; si++) {
      var s = signalsRaw[si];
      var gate = s.gate_reason || '';
      var gateLabel = GATE_LABELS[gate] || gate || '—';
      var isPassed = gate === 'passed';
      signals.push({
        dir: (s.direction || '').toUpperCase(),
        dirCls: s.direction === 'LONG' ? 'text-green' : s.direction === 'SHORT' ? 'text-red' : 'text-gray',
        score: s.score != null ? Number(s.score).toFixed(3) : '—',
        tactical: s.tactical_score != null ? Number(s.tactical_score).toFixed(3) : '—',
        nFactors: s.n_active_factors || 0,
        gate: gateLabel,
        gateCls: isPassed ? 'text-green' : 'text-orange',
        gateBadge: isPassed ? 'badge-green' : 'badge-orange',
      });
    }

    // ── 因子投票面板 ──
    var fv = (ss && ss.factor_votes) || {};
    var comp = (ss && ss.last_composite) || {};
    var voteList = [];
    var fvNames = Object.keys(fv).filter(function(name) {
      return !_isInternalFactorName(name);
    });
    // 按 |signal| 降序排列
    fvNames.sort(function(a, b) {
      return Math.abs((fv[b].signal || 0)) - Math.abs((fv[a].signal || 0));
    });
    var longCount = 0, shortCount = 0, flatCount = 0;
    for (var vi = 0; vi < fvNames.length; vi++) {
      var v = fv[fvNames[vi]];
      var dir = v.direction;
      if (dir > 0) longCount++;
      else if (dir < 0) shortCount++;
      else flatCount++;
      voteList.push({
        name: fvNames[vi].replace(/_/g, ' '),
        signal: v.signal != null ? (v.signal >= 0 ? '+' : '') + v.signal.toFixed(3) : '—',
        signalCls: dir > 0 ? 'text-green' : dir < 0 ? 'text-red' : 'text-gray',
        barPct: Math.min(Math.abs(v.signal || 0) * 100, 100),
        barCls: dir > 0 ? 'progress-green' : dir < 0 ? 'progress-red' : 'progress-gray',
      });
    }
    var compDir = comp.direction === 1 ? 'LONG' : comp.direction === -1 ? 'SHORT' : 'FLAT';
    var compDirCls = comp.direction === 1 ? 'text-green' : comp.direction === -1 ? 'text-red' : 'text-gray';
    var compPassed = !!comp.gate_passed;
    var gateReason = comp.gate_reason || '—';

    var execSummary = (ss && ss.execution_summary) || {};
    var execEventsRaw = (ss && ss.execution_events) || [];
    var execEvents = [];
    for (var ei = Math.max(0, execEventsRaw.length - 5); ei < execEventsRaw.length; ei++) {
      var ev = execEventsRaw[ei];
      var stageLabel = ev.stage === 'success' ? '已成交' : ev.stage === 'ctrader_reject' ? 'cTrader 拒单' : ev.stage === 'local_skip' ? '本地拦截' : ev.stage === 'attempt' ? '准备下单' : ev.stage || '—';
      execEvents.push({
        tick: ev.tick,
        direction: (ev.direction || '').toUpperCase(),
        stage: stageLabel,
        stageCls: ev.stage === 'success' ? 'text-green' : ev.stage === 'ctrader_reject' ? 'text-red' : ev.stage === 'local_skip' ? 'text-orange' : 'text-gray',
        reason: ev.reason || '—',
      });
    }
    var execStage = '等待执行';
    var execStageCls = 'text-gray';
    var execReason = '暂无执行记录';
    if (execSummary.last_stage === 'success') {
      execStage = '已成交';
      execStageCls = 'text-green';
      execReason = execSummary.last_reason || 'cTrader 已成交';
    } else if (execSummary.last_stage === 'ctrader_reject') {
      execStage = 'cTrader 拒单';
      execStageCls = 'text-red';
      execReason = execSummary.last_reason || 'cTrader 拒绝下单';
    } else if (execSummary.last_stage === 'local_skip') {
      execStage = '本地拦截';
      execStageCls = 'text-orange';
      execReason = execSummary.last_reason || '本地风控拦截';
    } else if (execSummary.last_stage === 'attempt') {
      execStage = '尝试下单';
      execStageCls = 'text-blue';
      execReason = execSummary.last_reason || '已准备下单';
    }

    // 闸门详细说明
    var GATE_DETAIL = {
      'passed': '所有检查通过，信号强度足够',
      'signal_below_threshold': '信号得分低于开仓阈值',
      'nfp_skip': '非农数据事件，跳过本周期',
      'gvz_gate': 'GVZ 波动率闸门触发',
      'macd_reverse': 'MACD 方向与信号相反',
    };
    var gateDetail = '';
    if (gateReason === 'passed') gateDetail = '所有检查通过，信号强度足够';
    else if (gateReason.startsWith('cooldown')) gateDetail = '冷却期未结束，还需等待';
    else gateDetail = GATE_DETAIL[gateReason] || ('拦截原因: ' + gateReason);
    var gateLabel = gateReason === 'passed' ? '放行' : '拦截';
    var gateBadge = gateReason === 'passed' ? 'badge-green' : gateReason.startsWith('cooldown') ? 'badge-orange' : 'badge-red';

    var contractOz = 100;
    var pointSize = 0.01;
    var pointValuePerContract = 1.0;
    var contractNotional = currentPrice > 0 ? (currentPrice * contractOz) : 0;
    var slTpExample = '';
    if (currentPrice > 0) {
      slTpExample = '100oz 标准合约，1点=' + pointSize.toFixed(2) + '，1点/标准合约≈' + pointValuePerContract.toFixed(2) + ' USD';
    }
    // 决策原因
    var decisionReason = '';
    if (compPassed) {
      decisionReason = '闸门放行，已尝试发送 ' + compDir + ' 订单';
    } else if (gateReason === 'signal_below_threshold') {
      decisionReason = '信号强度不足，等待更强信号';
    } else if (gateReason.startsWith('cooldown')) {
      decisionReason = '冷却期限制，避免频繁交易';
    } else {
      decisionReason = '被闸门拦截: ' + gateReason;
    }

    this.setData({
      connected, connLabel, source: t.source || 'none',
      // 账户 (WS 实时)
      equity: eq > 0 ? Number(eq).toFixed(2) : '—',
      balance: bal > 0 ? Number(bal).toFixed(2) : '—',
      pnl: (pnl >= 0 ? '+' : '') + pnl.toFixed(2),
      pnlCls: pnl > 0 ? 'text-green' : pnl < 0 ? 'text-red' : 'text-gray',
      contractOz: contractOz,
      pointSize: pointSize.toFixed(2),
      pointValuePerContract: pointValuePerContract.toFixed(2) + ' USD',
      contractNotional: contractNotional > 0 ? contractNotional.toFixed(2) + ' USD' : '—',
      slTpExample: slTpExample,
      // 统计
      trades, wins, losses,
      winRate: trades > 0 ? wr.toFixed(1) + '%' : '—',
      winRateCls: trades > 0 ? (wr >= 50 ? 'text-green' : 'text-red') : 'text-gray',
      winRateBar: wr,
      winRateBarCls: wr >= 50 ? 'progress-green' : 'progress-red',
      drawdown: dd > 0 ? dd.toFixed(2) + '%' : '0%',
      price: t.current_price ? Number(t.current_price).toFixed(2) : '—',
      // 风控
      circuitBreaker: !!risk.circuit_breaker,
      consecLoss: risk.consecutive_loss || 0,
      // 信号
      signals: signals,
      signalReason: (ss && ss.reason) || '',
      // 因子投票
      voteList: voteList,
      voteLong: longCount,
      voteShort: shortCount,
      voteFlat: flatCount,
      compDir: compDir,
      compDirCls: compDirCls,
      compScore: comp.score != null ? comp.score.toFixed(4) : '—',
      compPassed: compPassed,
      compGate: gateLabel,
      compGateCls: compPassed ? 'text-green' : gateReason.startsWith('cooldown') ? 'text-orange' : 'text-red',
      compGateDetail: gateDetail,
      compGateBadge: gateBadge,
      compDecisionReason: decisionReason,
      compTactical: comp.tactical_score != null ? comp.tactical_score.toFixed(3) : '—',
      compMacro: comp.macro_score != null ? comp.macro_score.toFixed(3) : '—',
      // 执行链路
      execEvents: execEvents,
      execStage: execStage,
      execStageCls: execStageCls,
      execReason: execReason,
      execAttemptCount: execSummary.attempts || 0,
      execWireSendCount: execSummary.wire_sends || 0,
      execSkipCount: execSummary.local_skips || 0,
      execSuccessCount: execSummary.successes || 0,
      execFailCount: execSummary.failures || 0,
      hasVotes: fvNames.length > 0,
    });

    // 如果 WS 没数据, 触发 HTTP 兜底
    if (!eq && !bal) {
      this._fallbackFetch();
    }
  },

  // ── 拉取策略状态（含最近信号）─
  async _fetchSignals() {
    try {
      var strat = await api.get('/api/live/strategy-status');
      if (strat) {
        app.globalData.strategyStatus = strat;
        this._update();
      }
    } catch (e) { /* silent */ }
  },

  // ── HTTP 兜底: WS 断开时从 /api/live/account + positions 补数据 ──
  async _fallbackFetch() {
    try {
      const [acct, pos] = await Promise.all([
        api.get('/api/live/account'),
        api.get('/api/live/positions'),
      ]);

      const hasAcct = acct && acct.ok;

      this.setData({
        equity: (hasAcct && acct.equity && (this.data.equity === '—' || !this.data.equity))
          ? Number(acct.equity).toFixed(2) : this.data.equity,
        balance: (hasAcct && acct.balance && (this.data.balance === '—' || !this.data.balance))
          ? Number(acct.balance).toFixed(2) : this.data.balance,
        margin: hasAcct && acct.margin ? Number(acct.margin).toFixed(2) : '—',
        marginFree: hasAcct && acct.margin_free ? Number(acct.margin_free).toFixed(2) : '—',
        leverage: hasAcct && acct.leverage ? acct.leverage : '—',
        currency: hasAcct && acct.currency ? acct.currency : '',
      });

      var plist = (pos && Array.isArray(pos.positions)) ? pos.positions : [];
      if (plist.length > 0) {
        var list = [];
        for (var i = 0; i < plist.length; i++) {
          var p = plist[i];
          var dir = (p.type === 'buy' || p.direction === 'LONG' || p.tradeSide === 'BUY') ? 'LONG' : 'SHORT';
          var entry = p.price_open || p.openPrice || p.entry_price || 0;
          var size = p.volume || p.size || 0;
          // v5: 服务器现在用 "pnl" 字段
          var upl = p.pnl || p.profit || p.unrealizedPnl || 0;
          var sym = p.symbol || p.symbolName || '';
          list.push({
            dir: dir === 'LONG' ? '多头' : '空头',
            dirCls: dir === 'LONG' ? 'text-green' : 'text-red',
            entry: entry ? Number(entry).toFixed(2) : '—',
            size: size ? Number(size).toFixed(2) : '—',
            apiSize: size ? String(Number(size).toFixed(2)) : '—',
            pnl: (upl >= 0 ? '+' : '') + Number(upl).toFixed(2),
            pnlCls: upl > 0 ? 'text-green' : upl < 0 ? 'text-red' : 'text-gray',
            symbol: sym,
          });
        }
        this.setData({ positions: list });
      }
    } catch (e) {
      // 静默失败, WS 主通道下次会更新
    }
  },
});
