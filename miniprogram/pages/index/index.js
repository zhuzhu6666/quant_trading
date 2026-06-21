import api from '../../utils/api';

const app = getApp();

const NODE_DEFS = [
  { key: 'data_sync',          label: '数据同步' },
  { key: 'factor_engine',      label: '因子引擎' },
  { key: 'signal_normalizer',  label: '信号归一化' },
  { key: 'portfolio_compositor', label: '组合构建' },
  { key: 'execution_gate',     label: '执行闸门' },
  { key: 'execution',          label: '实盘执行' },
  { key: 'attribution',        label: '归因引擎' },
  { key: 'adaptive_weight',    label: '自适应权重' },
  { key: 'risk',               label: '风控系统' },
];

Page({
  data: {
    connected: false,
    connLabel: '等待连接',
    pipelineRunning: false,
    pipelineLabel: '已停止',
    pipelineBadge: 'badge-gray',
    nodes: [],
    runningCount: 0,
    expanded: true,
    lastUpdate: '',
    controlMsg: '',
    controlOk: false,
    controlErr: false,
  },

  onLoad() { this._update(); },
  onShow() { this._update(); },
  onGlobalStateUpdate() { this._update(); },

  toggleExpand() { this.setData({ expanded: !this.data.expanded }); },

  async startPipeline() {
    if (this.data.pipelineRunning) return;
    this.setData({ controlMsg: '启动中...', controlOk: false, controlErr: false });
    const result = await api.post('/api/live/start');
    if (result && result.ok) {
      this.setData({ pipelineRunning: true, controlMsg: '已启动', controlOk: true, controlErr: false });
      this._broadcastRefresh();
    } else {
      this.setData({ controlMsg: (result && result.detail) || '启动失败', controlOk: false, controlErr: true });
    }
  },

  async stopPipeline() {
    if (!this.data.pipelineRunning) return;
    this.setData({ controlMsg: '停止中...', controlOk: false, controlErr: false });
    const result = await api.post('/api/live/stop');
    if (result && result.ok) {
      this.setData({ pipelineRunning: false, controlMsg: '已停止', controlOk: true, controlErr: false });
      this._broadcastRefresh();
    } else {
      this.setData({ controlMsg: (result && result.detail) || '停止失败', controlOk: false, controlErr: true });
    }
  },

  _broadcastRefresh() {
    if (app && app._poll) app._poll();
  },

  _update() {
    const g = app.globalData;
    const loop = g.closedLoop;
    const t = g.trading || {};
    const lastUpdate = g.lastUpdate || 0;
    const recent = (Date.now() - lastUpdate) < 8000;
    const hasData = !!(loop && loop.nodes && Object.keys(loop.nodes).length > 0);
    const connected = hasData || (t.source && t.source !== 'none');

    if (!connected || !recent) {
      this.setData({
        connected: false,
        connLabel: hasData && !recent ? '数据陈旧' : '等待连接',
        pipelineRunning: false,
        pipelineLabel: '未启动',
        pipelineBadge: 'badge-gray',
        nodes: NODE_DEFS.map(n => ({
          ...n, dot: 'dot-orange', statusCls: 'text-orange', statusText: '待机'
        })),
        runningCount: 0,
        lastUpdate: lastUpdate ? this._fmtTime(lastUpdate) : '',
      });
      return;
    }

    const nodes = loop.nodes || {};
    const running = loop.pipeline_active || false;

    const nodeList = NODE_DEFS.map(def => {
      const n = nodes[def.key];
      if (!n) return { label: def.label, dot: 'dot-orange', statusCls: 'text-orange', statusText: '待机' };
      return this._mapNode(def.label, n);
    });
    const runningCount = nodeList.filter(n => n.dot === 'dot-green').length;

    this.setData({
      connected: true,
      connLabel: running ? '运行中 · 实时' : '已连接 · 待机',
      pipelineRunning: running,
      pipelineLabel: running ? '运行中' : '已停止',
      pipelineBadge: running ? 'badge-green' : 'badge-gray',
      nodes: nodeList,
      runningCount,
      lastUpdate: this._fmtTime(lastUpdate),
    });
  },

  _mapNode(label, node) {
    const s = (node && node.status) || 'unknown';
    const green = ['running', 'ok', 'active', 'initialized'];
    const orange = ['no_data', 'cold_start', 'stale', 'waiting', 'inactive'];
    const red = ['stale_critical', 'circuit_breaker', 'error', 'off', 'stopped'];

    if (green.includes(s)) return { label, dot: 'dot-green', statusCls: 'text-green', statusText: '正常' };
    if (orange.includes(s)) return { label, dot: 'dot-orange', statusCls: 'text-orange', statusText: '待机' };
    if (red.includes(s)) return { label, dot: 'dot-red', statusCls: 'text-red', statusText: s === 'circuit_breaker' ? '熔断' : '异常' };
    return { label, dot: 'dot-gray', statusCls: 'text-gray', statusText: '未知' };
  },

  _fmtTime(ts) {
    const d = new Date(ts);
    const pad = n => String(n).padStart(2, '0');
    return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
  },
});
