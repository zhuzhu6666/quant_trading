export function fmtNum(n: number, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return n.toLocaleString("en-US", { minimumFractionDigits: decimals, maximumFractionDigits: decimals });
}

export function fmtPct(n: number, decimals = 2): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  return `${n >= 0 ? "+" : ""}${fmtNum(n, decimals)}%`;
}

export function fmtUSD(n: number): string {
  if (n === null || n === undefined || Number.isNaN(n)) return "--";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${fmtNum(Math.abs(n), 2)}`;
}

export function classNames(...names: (string | false | null | undefined)[]): string {
  return names.filter(Boolean).join(" ");
}
