export type AuthSnapshot = {
  token: string | null;
  user: string | null;
  loading: boolean;
  authenticated: boolean;
};

export function authStateAfterMeFailure(previous: AuthSnapshot, status?: number): AuthSnapshot {
  if (status === 401) {
    return {
      ...previous,
      token: null,
      user: null,
      loading: false,
      authenticated: false,
    };
  }
  return {
    ...previous,
    loading: false,
    authenticated: Boolean(previous.token),
  };
}
