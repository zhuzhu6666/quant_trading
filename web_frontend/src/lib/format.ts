export const clamp = (value: number, min: number, max: number): number =>
  Math.min(Math.max(value, min), max);

export const safeNumber = (value: unknown, fallback = 0): number => {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
};

const normalizeCurrency = (currency: unknown): string => {
  const text = String(currency || "").trim().toUpperCase();
  return /^[A-Z]{3}$/.test(text) ? text : "EUR";
};

export const formatMoney = (value: unknown, currency = "USD"): string => {
  const amount = safeNumber(value, 0);
  const safeCurrency = normalizeCurrency(currency);
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

export const formatTime = (value: unknown): string => {
  if (typeof value === "string") {
    const parsed = Date.parse(value);
    if (!Number.isNaN(parsed)) {
      return new Intl.DateTimeFormat("zh-CN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        year: "numeric",
        month: "2-digit",
        day: "2-digit",
      }).format(new Date(parsed));
    }
  }
  const ts = safeNumber(value, 0);
  if (!ts) return "--";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(ts > 1e12 ? new Date(ts) : new Date(ts * 1000));
};
