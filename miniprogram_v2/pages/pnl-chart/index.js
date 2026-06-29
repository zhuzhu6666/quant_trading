import liveStore from '../../stores/live';
import { refreshLiveSnapshot } from '../../services/live';
import { formatMoney, formatPct, formatTime, toneFromPnl } from '../../utils/format';

const UChartsModule = require('../../vendor/ucharts/u-charts.min');
const UCharts = UChartsModule.default || UChartsModule;

const INITIAL_CAPITAL = 500;
const CHART_ID = 'pnlChart';

function getCanvasScale() {
  if (wx.getWindowInfo) {
    const info = wx.getWindowInfo();
    return Number(info.pixelRatio || 1);
  }
  return 1;
}

function formatCapital(value) {
  const n = Number(value || 0);
  return n.toFixed(2);
}

function makePointsSignature(points = []) {
  if (!points.length) return 'empty';
  const first = points[0];
  const latest = points[points.length - 1];
  return [
    points.length,
    first.ts,
    first.cumulative,
    latest.ts,
    latest.cumulative,
  ].join('|');
}

function makeSummarySignature(summary = {}, points = []) {
  return [
    Number(summary.realized_pnl || 0),
    Number(summary.trades || points.length || 0),
    Number(summary.win_rate || 0),
  ].join('|');
}

function normalizePoints(series = {}) {
  const rawPoints = Array.isArray(series.points) ? series.points : [];
  return rawPoints
    .map((item, index) => {
      const cumulative = Number(item.cumulative || 0);
      const pnl = Number(item.pnl || 0);
      return {
        id: `trade-${Number(item.ts || 0)}-${index}`,
        ts: Number(item.ts || 0),
        pnl,
        cumulative,
        equity: INITIAL_CAPITAL + cumulative,
        timeText: formatTime(item.ts),
        pnlText: formatMoney(pnl),
        cumulativeText: formatMoney(cumulative),
        equityText: formatCapital(INITIAL_CAPITAL + cumulative),
        toneClass: toneFromPnl(pnl) === 'positive' ? 'accent-pos' : toneFromPnl(pnl) === 'negative' ? 'accent-neg' : '',
      };
    })
    .filter((item) => Number.isFinite(item.cumulative))
    .sort((a, b) => a.ts - b.ts);
}

function buildRange(points, rangeKey) {
  if (!points.length) return [];
  const first = points[0];
  const basePoint = {
    id: 'initial-capital',
    ts: first.ts ? first.ts - 1 : 0,
    pnl: 0,
    cumulative: 0,
    equity: INITIAL_CAPITAL,
    timeText: '起点',
    pnlText: formatMoney(0),
    cumulativeText: formatMoney(0),
    equityText: formatCapital(INITIAL_CAPITAL),
    toneClass: '',
  };
  if (rangeKey === 'all') return [basePoint].concat(points);
  const size = Number(rangeKey || 80);
  const startIndex = Math.max(points.length - size, 0);
  const anchor = startIndex > 0 ? points[startIndex - 1] : basePoint;
  return [anchor].concat(points.slice(startIndex));
}

Page({
  data: {
    loading: false,
    empty: true,
    equityText: formatCapital(INITIAL_CAPITAL),
    profitText: formatMoney(0),
    profitToneClass: '',
    tradeCountText: '0',
    winRateText: '0.00%',
    chartHint: '等待历史平仓数据',
    selectedText: '等待历史平仓数据',
    rangeKey: '80',
    recentRows: [],
  },

  onLoad() {
    this._points = [];
    this._chart = null;
    this._viewSignature = '';
    this._chartSignature = '';
    this._unsub = liveStore.subscribe(() => this.syncFromStore());
    this.syncFromStore();
    this.onRefresh();
  },

  onReady() {
    this.renderChart();
  },

  onUnload() {
    if (this._unsub) this._unsub();
  },

  async onRefresh() {
    if (this.data.loading) return;
    this.setData({ loading: true });
    try {
      await refreshLiveSnapshot({ force: true });
      this.syncFromStore();
    } finally {
      this.setData({ loading: false });
    }
  },

  syncFromStore() {
    const series = liveStore.getState().realizedPnlSeries || {};
    const summary = series.summary || {};
    const points = normalizePoints(series);
    this._points = points;
    const latest = points[points.length - 1] || null;
    const nextViewSignature = [
      this.data.rangeKey,
      makePointsSignature(points),
      makeSummarySignature(summary, points),
    ].join('::');
    if (nextViewSignature === this._viewSignature) return;
    this._viewSignature = nextViewSignature;
    const realized = Number(summary.realized_pnl ?? (latest ? latest.cumulative : 0));
    const winRate = Number(summary.win_rate || 0);
    this.setData({
      empty: !points.length,
      equityText: formatCapital(INITIAL_CAPITAL + realized),
      profitText: formatMoney(realized),
      profitToneClass: toneFromPnl(realized) === 'positive' ? 'accent-pos' : toneFromPnl(realized) === 'negative' ? 'accent-neg' : '',
      tradeCountText: String(summary.trades || points.length),
      winRateText: formatPct(winRate * 100),
      chartHint: points.length
        ? `当前显示 ${this.data.rangeKey === 'all' ? '全量' : `最近 ${this.data.rangeKey} 笔`} · 初始资金 ${formatCapital(INITIAL_CAPITAL)}`
        : '暂无历史平仓记录',
      selectedText: latest
        ? `${latest.timeText} · 单笔 ${latest.pnlText} · 累计 ${latest.cumulativeText} · 权益 ${latest.equityText}`
        : '等待历史平仓数据',
      recentRows: points.slice(-8).reverse(),
    }, () => {
      this.renderChart();
    });
  },

  onRangeTap(event) {
    const rangeKey = String((event.currentTarget && event.currentTarget.dataset && event.currentTarget.dataset.range) || '80');
    if (rangeKey === this.data.rangeKey) return;
    this._viewSignature = '';
    this._chartSignature = '';
    this.setData({
      rangeKey,
      chartHint: `当前显示 ${rangeKey === 'all' ? '全量' : `最近 ${rangeKey} 笔`} · 初始资金 ${formatCapital(INITIAL_CAPITAL)}`,
    }, () => this.renderChart());
  },

  renderChart() {
    const points = this._points || [];
    if (!points.length) return;
    const renderSeq = (this._chartRenderSeq || 0) + 1;
    this._chartRenderSeq = renderSeq;
    wx.createSelectorQuery()
      .in(this)
      .select(`#${CHART_ID}`)
      .fields({ node: true, size: true }, (res) => {
        if (renderSeq !== this._chartRenderSeq) return;
        if (!(res && res.node && res.width && res.height)) return;
        const canvas = res.node;
        const context = canvas.getContext('2d');
        const width = res.width;
        const height = res.height;
        const canvasScale = getCanvasScale();
        canvas.width = Math.round(width * canvasScale);
        canvas.height = Math.round(height * canvasScale);
        context.scale(canvasScale, canvasScale);

        const visiblePoints = buildRange(points, this.data.rangeKey);
        const categories = visiblePoints.map((item, index) => {
          if (index === 0 || index === visiblePoints.length - 1) return item.timeText;
          return '';
        });
        const data = visiblePoints.map((item) => Number(item.equity.toFixed(2)));
        const values = data.concat([INITIAL_CAPITAL]);
        const minValue = Math.min(...values);
        const maxValue = Math.max(...values);
        const span = maxValue - minValue || 1;
        const yMin = minValue - span * 0.12;
        const yMax = maxValue + span * 0.12;
        const color = data[data.length - 1] >= INITIAL_CAPITAL ? '#16a34a' : '#dc2626';
        this._visiblePoints = visiblePoints;
        const scrollEnabled = this.data.rangeKey === 'all' && visiblePoints.length > 80;
        const dataChecksum = data.reduce((sum, value, index) => sum + Math.round(value * 100) * (index + 1), 0);
        const nextChartSignature = [
          this.data.rangeKey,
          width,
          height,
          scrollEnabled ? 'scroll' : 'fixed',
          data.length,
          data[0],
          data[data.length - 1],
          minValue,
          maxValue,
          dataChecksum,
        ].join('|');
        if (nextChartSignature === this._chartSignature) return;
        this._chartSignature = nextChartSignature;
        this._chart = new UCharts({
          type: 'line',
          context,
          canvas2d: true,
          width,
          height,
          pixelRatio: 1,
          fontSize: 10,
          categories,
          series: [{
            name: '系统权益',
            data,
            color,
          }],
          animation: false,
          background: '#ffffff',
          padding: [8, 8, 10, 4],
          enableScroll: scrollEnabled,
          dataLabel: false,
          dataPointShape: false,
          legend: { show: false },
          xAxis: {
            disableGrid: true,
            itemCount: scrollEnabled ? 80 : Math.max(visiblePoints.length, 1),
            scrollShow: false,
            fontColor: '#8a95a3',
            fontSize: 10,
            lineHeight: 14,
            marginTop: 4,
            axisLine: false,
          },
          yAxis: {
            gridType: 'dash',
            dashLength: 4,
            splitNumber: 4,
            gridColor: '#e2e8f0',
            padding: 4,
            data: [{
              min: Number(yMin.toFixed(2)),
              max: Number(yMax.toFixed(2)),
              axisLine: false,
              fontColor: '#8a95a3',
              format: (val) => Number(val).toFixed(0),
            }],
          },
          extra: {
            line: {
              type: 'straight',
              width: 2,
              activeType: 'hollow',
            },
            tooltip: {
              showBox: true,
            },
          },
        });
      })
      .exec();
  },

  onChartTouchStart(event) {
    if (this._chart) this._chart.scrollStart(event);
  },

  onChartTouchMove(event) {
    if (!this._chart) return;
    const touches = event.touches || event.changedTouches || [];
    if (touches.length > 1 && this._chart.dobuleZoom) {
      this._chart.dobuleZoom(event);
      return;
    }
    this._chart.scroll(event);
  },

  onChartTouchEnd(event) {
    if (!this._chart) return;
    this._chart.scrollEnd(event);
    this._chart.showToolTip(event, {
      formatter: (item, category, index) => {
        const point = (this._visiblePoints || [])[index];
        if (!point) return `${item.name}: ${item.data}`;
        this.setData({
          selectedText: `${point.timeText} · 单笔 ${point.pnlText} · 累计 ${point.cumulativeText} · 权益 ${point.equityText}`,
        });
        return `权益 ${point.equityText}`;
      },
    });
  },
});
