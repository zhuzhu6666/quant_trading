export type TimeValue = string | number | Date | null | undefined;

const DISPLAY_TIME_ZONE = "Asia/Shanghai";
const SECOND = 1_000;

let serverClockAnchor: { serverSeconds: number; localMonotonicMs: number } | null = null;

const dateFormatter = new Intl.DateTimeFormat("zh-CN", {
  timeZone: DISPLAY_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function monotonicNowMs(): number {
  return typeof performance !== "undefined" ? performance.now() : Date.now();
}

export function epochSeconds(value: TimeValue): number {
  if (value instanceof Date) {
    const milliseconds = value.getTime();
    return Number.isFinite(milliseconds) ? milliseconds / SECOND : 0;
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    return value > 1e12 ? value / SECOND : value;
  }
  if (typeof value === "string" && value.trim()) {
    const numeric = Number(value);
    if (Number.isFinite(numeric)) return numeric > 1e12 ? numeric / SECOND : numeric;
    const parsed = Date.parse(value);
    return Number.isFinite(parsed) ? parsed / SECOND : 0;
  }
  return 0;
}

/**
 * Anchor display ages to the server clock instead of each Windows clock.
 * The anchor advances with a monotonic clock so a local wall-clock change
 * cannot make every fact jump backwards or forwards together.
 */
export function syncServerClock(value: TimeValue): void {
  const serverSeconds = epochSeconds(value);
  if (serverSeconds <= 0 || !Number.isFinite(serverSeconds)) return;
  serverClockAnchor = { serverSeconds, localMonotonicMs: monotonicNowMs() };
}

export function syncServerClockFromPayload(payload: unknown): void {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return;
  const source = payload as Record<string, unknown>;
  const fact = source._fact && typeof source._fact === "object" && !Array.isArray(source._fact)
    ? source._fact as Record<string, unknown>
    : {};
  const generatedAt = fact.generated_at;
  const serverTime = source.server_time ?? source.serverTime;
  syncServerClock(
    (serverTime ?? generatedAt) as TimeValue,
  );
}

export function serverNowSeconds(): number {
  if (!serverClockAnchor) return Date.now() / SECOND;
  return serverClockAnchor.serverSeconds + (monotonicNowMs() - serverClockAnchor.localMonotonicMs) / SECOND;
}

function dateParts(value: TimeValue): Record<string, string> | null {
  const seconds = epochSeconds(value);
  if (seconds <= 0) return null;
  const date = new Date(seconds * SECOND);
  if (!Number.isFinite(date.getTime())) return null;
  return Object.fromEntries(dateFormatter.formatToParts(date).map((part) => [part.type, part.value]));
}

/** Render every server timestamp with one stable desktop format. */
export function formatTimestamp(value: TimeValue, fallback = "时间未知"): string {
  const parts = dateParts(value);
  if (!parts) return fallback;
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

/** Render a fact observation with the same absolute and relative time pair everywhere. */
export function formatObservedTime(value: TimeValue, fallback = "时间未知"): string {
  const timestamp = formatTimestamp(value, fallback);
  const observedAt = epochSeconds(value);
  if (observedAt <= 0) return timestamp;
  return `${timestamp} · ${formatAgeSeconds(serverNowSeconds() - observedAt)}`;
}

export function formatClock(value: TimeValue, fallback = "时间未知"): string {
  const parts = dateParts(value);
  if (!parts) return fallback;
  return `${parts.hour}:${parts.minute}:${parts.second}`;
}

/** Render a compact absolute date/time label without falling back to browser time. */
export function formatShortDateTime(value: TimeValue, fallback = "时间未知"): string {
  const parts = dateParts(value);
  if (!parts) return fallback;
  return `${parts.month}-${parts.day} ${parts.hour}:${parts.minute}`;
}

export function formatAgeSeconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "年龄未知";
  if (value < -5) return "时钟偏差";
  const seconds = Math.max(0, value);
  if (seconds < 1) return "刚刚";
  if (seconds < 60) return `${Math.round(seconds)}秒前`;
  if (seconds < 3_600) {
    const minutes = Math.floor(seconds / 60);
    const remainder = Math.floor(seconds % 60);
    return `${minutes}分${String(remainder).padStart(2, "0")}秒前`;
  }
  if (seconds < 86_400) {
    const hours = Math.floor(seconds / 3_600);
    const minutes = Math.floor((seconds % 3_600) / 60);
    return `${hours}小时${String(minutes).padStart(2, "0")}分前`;
  }
  const days = Math.floor(seconds / 86_400);
  const hours = Math.floor((seconds % 86_400) / 3_600);
  return `${days}天${String(hours).padStart(2, "0")}小时前`;
}
