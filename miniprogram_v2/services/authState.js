export function authStateAfterMeFailure(previous = {}, token = '', statusCode) {
  if (Number(statusCode) === 401) {
    return {
      clearToken: true,
      authenticated: false,
      statePatch: { token: '', user: null, isAuthenticated: false, busy: false },
    };
  }
  return {
    clearToken: false,
    authenticated: Boolean(token),
    statePatch: {
      ...previous,
      token,
      isAuthenticated: Boolean(token),
      busy: false,
    },
  };
}
