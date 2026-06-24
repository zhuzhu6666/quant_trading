function clone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

export function createStore(initialState = {}) {
  let state = clone(initialState);
  const listeners = [];

  function getState() {
    return clone(state);
  }

  function setState(patch) {
    state = { ...state, ...patch };
    listeners.forEach((fn) => {
      try {
        fn(getState());
      } catch (err) {
        console.warn('[store] listener error', err);
      }
    });
  }

  function subscribe(fn) {
    listeners.push(fn);
    return () => {
      const idx = listeners.indexOf(fn);
      if (idx >= 0) listeners.splice(idx, 1);
    };
  }

  return {
    getState,
    setState,
    subscribe,
  };
}
