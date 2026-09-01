import { apiRequest } from "@/api/client";
import { readFact } from "@/api/fact";
import type {
  Bar,
  MarketBars,
  RealizedPnlPoint,
  RealizedPnlScope,
  RealizedPnlSeries,
} from "@/types/contracts";
import {
  arrayField,
  numberValue,
  object,
  stringValue,
} from "@/api/domains/shared";

export function decodeMarketBars(payload: unknown, symbol: string, timeframe: string): MarketBars {
  const source = object(payload);
  const bars: Bar[] = arrayField(source, "bars").flatMap((value) => {
    const bar = object(value);
    const t = numberValue(bar, "t");
    const o = numberValue(bar, "o");
    const h = numberValue(bar, "h");
    const l = numberValue(bar, "l");
    const c = numberValue(bar, "c");
    if ([t, o, h, l, c].some((entry) => entry === null)) return [];
    return [{ t: t as number, o: o as number, h: h as number, l: l as number, c: c as number, v: numberValue(bar, "v") ?? 0, spread: numberValue(bar, "spread") ?? 0 }];
  });
  const fact = readFact(source, "market.bars.v1");
  const range = object(source.range);
  return {
    fact,
    symbol,
    timeframe,
    bars,
    total: numberValue(source, "total") ?? bars.length,
    rangeFrom: numberValue(range, "from"),
    rangeTo: numberValue(range, "to"),
  };
}

function realizedPnlScope(value: unknown): RealizedPnlScope {
  return value === "today" || value === "24h" || value === "7d" || value === "30d" || value === "all" ? value : "today";
}

export function decodeRealizedPnlSeries(payload: unknown): RealizedPnlSeries {
  const source = object(payload);
  const rawPoints: Array<RealizedPnlPoint & { cumulativeFromServer: number | null }> = arrayField(source, "points").flatMap((value) => {
    const point = object(value);
    const ts = numberValue(point, "ts") ?? numberValue(point, "exec_timestamp") ?? numberValue(point, "closed_at");
    const pnl = numberValue(point, "pnl");
    if (ts === null || pnl === null) return [];
    return [{
      ts,
      pnl,
      cumulative: 0,
      cumulativeFromServer: numberValue(point, "cumulative"),
      source: stringValue(point, "source"),
    }];
  });
  rawPoints.sort((left, right) => left.ts - right.ts);
  let running = 0;
  const points = rawPoints.map(({ cumulativeFromServer, ...point }) => {
    running = cumulativeFromServer ?? running + point.pnl;
    return { ...point, cumulative: cumulativeFromServer ?? running };
  });
  const summary = object(source.summary);
  return {
    fact: readFact(source, "live.realized-pnl.v2"),
    scope: realizedPnlScope(source.scope),
    currency: stringValue(source, "currency"),
    fromTs: numberValue(source, "from_ts"),
    toTs: numberValue(source, "to_ts"),
    summary: {
      realizedPnl: numberValue(summary, "realized_pnl"),
      trades: numberValue(summary, "trades"),
      wins: numberValue(summary, "wins"),
      losses: numberValue(summary, "losses"),
      winRate: numberValue(summary, "win_rate"),
    },
    points,
  };
}

export type MarketBarsSource = "monthly" | "live";

export function getMarketBars(
  symbol = "XAUUSD+",
  timeframe = "M15",
  limit = 180,
  range?: { fromTs?: number; toTs?: number },
  source: MarketBarsSource = "monthly",
): Promise<MarketBars> {
  const params = new URLSearchParams({ symbol, timeframe, limit: String(limit) });
  if (range?.fromTs !== undefined) params.set("from", String(Math.floor(range.fromTs)));
  if (range?.toTs !== undefined) params.set("to", String(Math.ceil(range.toTs)));
  if (source !== "monthly") params.set("source", source);
  return apiRequest<unknown>(`/api/market/bars?${params.toString()}`).then((payload) => decodeMarketBars(payload, symbol, timeframe));
}

export function getRealizedPnlSeries(scope: RealizedPnlScope = "all"): Promise<RealizedPnlSeries> {
  const params = new URLSearchParams({ scope, tz: "Asia/Shanghai" });
  return apiRequest<unknown>(`/api/live/realized-pnl-series?${params.toString()}`).then(decodeRealizedPnlSeries);
}
