import liveStore from '../../stores/live';
import learningStore from '../../stores/learning';
import { refreshLiveSnapshot, startTradingLoop, stopTradingLoop } from '../../services/live';
import { openLearningGovernancePage, refreshLearning, runLearningGovernance } from '../../services/learning';
import { formatMoney, formatPct, formatTime, toneFromPnl } from '../../utils/format';

const PNL_CANVAS_ID = 'realizedPnlCanvas';
const SYSTEM_INITIAL_CAPITAL = 500;
const DEFAULT_VISIBLE_TRADES = 80;
const MIN_VISIBLE_TRADES = 12;

function humanizeResponsibility(value = '') {
  const key = String(value || '').toLowerCase();
  if (key === 'exit') return '退出问题';
  if (key === 'timing') return '时长问题';
  if (key === 'regime') return '市场切换问题';
  if (key === 'parameter') return '参数问题';
  if (key === 'thesis') return 'thesis 失效';
  if (key === 'holding') return '持仓效率问题';
  return '暂未定责';
}

function formatOverviewHintText(item = null) {
  if (!item) return '';
  return `${item.title || '--'} · ${item.stage_tag || '--'} · ${item.summary || ''}`;
}

function formatCapital(value) {
  const n = Number(value || 0);
  return n.toFixed(2);
}

function uniqueRecentTrades(points = [], limit = 3) {
  const seen = new Set();
  const rows = [];
  for (let index = points.length - 1; index >= 0 && rows.length < limit; index -= 1) {
    const item = points[index];
    const key = [
      item.timeText || '',
      item.pnlText || formatMoney(item.pnl),
      item.cumulativeText || formatMoney(item.cumulative),
      item.equityText || formatCapital(item.equity),
    ].join('|');
    if (seen.has(key)) continue;
    seen.add(key);
    rows.push(item);
  }
  return rows;
}

function buildSystemNowSummary({ loopRunning, positionCount, pendingTodoCount, learningSummaryStatus }) {
  const loopText = loopRunning ? '交易循环在线' : '交易循环未运行';
  const positionText = positionCount > 0 ? `当前有 ${positionCount} 笔持仓` : '当前无持仓';
  const learningText = pendingTodoCount > 0 ? `自动治理队列 ${pendingTodoCount} 项` : '自动治理暂无待处理';
  const needsOperator = learningSummaryStatus === 'error' || !loopRunning;
  const hasGovernanceQueue = pendingTodoCount > 0;
  return {
    tone: needsOperator ? 'warning' : hasGovernanceQueue ? 'warning' : 'positive',
    sentence: `系统现在：${loopText}；${positionText}；${learningText}；接管状态：${needsOperator ? '需要运维查看' : '无需接管'}。`,
    loopText,
    positionText,
    learningText,
    humanActionText: needsOperator ? '需要运维查看' : '无需接管',
  };
}

function buildTemplateProgressNote({ candidateCounts = {}, recommendationCounts = {}, templateOpsSummary = '' }) {
  const pendingCandidates = Number(candidateCounts.pending_review || 0);
  const recommendations = Number(recommendationCounts.total || 0);
  const online = Number(recommendationCounts.online_light || 0);
  const offline = Number(recommendationCounts.offline_deep || 0);
  if (pendingCandidates || recommendations) {
    const parts = [];
    if (pendingCandidates) parts.push(`候选待治理 ${pendingCandidates}`);
    if (recommendations) parts.push(`推荐 ${recommendations}`);
    if (online || offline) parts.push(`在线 ${online} / 离线 ${offline}`);
    return parts.join('，');
  }
  const summaryText = String(templateOpsSummary || '');
  if (summaryText.includes('已批准')) return '最新候选已批准';
  if (summaryText.includes('已拒绝')) return '最新候选已拒绝';
  if (summaryText.includes('参数治理')) return '治理链已同步';
  return '暂无待处理';
}

function buildPnlCurve(series = {}) {
  const rawPoints = Array.isArray(series.points) ? series.points : [];
  const points = rawPoints
    .map((item, index) => ({
      id: `trade-${Number(item.ts || 0)}-${index}`,
      ts: Number(item.ts || 0),
      pnl: Number(item.pnl || 0),
      cumulative: Number(item.cumulative || 0),
      source: item.source || '',
      index,
    }))
    .filter((item) => Number.isFinite(item.cumulative))
    .sort((a, b) => (a.ts - b.ts) || (a.index - b.index));
  const values = points.map((item) => item.cumulative);
  const minValue = values.length ? Math.min(...values, 0) : 0;
  const maxValue = values.length ? Math.max(...values, 0) : 0;
  const chartPoints = points.map((item) => {
    return {
      ...item,
      timeText: formatTime(item.ts),
      pnlText: formatMoney(item.pnl),
      cumulativeText: formatMoney(item.cumulative),
      equity: SYSTEM_INITIAL_CAPITAL + item.cumulative,
      equityText: formatCapital(SYSTEM_INITIAL_CAPITAL + item.cumulative),
      tone: toneFromPnl(item.pnl),
    };
  });
  const plotPoints = chartPoints.length
    ? [{
        id: 'baseline',
        ts: chartPoints[0].ts,
        pnl: 0,
        cumulative: 0,
        equity: SYSTEM_INITIAL_CAPITAL,
        timeText: '起点',
        pnlText: formatMoney(0),
        cumulativeText: formatMoney(0),
        equityText: formatCapital(SYSTEM_INITIAL_CAPITAL),
        tone: 'neutral',
        baseline: true,
      }, ...chartPoints]
    : [];
  return {
    points: chartPoints,
    plotPoints,
    segments: [],
    minText: formatMoney(minValue),
    maxText: formatMoney(maxValue),
    latest: chartPoints.length ? chartPoints[chartPoints.length - 1] : null,
    recent: uniqueRecentTrades(chartPoints, 4),
  };
}

function roundedRect(ctx, x, y, width, height, radius) {
  const r = Math.min(radius, width / 2, height / 2);
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + width - r, y);
  ctx.quadraticCurveTo(x + width, y, x + width, y + r);
  ctx.lineTo(x + width, y + height - r);
  ctx.quadraticCurveTo(x + width, y + height, x + width - r, y + height);
  ctx.lineTo(x + r, y + height);
  ctx.quadraticCurveTo(x, y + height, x, y + height - r);
  ctx.lineTo(x, y + r);
  ctx.quadraticCurveTo(x, y, x + r, y);
  ctx.closePath();
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function normalizeViewport(total, start, size, previousTotal = total) {
  if (total <= 0) return { start: 0, size: DEFAULT_VISIBLE_TRADES };
  const defaultSize = Math.min(DEFAULT_VISIBLE_TRADES, total);
  const rawSize = Number(size || defaultSize);
  const nextSize = clamp(Math.round(rawSize), Math.min(MIN_VISIBLE_TRADES, total), total);
  const wasPinnedToLatest = Number(start || 0) + Number(size || defaultSize) >= previousTotal - 1;
  const preferredStart = wasPinnedToLatest ? total - nextSize : Number(start || 0);
  return {
    start: clamp(Math.round(preferredStart), 0, Math.max(total - nextSize, 0)),
    size: nextSize,
  };
}

function buildViewportLabel(total, start, size) {
  if (!total) return '0 / 0';
  const end = Math.min(start + size, total);
  return `${start + 1}-${end} / ${total}`;
}

function buildVisiblePnlPlot(points, start, size) {
  if (!points.length) return [];
  const end = Math.min(start + size, points.length);
  const visible = points.slice(start, end);
  if (!visible.length) return [];
  const anchor = start > 0
    ? {
        ...points[start - 1],
        id: `anchor-${points[start - 1].id}`,
        pnl: 0,
        pnlText: formatMoney(0),
        timeText: formatTime(points[start - 1].ts),
        baseline: true,
      }
    : {
        id: 'baseline',
        ts: visible[0].ts,
        pnl: 0,
        cumulative: 0,
        equity: SYSTEM_INITIAL_CAPITAL,
        timeText: '起点',
        pnlText: formatMoney(0),
        cumulativeText: formatMoney(0),
        equityText: formatCapital(SYSTEM_INITIAL_CAPITAL),
        tone: 'neutral',
        baseline: true,
      };
  return [anchor, ...visible];
}

Page({
  data: {
    wsLabel: '未连接',
    wsTone: 'warning',
    systemNowSentence: '系统现在：读取中',
    systemNowTone: 'neutral',
    systemNowLoopText: '交易循环未确认',
    systemNowPositionText: '当前无持仓',
    systemNowLearningText: '学习治理暂无待处理',
    systemNowNeedHumanText: '否',
    equity: '--',
    realizedPnl: '--',
    realizedPnlTone: 'neutral',
    unrealizedPnl: '--',
    unrealizedPnlTone: 'neutral',
    livePnl: '--',
    livePnlTone: 'neutral',
    realizedCurve: [],
    realizedCurvePlot: [],
    realizedCurveViewportStart: 0,
    realizedCurveViewportSize: DEFAULT_VISIBLE_TRADES,
    realizedCurveViewportLabel: '0 / 0',
    realizedCurveCanPanLeft: false,
    realizedCurveCanPanRight: false,
    realizedCurveCanZoomIn: false,
    realizedCurveCanZoomOut: false,
    realizedCurveSegments: [],
    realizedCurveEmpty: true,
    realizedCurveMin: '--',
    realizedCurveMax: '--',
    realizedCurveSummary: '今日暂无已平仓记录',
    realizedCurveLatest: '--',
    realizedCurveProfit: '--',
    realizedCurveInitialCapital: formatCapital(SYSTEM_INITIAL_CAPITAL),
    realizedCurveTrades: '0',
    realizedCurveWinRate: '0.00%',
    realizedSelectedTrade: null,
    realizedRecentTrades: [],
    drawdown: '--',
    positions: '0',
    positionSummary: '当前无持仓',
    loopLabel: '未知',
    loopTone: 'neutral',
    latestReview: null,
    learningSummary: {},
    governanceLabel: '观察中',
    governanceTone: 'neutral',
    learningApplications: 0,
    templateOpsSummary: '',
    closureSteps: [],
    progressDetailRows: [],
    loopRunning: false,
    startBusy: false,
    stopBusy: false,
    governBusy: false,
    pendingTemplateCandidateCount: 0,
    pendingTemplateRecommendationCount: 0,
    onlineLightRecommendationCount: 0,
    offlineDeepRecommendationCount: 0,
    pendingCandidateHint: '',
    pendingOnlineRecommendationHint: '',
    pendingOfflineRecommendationHint: '',
    governanceHeadlineSummary: '',
    governanceTodoCard: null,
    learningSummaryStatus: 'idle',
    learningSummaryHint: '',
    updatedAt: '--',
  },

  onLoad() {
    this._unsubs = [
      liveStore.subscribe(() => this.syncView()),
      learningStore.subscribe(() => this.syncView()),
    ];
    this.syncView();
    this.refreshAll();
  },

  onReady() {
    this.drawRealizedPnlCanvas();
  },

  onShow() {
    this.refreshAll();
  },

  onUnload() {
    (this._unsubs || []).forEach((fn) => fn && fn());
  },

  syncView() {
    const live = liveStore.getState();
    const learning = learningStore.getState();
    const trading = live.trading || {};
    const loopStatus = live.loopStatus || {};
    const summary = learning.summary || { suggestions: {}, latest_review: null };
    const suggestions = summary.suggestions || {};
    const applications = Number(summary.applications || 0);
    const candidateCounts = summary.parameter_template_candidates || {};
    const recommendationCounts = summary.parameter_template_recommendations || {};
    const proposed = Number(suggestions.proposed || 0);
    const summaryOverview = summary.parameter_template_overview || {};
    const summaryHeadline = summaryOverview.headline || {};
    const governanceLabel = String(summaryHeadline.label || '学习观察中');
    const governanceTone = String(summaryHeadline.tone || 'neutral');
    const summaryGovernanceTodo = summary.parameter_template_todo || null;
    const governanceTodoCard = summaryGovernanceTodo
      ? {
          factorId: String(summaryGovernanceTodo.factor_id || ''),
          candidateId: String(summaryGovernanceTodo.candidate_id || ''),
          recommendationId: String(summaryGovernanceTodo.recommendation_id || ''),
          title: String(summaryGovernanceTodo.title || ''),
          priorityLabel: String(summaryGovernanceTodo.priority_label || ''),
          stageTag: String(summaryGovernanceTodo.stage_tag || ''),
          targetTypeText: String(summaryGovernanceTodo.target_type || ''),
          actionLabel: String(summaryGovernanceTodo.action_label || ''),
          summary: String(summaryGovernanceTodo.priority_summary || summaryGovernanceTodo.summary || ''),
          queueHint: String(summaryGovernanceTodo.queue_hint || ''),
        }
      : null;
    const pendingCandidateHintObject = summaryOverview.pending_candidate_hint || null;
    const onlineLightHintObject = summaryOverview.online_light_hint || null;
    const offlineDeepHintObject = summaryOverview.offline_deep_hint || null;
    const templateOpsSummary = String(summary.parameter_template_ops_summary || '');
    const realizedPnl = Number(trading.realized_pnl ?? (trading.daily && trading.daily.pnl) ?? 0);
    const unrealizedPnl = Number(trading.unrealized_pnl || 0);
    const livePnl = Number(trading.live_pnl ?? unrealizedPnl);
    const pnlSeries = live.realizedPnlSeries || {};
    const pnlSeriesSummary = pnlSeries.summary || {};
    const pnlCurve = buildPnlCurve(pnlSeries);
    const realizedTrades = Number(pnlSeriesSummary.trades || 0);
    const realizedWinRate = Number(pnlSeriesSummary.win_rate || 0);
    const realizedCurveValue = Number(pnlSeriesSummary.realized_pnl);
    const displayRealizedPnl = Number.isFinite(realizedCurveValue)
      ? realizedCurveValue
      : (pnlCurve.latest ? pnlCurve.latest.cumulative : realizedPnl);
    const previousCurveTotal = Number(this._realizedCurveTotal || 0);
    const curveTotal = pnlCurve.points.length;
    const viewport = normalizeViewport(
      curveTotal,
      this.data.realizedCurveViewportStart,
      this.data.realizedCurveViewportSize,
      previousCurveTotal,
    );
    const visiblePlot = buildVisiblePnlPlot(pnlCurve.points, viewport.start, viewport.size);
    const visibleTrades = visiblePlot.filter((item) => !item.baseline);
    const selectedId = this.data.realizedSelectedTrade && this.data.realizedSelectedTrade.id;
    const selectedTrade = visibleTrades.find((item) => item.id === selectedId)
      || visibleTrades[visibleTrades.length - 1]
      || null;
    this._realizedCurveTotal = curveTotal;
    const learningSummaryStatus = String(learning.summaryStatus || 'idle');
    const learningSummaryHint = learningSummaryStatus === 'error'
      ? '学习摘要接口超时/失败，当前卡片可能显示的是空白兜底值，不代表系统没有学习数据。'
      : '';
    const templateProgressNote = buildTemplateProgressNote({
      candidateCounts,
      recommendationCounts,
      templateOpsSummary,
    });
    const templateProgressTone = Number(candidateCounts.pending_review || 0) || Number(recommendationCounts.total || 0)
      ? 'warning'
      : templateOpsSummary.includes('已批准') ? 'positive' : 'neutral';
    const progressDetailRows = templateOpsSummary
      ? [{
          id: 'template',
          title: '参数治理详情',
          text: templateOpsSummary,
        }]
      : [];
    const pendingTodoCount = Number(candidateCounts.pending_review || 0) + Number(recommendationCounts.total || 0);
    const systemNow = buildSystemNowSummary({
      loopRunning: !!loopStatus.running,
      positionCount: Number(trading.n_positions || 0),
      pendingTodoCount,
      learningSummaryStatus,
    });
    this.setData({
      systemNowSentence: systemNow.sentence,
      systemNowTone: systemNow.tone,
      systemNowLoopText: systemNow.loopText,
      systemNowPositionText: systemNow.positionText,
      systemNowLearningText: systemNow.learningText,
      systemNowNeedHumanText: systemNow.humanActionText,
      wsLabel: live.wsConnected ? '实时已连接' : '轮询兜底中',
      wsTone: live.wsConnected ? 'positive' : 'warning',
      equity: formatMoney(trading.equity),
      realizedPnl: formatMoney(displayRealizedPnl),
      realizedPnlTone: toneFromPnl(displayRealizedPnl),
      unrealizedPnl: formatMoney(unrealizedPnl),
      unrealizedPnlTone: toneFromPnl(unrealizedPnl),
      livePnl: formatMoney(livePnl),
      livePnlTone: toneFromPnl(livePnl),
      realizedCurve: pnlCurve.points,
      realizedCurvePlot: visiblePlot,
      realizedCurveViewportStart: viewport.start,
      realizedCurveViewportSize: viewport.size,
      realizedCurveViewportLabel: buildViewportLabel(curveTotal, viewport.start, viewport.size),
      realizedCurveCanPanLeft: viewport.start > 0,
      realizedCurveCanPanRight: viewport.start + viewport.size < curveTotal,
      realizedCurveCanZoomIn: viewport.size > Math.min(MIN_VISIBLE_TRADES, curveTotal || MIN_VISIBLE_TRADES),
      realizedCurveCanZoomOut: viewport.size < curveTotal,
      realizedCurveSegments: pnlCurve.segments,
      realizedCurveEmpty: !pnlCurve.points.length,
      realizedCurveMin: pnlCurve.minText,
      realizedCurveMax: pnlCurve.maxText,
      realizedCurveSummary: realizedTrades
        ? `初始资金 ${formatCapital(SYSTEM_INITIAL_CAPITAL)} · 累计盈利 ${formatMoney(displayRealizedPnl)} · 历史 ${realizedTrades} 笔平仓`
        : '暂无历史平仓记录',
      realizedCurveLatest: pnlCurve.latest ? pnlCurve.latest.equityText : formatCapital(SYSTEM_INITIAL_CAPITAL),
      realizedCurveProfit: formatMoney(displayRealizedPnl),
      realizedCurveInitialCapital: formatCapital(SYSTEM_INITIAL_CAPITAL),
      realizedCurveTrades: String(realizedTrades),
      realizedCurveWinRate: formatPct(realizedWinRate * 100),
      realizedSelectedTrade: selectedTrade,
      realizedRecentTrades: pnlCurve.recent,
      drawdown: formatPct(trading.daily && trading.daily.drawdown_pct),
      positions: String(trading.n_positions || 0),
      positionSummary: (trading.position_summary && trading.position_summary.label) || '当前无持仓',
      loopLabel: loopStatus.running ? '交易循环运行中' : '交易循环已停止',
      loopTone: loopStatus.running ? 'positive' : 'warning',
      loopRunning: !!loopStatus.running,
      latestReview: summary.latest_review,
      learningSummaryRaw: summary,
      learningSummary: suggestions,
      governanceLabel,
      governanceTone,
      governanceHeadlineSummary: String(summaryHeadline.summary || ''),
      governanceTodoCard,
      learningApplications: applications,
      templateOpsSummary,
      pendingTemplateCandidateCount: Number(candidateCounts.pending_review || 0),
      pendingTemplateRecommendationCount: Number(recommendationCounts.total || 0),
      onlineLightRecommendationCount: Number(recommendationCounts.online_light || 0),
      offlineDeepRecommendationCount: Number(recommendationCounts.offline_deep || 0),
      pendingCandidateHint: pendingCandidateHintObject
        ? formatOverviewHintText(pendingCandidateHintObject)
        : '',
      pendingOnlineRecommendationHint: onlineLightHintObject
        ? formatOverviewHintText(onlineLightHintObject)
        : '',
      pendingOfflineRecommendationHint: offlineDeepHintObject
        ? formatOverviewHintText(offlineDeepHintObject)
        : '',
      learningSummaryStatus,
      learningSummaryHint,
      closureSteps: [
        {
          id: 'signal',
          index: '1',
          title: '信号生成',
          note: loopStatus.running ? '交易循环在线' : '等待启动',
          tone: loopStatus.running ? 'positive' : 'warning',
        },
        {
          id: 'position',
          index: '2',
          title: '仓位执行',
          note: trading.n_positions ? `当前 ${trading.n_positions} 笔持仓` : '当前无持仓',
          tone: trading.n_positions ? 'positive' : 'neutral',
        },
        {
          id: 'review',
          index: '3',
          title: '平仓复盘',
          note: summary.latest_review ? summary.latest_review.outcome_label || '已生成复盘' : '等待平仓样本',
          tone: summary.latest_review ? 'positive' : 'neutral',
        },
        {
          id: 'apply',
          index: '4',
          title: '经验应用',
          note: applications ? `已应用 ${applications} 次` : proposed ? '有建议待治理' : '暂未应用',
          tone: applications ? 'positive' : proposed ? 'warning' : 'neutral',
        },
        {
          id: 'template',
          index: '5',
          title: '参数治理',
          note: templateProgressNote,
          tone: templateProgressTone,
        },
      ],
      progressDetailRows,
      updatedAt: formatTime(live.lastUpdate || learning.updatedAt),
    }, () => {
      this.drawRealizedPnlCanvas();
    });
  },

  drawRealizedPnlCanvas() {
    const plotPoints = Array.isArray(this.data.realizedCurvePlot) ? this.data.realizedCurvePlot : [];
    if (!plotPoints.length) {
      this._realizedPnlLayout = null;
      return;
    }
    wx.createSelectorQuery()
      .in(this)
      .select(`#${PNL_CANVAS_ID}`)
      .fields({ node: true, size: true })
      .exec((res) => {
        const canvasInfo = res && res[0];
        if (!(canvasInfo && canvasInfo.node && canvasInfo.width && canvasInfo.height)) return;

        const canvas = canvasInfo.node;
        const ctx = canvas.getContext('2d');
        const width = canvasInfo.width;
        const height = canvasInfo.height;
        const systemInfo = wx.getWindowInfo ? wx.getWindowInfo() : { pixelRatio: 1 };
        const dpr = systemInfo.pixelRatio || 1;
        canvas.width = width * dpr;
        canvas.height = height * dpr;
        ctx.scale(dpr, dpr);
        ctx.clearRect(0, 0, width, height);

        const values = plotPoints.map((item) => Number(item.cumulative || 0));
        const minValue = Math.min(...values, 0);
        const maxValue = Math.max(...values, 0);
        const rawSpan = maxValue - minValue;
        const span = rawSpan || Math.max(1, Math.abs(maxValue || minValue || 1));
        const pad = span * 0.12;
        const visualMin = minValue - pad;
        const visualMax = maxValue + pad;
        const visualSpan = visualMax - visualMin || 1;
        const chart = {
          left: 42,
          right: width - 14,
          top: 24,
          bottom: height - 34,
        };
        const chartWidth = Math.max(1, chart.right - chart.left);
        const chartHeight = Math.max(1, chart.bottom - chart.top);
        const toX = (index) => chart.left + (plotPoints.length <= 1 ? chartWidth / 2 : (index / (plotPoints.length - 1)) * chartWidth);
        const toY = (value) => chart.bottom - ((value - visualMin) / visualSpan) * chartHeight;
        const layoutPoints = plotPoints.map((item, index) => ({
          ...item,
          x: toX(index),
          y: toY(Number(item.cumulative || 0)),
        }));

        ctx.fillStyle = '#8a95a3';
        ctx.font = '10px sans-serif';
        ctx.textBaseline = 'middle';
        ctx.fillText(formatMoney(maxValue), 8, chart.top);
        ctx.fillText(formatMoney(minValue), 8, chart.bottom);

        ctx.strokeStyle = '#edf2f7';
        ctx.lineWidth = 1;
        [chart.top, chart.top + chartHeight / 2, chart.bottom].forEach((y) => {
          ctx.beginPath();
          ctx.moveTo(chart.left, y);
          ctx.lineTo(chart.right, y);
          ctx.stroke();
        });

        const zeroY = toY(0);
        if (zeroY >= chart.top && zeroY <= chart.bottom) {
          ctx.save();
          if (ctx.setLineDash) ctx.setLineDash([4, 5]);
          ctx.strokeStyle = '#cbd5e1';
          ctx.lineWidth = 1;
          ctx.beginPath();
          ctx.moveTo(chart.left, zeroY);
          ctx.lineTo(chart.right, zeroY);
          ctx.stroke();
          ctx.restore();
        }

        const firstPoint = layoutPoints[0];
        const latestPoint = layoutPoints[layoutPoints.length - 1];
        ctx.fillStyle = '#8a95a3';
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
        ctx.fillText(firstPoint.timeText || '--', chart.left, height - 10);
        ctx.textAlign = 'right';
        ctx.fillText(latestPoint.timeText || '--', chart.right, height - 10);

        const latestCumulative = Number(latestPoint.cumulative || 0);
        ctx.beginPath();
        if (layoutPoints.length) {
          ctx.moveTo(layoutPoints[0].x, zeroY);
          ctx.lineTo(layoutPoints[0].x, layoutPoints[0].y);
          for (let index = 1; index < layoutPoints.length; index += 1) {
            const prev = layoutPoints[index - 1];
            const point = layoutPoints[index];
            ctx.lineTo(point.x, prev.y);
            ctx.lineTo(point.x, point.y);
          }
          ctx.lineTo(latestPoint.x, zeroY);
          ctx.closePath();
          ctx.fillStyle = latestCumulative >= 0 ? 'rgba(22, 163, 74, 0.10)' : 'rgba(220, 38, 38, 0.10)';
          ctx.fill();
        }

        ctx.strokeStyle = latestCumulative >= 0 ? '#16a34a' : '#dc2626';
        ctx.lineWidth = 2.5;
        ctx.lineJoin = 'round';
        ctx.lineCap = 'round';
        ctx.beginPath();
        layoutPoints.forEach((point, index) => {
          if (index === 0) {
            ctx.moveTo(point.x, point.y);
          } else {
            const prev = layoutPoints[index - 1];
            ctx.lineTo(point.x, prev.y);
            ctx.lineTo(point.x, point.y);
          }
        });
        ctx.stroke();

        const selectedId = this.data.realizedSelectedTrade && this.data.realizedSelectedTrade.id;
        const markerEvery = layoutPoints.length <= 90 ? 1 : Math.ceil(layoutPoints.length / 60);
        layoutPoints.forEach((point, index) => {
          const positive = Number(point.pnl || 0) > 0;
          const negative = Number(point.pnl || 0) < 0;
          const selected = point.id === selectedId;
          const latest = index === layoutPoints.length - 1;
          if (!(point.baseline || selected || latest || index % markerEvery === 0)) return;
          ctx.beginPath();
          ctx.arc(point.x, point.y, selected ? 4.8 : point.baseline ? 3 : 3.8, 0, Math.PI * 2);
          ctx.fillStyle = point.baseline ? '#94a3b8' : positive ? '#16a34a' : negative ? '#dc2626' : '#007aff';
          ctx.fill();
          ctx.lineWidth = selected ? 2.5 : 2;
          ctx.strokeStyle = '#ffffff';
          ctx.stroke();
        });

        if (latestPoint && !latestPoint.baseline) {
          const badgeText = latestPoint.cumulativeText || formatMoney(latestPoint.cumulative);
          ctx.font = '11px sans-serif';
          const badgeWidth = Math.max(42, ctx.measureText(badgeText).width + 16);
          const badgeHeight = 22;
          const badgeX = clamp(latestPoint.x - badgeWidth / 2, chart.left, chart.right - badgeWidth);
          const badgeY = clamp(latestPoint.y - 34, chart.top, chart.bottom - badgeHeight);
          roundedRect(ctx, badgeX, badgeY, badgeWidth, badgeHeight, 8);
          ctx.fillStyle = '#ffffff';
          ctx.fill();
          ctx.strokeStyle = 'rgba(15, 23, 42, 0.10)';
          ctx.lineWidth = 1;
          ctx.stroke();
          ctx.fillStyle = Number(latestPoint.cumulative || 0) >= 0 ? '#047857' : '#b91c1c';
          ctx.textAlign = 'center';
          ctx.textBaseline = 'middle';
          ctx.fillText(badgeText, badgeX + badgeWidth / 2, badgeY + badgeHeight / 2 + 0.5);
        }

        this._realizedPnlLayout = { points: layoutPoints, width, height };
      });
  },

  setRealizedPnlViewport(nextStart, nextSize) {
    const points = Array.isArray(this.data.realizedCurve) ? this.data.realizedCurve : [];
    const total = points.length;
    const viewport = normalizeViewport(total, nextStart, nextSize, total);
    const visiblePlot = buildVisiblePnlPlot(points, viewport.start, viewport.size);
    const visibleTrades = visiblePlot.filter((item) => !item.baseline);
    const selectedId = this.data.realizedSelectedTrade && this.data.realizedSelectedTrade.id;
    const selectedTrade = visibleTrades.find((item) => item.id === selectedId)
      || visibleTrades[visibleTrades.length - 1]
      || null;
    this.setData({
      realizedCurvePlot: visiblePlot,
      realizedCurveViewportStart: viewport.start,
      realizedCurveViewportSize: viewport.size,
      realizedCurveViewportLabel: buildViewportLabel(total, viewport.start, viewport.size),
      realizedCurveCanPanLeft: viewport.start > 0,
      realizedCurveCanPanRight: viewport.start + viewport.size < total,
      realizedCurveCanZoomIn: viewport.size > Math.min(MIN_VISIBLE_TRADES, total || MIN_VISIBLE_TRADES),
      realizedCurveCanZoomOut: viewport.size < total,
      realizedSelectedTrade: selectedTrade,
    }, () => {
      this.drawRealizedPnlCanvas();
    });
  },

  onPnlPanPrev() {
    const size = Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES);
    const step = Math.max(1, Math.round(size * 0.75));
    this.setRealizedPnlViewport(Number(this.data.realizedCurveViewportStart || 0) - step, size);
  },

  onPnlPanNext() {
    const size = Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES);
    const step = Math.max(1, Math.round(size * 0.75));
    this.setRealizedPnlViewport(Number(this.data.realizedCurveViewportStart || 0) + step, size);
  },

  onPnlZoomIn() {
    const start = Number(this.data.realizedCurveViewportStart || 0);
    const size = Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES);
    const nextSize = Math.max(MIN_VISIBLE_TRADES, Math.round(size * 0.55));
    const center = start + size / 2;
    this.setRealizedPnlViewport(Math.round(center - nextSize / 2), nextSize);
  },

  onPnlZoomOut() {
    const points = Array.isArray(this.data.realizedCurve) ? this.data.realizedCurve : [];
    const start = Number(this.data.realizedCurveViewportStart || 0);
    const size = Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES);
    const nextSize = Math.min(points.length || size, Math.round(size * 1.8));
    const center = start + size / 2;
    this.setRealizedPnlViewport(Math.round(center - nextSize / 2), nextSize);
  },

  onPnlLatest() {
    const points = Array.isArray(this.data.realizedCurve) ? this.data.realizedCurve : [];
    const size = Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES);
    this.setRealizedPnlViewport(Math.max(points.length - size, 0), size);
  },

  onOpenPnlChart() {
    wx.navigateTo({ url: '/pages/pnl-chart/index' });
  },

  selectNearestRealizedPnlPoint(touch) {
    const layout = this._realizedPnlLayout;
    if (!(layout && touch)) return;
    const candidates = layout.points.filter((item) => !item.baseline);
    if (!candidates.length) return;
    let nearest = candidates[0];
    let nearestDistance = Number.POSITIVE_INFINITY;
    candidates.forEach((item) => {
      const dx = Number(touch.x || 0) - item.x;
      const dy = Number(touch.y || 0) - item.y;
      const distance = Math.sqrt(dx * dx + dy * dy);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearest = item;
      }
    });
    if (nearest && nearestDistance <= 48) {
      this.setData({ realizedSelectedTrade: nearest }, () => {
        this.drawRealizedPnlCanvas();
      });
    }
  },

  onRealizedPnlTouchStart(event) {
    const touch = event.touches && event.touches[0];
    if (!touch) return;
    this._realizedPnlTouch = {
      x: Number(touch.x || 0),
      y: Number(touch.y || 0),
      start: Number(this.data.realizedCurveViewportStart || 0),
      size: Number(this.data.realizedCurveViewportSize || DEFAULT_VISIBLE_TRADES),
      moved: false,
      lastStart: Number(this.data.realizedCurveViewportStart || 0),
    };
  },

  onRealizedPnlTouchMove(event) {
    const touchState = this._realizedPnlTouch;
    const touch = event.touches && event.touches[0];
    if (!(touchState && touch)) return;
    const dx = Number(touch.x || 0) - touchState.x;
    const dy = Number(touch.y || 0) - touchState.y;
    if (Math.abs(dx) < 8 || Math.abs(dx) < Math.abs(dy)) return;
    const layoutWidth = (this._realizedPnlLayout && this._realizedPnlLayout.width) || 300;
    const deltaTrades = Math.round((-dx / Math.max(layoutWidth, 1)) * touchState.size);
    if (!deltaTrades) return;
    const nextStart = touchState.start + deltaTrades;
    if (nextStart === touchState.lastStart) return;
    touchState.moved = true;
    touchState.lastStart = nextStart;
    this.setRealizedPnlViewport(nextStart, touchState.size);
  },

  onRealizedPnlTouchEnd(event) {
    const touchState = this._realizedPnlTouch;
    const touch = (event.changedTouches && event.changedTouches[0])
      || (event.touches && event.touches[0]);
    this._realizedPnlTouch = null;
    if (!(touchState && touch)) return;
    if (!touchState.moved) {
      this.selectNearestRealizedPnlPoint(touch);
    }
  },

  async refreshAll() {
    await Promise.all([refreshLiveSnapshot(), refreshLearning()]);
  },

  async onStart() {
    if (this.data.loopRunning || this.data.startBusy || this.data.stopBusy) return;
    this.setData({ startBusy: true });
    try {
      await startTradingLoop();
      await refreshLiveSnapshot({ force: true });
    } finally {
      this.setData({ startBusy: false });
    }
  },

  async onStop() {
    if (!this.data.loopRunning || this.data.stopBusy || this.data.startBusy) return;
    this.setData({ stopBusy: true });
    try {
      await stopTradingLoop();
      await refreshLiveSnapshot({ force: true });
    } finally {
      this.setData({ stopBusy: false });
    }
  },

  async onGovern() {
    if (this.data.governBusy) return;
    this.setData({ governBusy: true });
    try {
      await runLearningGovernance();
    } finally {
      this.setData({ governBusy: false });
    }
  },

  goLearning() {
    wx.switchTab({ url: '/pages/learning/index' });
  },

  goTrading() {
    wx.switchTab({ url: '/pages/trading/index' });
  },

  openLatestGovernance() {
    const summary = this.data.learningSummaryRaw || {};
    const latestCandidate = summary.latest_parameter_template_candidate || null;
    const latestCandidateTrace = summary.latest_parameter_template_candidate_trace || null;
    const latestRecommendation = summary.latest_parameter_template_recommendation || null;
    if (latestCandidate && latestCandidate.candidate_id) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId: latestCandidate.candidate_id,
        factorId: latestCandidate.factor_id || '',
      });
      return;
    }
    if (latestCandidateTrace && latestCandidateTrace.recommendation_id) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: latestCandidateTrace.recommendation_id,
      });
      return;
    }
    if (latestRecommendation && latestRecommendation.recommendation_id) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: latestRecommendation.recommendation_id,
        factorId: latestRecommendation.factor_id || '',
      });
    }
  },

  openPendingCandidate() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).pending_candidate_hint) || null;
    if (!(hint && hint.candidate_id)) return;
    openLearningGovernancePage({
      type: 'offline_candidate',
      candidateId: hint.candidate_id,
      factorId: hint.factor_id || '',
    });
  },

  openPendingRecommendation() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).online_light_hint) || null;
    if (!(hint && hint.recommendation_id)) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: hint.recommendation_id,
      factorId: hint.factor_id || '',
    });
  },

  openOfflineRecommendation() {
    const summary = this.data.learningSummaryRaw || {};
    const hint = ((summary.parameter_template_overview || {}).offline_deep_hint) || null;
    if (!(hint && hint.recommendation_id)) return;
    openLearningGovernancePage({
      type: 'template_recommendation',
      recommendationId: hint.recommendation_id,
      factorId: hint.factor_id || '',
    });
  },

  openGovernanceTodo() {
    const item = this.data.governanceTodoCard || null;
    if (!item) return;
    if (item.candidateId) {
      openLearningGovernancePage({
        type: 'offline_candidate',
        candidateId: item.candidateId,
        factorId: item.factorId,
      });
      return;
    }
    if (item.recommendationId) {
      openLearningGovernancePage({
        type: 'template_recommendation',
        recommendationId: item.recommendationId,
        factorId: item.factorId,
      });
    }
  },
});
