import systemStore from '../../stores/system';
import { refreshOpsDomain } from '../../services/ops';
import { formatDateTime, toneFromStatus } from '../../utils/format';

Page({
  data: {
    scheduler: null,
    evolution: null,
    dbHealth: null,
    apiHealth: null,
    schedulerSummary: null,
    dbCards: [],
  },

  onLoad() {
    this._unsub = systemStore.subscribe(() => this.syncView());
    this.syncView();
    refreshOpsDomain();
  },

  onShow() {
    refreshOpsDomain();
  },

  onUnload() {
    this._unsub && this._unsub();
  },

  syncView() {
    const state = systemStore.getState();
    const apiHealth = state.apiHealth || {};
    const scheduler = state.scheduler || null;
    const jobs = (scheduler && scheduler.jobs) || [];
    const dbHealth = state.dbHealth;
    const dbCards = dbHealth
      ? Object.keys(dbHealth)
          .slice(0, 4)
          .map((key) => ({
            key,
            value: typeof dbHealth[key] === 'object' ? 'object' : String(dbHealth[key]),
          }))
      : [];
    this.setData({
      scheduler,
      evolution: state.evolution
        ? {
            ...state.evolution,
            tone: toneFromStatus(state.evolution.event_type || 'ok'),
            tsText: formatDateTime(state.evolution.ts || state.evolution.timestamp || 0),
          }
        : null,
      dbHealth,
      apiHealth: {
        ...apiHealth,
        tone: toneFromStatus(apiHealth.status || 'ok'),
      },
      schedulerSummary: {
        total: jobs.length,
        running: jobs.filter((item) => item.running).length,
        errors: jobs.reduce((sum, item) => sum + Number(item.error_count || 0), 0),
      },
      dbCards,
    });
  },

  async onRefresh() {
    await refreshOpsDomain();
  },
});
