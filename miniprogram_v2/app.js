import { bootstrapAuth } from './services/auth';
import { startLiveRuntime, stopLiveRuntime } from './services/live';
import sessionStore from './stores/session';

App({
  globalData: {
    appReady: false,
  },

  async onLaunch() {
    const authOk = await bootstrapAuth();
    if (authOk) {
      startLiveRuntime();
    }
    this.globalData.appReady = true;
  },

  async ensureRuntime() {
    const state = sessionStore.getState();
    if (state.isAuthenticated) {
      startLiveRuntime();
      return true;
    }
    return false;
  },

  async afterLogin() {
    startLiveRuntime();
  },

  beforeLogout() {
    stopLiveRuntime();
  },
});
