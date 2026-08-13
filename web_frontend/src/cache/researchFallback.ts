import type { FactEnvelope } from "@/api/fact";
import { decodeMarketBars } from "@/api/workbench";
import { getResearchCache } from "@/cache/researchCache";
import type { Bar, MarketBars, ResearchSnapshot } from "@/types/contracts";

type CachedMarketPayload = {
  bars: Bar[];
  symbol: string;
  timeframe: string;
};

function cachedFact(contract: string, source: string, observedAt: string | number, generatedAt: string | number): FactEnvelope {
  return {
    envelope: "fact.v1",
    contract,
    state: "stale",
    source: source ? `cache:${source}` : "cache:unknown",
    observed_at: observedAt,
    generated_at: generatedAt,
    stale_after_sec: 0,
    reason_code: "offline_cache",
    components: {},
  };
}

function isMarketPayload(value: unknown): value is CachedMarketPayload {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const source = value as { bars?: unknown; symbol?: unknown; timeframe?: unknown };
  return Array.isArray(source.bars) && typeof source.symbol === "string" && typeof source.timeframe === "string";
}

export async function readCachedMarketBars(cacheKey: string, symbol: string, timeframe: string): Promise<MarketBars | null> {
  const entry = await getResearchCache<CachedMarketPayload>(cacheKey, "market.bars.v1");
  if (!entry || !isMarketPayload(entry.payload)) return null;
  const decoded = decodeMarketBars(entry.payload, symbol, timeframe);
  return { ...decoded, fact: cachedFact("market.bars.v1", entry.source, entry.observed_at, entry.generated_at) };
}

function isResearchSnapshot(value: unknown): value is ResearchSnapshot {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const source = value as { rows?: unknown; contract?: unknown; title?: unknown };
  return Array.isArray(source.rows) && typeof source.contract === "string" && typeof source.title === "string";
}

export async function readCachedResearchSnapshot(cacheKey: string, contract: "ops.replay.v2" | "research.snapshot.v1" | "factor.catalog.v4"): Promise<ResearchSnapshot | null> {
  const entry = await getResearchCache<ResearchSnapshot>(cacheKey, contract);
  if (!entry || !isResearchSnapshot(entry.payload)) return null;
  return {
    ...entry.payload,
    fact: cachedFact(contract, entry.source, entry.observed_at, entry.generated_at),
    observedAt: entry.observed_at,
    status: "stale",
  };
}
