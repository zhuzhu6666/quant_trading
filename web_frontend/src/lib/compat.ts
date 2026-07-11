export type AnyRecord = Record<string, unknown>;

const WRAPPER_KEYS = [
  "data",
  "result",
  "payload",
  "detail",
  "details",
  "summary",
  "item",
  "items",
  "value",
  "response",
  "body",
  "snapshot",
] as const;

const INDEX_KEYS = new Set<string>(["0", "1", "2", "3", "4", "5", "6", "7", "8", "9"]);

export const isRecord = (value: unknown): value is AnyRecord =>
  !!value && typeof value === "object" && !Array.isArray(value);

function getByPath(input: AnyRecord, path: string): unknown {
  if (!path) {
    return undefined;
  }

  const parts = path.split(".").map((part) => part.trim()).filter(Boolean);
  if (!parts.length) {
    return undefined;
  }

  let current: unknown = input;
  for (const part of parts) {
    if (!isRecord(current)) {
      return undefined;
    }
    if (!(part in current)) {
      return undefined;
    }
    current = current[part];
    if (current === null || current === undefined) {
      return undefined;
    }
  }
  return current;
}

function pickValueInternal(input: unknown, keys: readonly string[], visited: Set<object>): unknown {
  if (input === null || input === undefined) {
    return undefined;
  }

  if (Array.isArray(input)) {
    for (const item of input) {
      const value = pickValueInternal(item, keys, visited);
      if (value !== undefined && value !== null) {
        return value;
      }
    }
    return undefined;
  }

  if (!isRecord(input)) {
    return undefined;
  }

  if (visited.has(input)) {
    return undefined;
  }
  visited.add(input);

  for (const key of keys) {
    const candidate = getByPath(input, key);
    if (candidate !== undefined && candidate !== null) {
      return candidate;
    }
    for (const direct of [key.replace(/_(.)/g, (_, c) => c.toUpperCase()), key.toLowerCase(), key.toUpperCase()]) {
      const byStyle = getByPath(input, direct);
      if (byStyle !== undefined && byStyle !== null) {
        return byStyle;
      }
    }
  }

  for (const wrapper of WRAPPER_KEYS) {
    if (Object.prototype.hasOwnProperty.call(input, wrapper)) {
      const child = input[wrapper];
      const fromWrapper = pickValueInternal(child, keys, visited);
      if (fromWrapper !== undefined && fromWrapper !== null) {
        return fromWrapper;
      }
    }
  }

  for (const [k, v] of Object.entries(input)) {
    if (!v) {
      continue;
    }
    if (INDEX_KEYS.has(k)) {
      continue;
    }
    const nested = pickValueInternal(v, keys, visited);
    if (nested !== undefined && nested !== null) {
      return nested;
    }
  }

  return undefined;
}

export const pick = (input: unknown, keys: readonly string[]): unknown => {
  return pickValueInternal(input, keys, new Set());
};

export const pickString = (input: unknown, keys: readonly string[], fallback = ""): string => {
  const value = pick(input, keys);
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === "string") {
    return value;
  }
  return String(value);
};

export const pickNumber = (input: unknown, keys: readonly string[], fallback = 0): number => {
  const value = pick(input, keys);
  if (value === undefined || value === null) {
    return fallback;
  }
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

export const pickBoolean = (input: unknown, keys: readonly string[], fallback = false): boolean => {
  const value = pick(input, keys);
  if (value === undefined || value === null) {
    return fallback;
  }
  if (typeof value === "boolean") {
    return value;
  }
  if (typeof value === "number") {
    return value !== 0;
  }
  const text = String(value).toLowerCase();
  return text === "true" || text === "ok" || text === "yes" || text === "running" || text === "healthy";
};

export const pickArray = (input: unknown, keys: readonly string[]): unknown[] => {
  const value = pick(input, keys);
  if (Array.isArray(value)) {
    return value;
  }
  if (isRecord(value)) {
    for (const arrayKey of ["items", "list", "data", "rows", "values"]) {
      const nested = value[arrayKey];
      if (Array.isArray(nested)) {
        return nested;
      }
    }
  }
  if (isRecord(input)) {
    for (const wrapper of WRAPPER_KEYS) {
      const wrapped = input[wrapper];
      if (Array.isArray(wrapped)) {
        return wrapped;
      }
    }
  }
  return [];
};

export const pickRecord = (input: unknown, keys: readonly string[]): AnyRecord | undefined => {
  const value = pick(input, keys);
  return isRecord(value) ? value : undefined;
};

export const pickObjectSummary = (
  input: unknown,
  keys: readonly string[],
  fallback = "",
): string => {
  const raw = pick(input, keys);
  if (raw === undefined || raw === null) {
    return fallback;
  }
  if (typeof raw === "string") {
    return raw;
  }
  if (typeof raw === "number" || typeof raw === "boolean") {
    return String(raw);
  }
  try {
    return JSON.stringify(raw);
  } catch {
    return String(raw);
  }
};

export const formatDirection = (value: unknown): string => {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "number") {
    if (value > 0) {
      return "LONG";
    }
    if (value < 0) {
      return "SHORT";
    }
    return "FLAT";
  }
  if (typeof value === "string") {
    const normalized = value.trim().toLowerCase();
    if (["1", "buy", "long", "up", "bull", "buying"].includes(normalized)) {
      return "LONG";
    }
    if (["-1", "sell", "short", "down", "bear", "selling"].includes(normalized)) {
      return "SHORT";
    }
    return value;
  }
  return "";
};

export const compactJson = (value: unknown, maxChars = 1600): string => {
  if (value === undefined || value === null) {
    return "";
  }
  try {
    const text = JSON.stringify(value, null, 2);
    if (text.length <= maxChars) {
      return text;
    }
    return `${text.slice(0, maxChars)}…(${text.length - maxChars} 字符被截断)`;
  } catch {
    return String(value);
  }
};

export const formatReadableTime = (value: unknown): string => {
  if (value === undefined || value === null) {
    return "";
  }
  if (typeof value === "number" && Number.isFinite(value)) {
    const ts = value > 1e12 ? value : value * 1000;
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(ts > 1e12 ? new Date(ts) : new Date(value));
  }
  if (typeof value === "string") {
    const d = new Date(value);
    if (Number.isNaN(d.getTime())) {
      return value;
    }
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(d);
  }
  return "";
};

export const asRecord = (value: unknown): AnyRecord => (isRecord(value) ? value : {});
