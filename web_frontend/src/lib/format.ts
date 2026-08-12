export const clamp = (value: number, min: number, max: number): number =>
  Math.min(Math.max(value, min), max);

export const safeNumber = (value: unknown, fallback = 0): number => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const normalizeCurrency = (currency: unknown): string => {
  const text = String(currency || "").trim().toUpperCase();
  return /^[A-Z]{3}$/.test(text) ? text : "";
};

export const formatMoney = (value: unknown, currency = ""): string => {
  const amount = safeNumber(value, 0);
  const safeCurrency = normalizeCurrency(currency);
  if (!safeCurrency) return amount.toLocaleString("zh-CN", { maximumFractionDigits: 2, minimumFractionDigits: 2 });
  try {
    return new Intl.NumberFormat("zh-CN", {
      style: "currency",
      currency: safeCurrency,
      maximumFractionDigits: 2,
    }).format(amount);
  } catch {
    return `${safeCurrency} ${amount.toFixed(2)}`;
  }
};

export const formatDecimal = (value: unknown, digits = 2): string =>
  safeNumber(value, 0).toLocaleString("en-US", {
    maximumFractionDigits: digits,
    minimumFractionDigits: 0,
  });

export const formatRate = (value: unknown): string => `${formatDecimal(value, 2)}%`;

const toDate = (value: unknown): Date | null => {
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) return new Date(parsed);
  }
  const ts = safeNumber(value, 0);
  if (!ts) return null;
  const date = new Date(ts > 1e12 ? ts : ts * 1000);
  return Number.isNaN(date.getTime()) ? null : date;
};

const dateTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const clockTimeFormatter = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
});

export const formatTime = (value: unknown): string => {
  const date = toDate(value);
  return date ? dateTimeFormatter.format(date) : "";
};

/** Format a bar's open timestamp as a visible start→end interval. */
export const formatTimeRange = (startValue: unknown, durationSeconds: number): string => {
  const start = toDate(startValue);
  const duration = safeNumber(durationSeconds, 0);
  if (!start || duration <= 0) return "";
  const end = new Date(start.getTime() + duration * 1000);
  return `${clockTimeFormatter.format(start)}–${clockTimeFormatter.format(end)}`;
};
