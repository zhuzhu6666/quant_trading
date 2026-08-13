export type LiveSnapshotProbe = {
  _fact?: {
    envelope?: unknown;
    contract?: unknown;
  };
  generated_at?: string | number | null;
};

export function isCompleteLiveSnapshot(value: unknown): value is LiveSnapshotProbe {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const fact = (value as LiveSnapshotProbe)._fact;
  return fact?.envelope === "fact.v1" && fact.contract === "live.state.v2";
}

export function snapshotTimestamp(value: { generated_at?: string | number | null } | null): number {
  const generatedAt = value?.generated_at;
  if (typeof generatedAt === "number" && Number.isFinite(generatedAt)) return generatedAt;
  if (typeof generatedAt === "string") {
    const parsed = Date.parse(generatedAt);
    return Number.isFinite(parsed) ? parsed / 1000 : 0;
  }
  return 0;
}

export function shouldAcceptSnapshot(previousTimestamp: number, nextTimestamp: number): boolean {
  return previousTimestamp <= 0 || nextTimestamp <= 0 || nextTimestamp >= previousTimestamp;
}

export function reconnectDelay(attempt: number, jitter = 0): number {
  const boundedAttempt = Math.min(Math.max(1, attempt), 6);
  const base = Math.min(30_000, 1_500 * 2 ** (boundedAttempt - 1));
  return Math.min(30_000, base + Math.max(0, Math.min(250, jitter)));
}

export function isAuthenticationClose(code: number): boolean {
  return code === 4001 || code === 4003;
}
