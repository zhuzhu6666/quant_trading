let accessToken: string | null = null;
const listeners = new Set<(token: string | null) => void>();

export function getAccessToken(): string | null {
  return accessToken;
}

export function setAccessToken(token: string): void {
  accessToken = token;
  for (const listener of listeners) listener(accessToken);
}

export function clearAccessToken(): void {
  accessToken = null;
  for (const listener of listeners) listener(accessToken);
}

export function subscribeAccessToken(listener: (token: string | null) => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Access tokens are intentionally never serialized or persisted. */
export function hasAccessToken(): boolean {
  return accessToken !== null;
}
