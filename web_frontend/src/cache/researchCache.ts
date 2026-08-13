import type { CacheContract, CacheEntry } from "@/types/contracts";

export type CacheStoreName = "market_snapshots" | "replay_snapshots" | "factor_snapshots" | "research_snapshots";

const DB_NAME = "quant-workbench-research";
const DB_VERSION = 1;
const STORE_NAMES: readonly CacheStoreName[] = ["market_snapshots", "replay_snapshots", "factor_snapshots", "research_snapshots"];
const FORBIDDEN_KEYS = new Set(["account", "positions", "risk", "safety", "readiness", "control", "access_token", "refresh_token", "token", "mutation_result", "authentication"]);

export function cacheContainsForbiddenFields(value: unknown): boolean {
  if (Array.isArray(value)) return value.some(cacheContainsForbiddenFields);
  if (!value || typeof value !== "object") return false;
  return Object.entries(value).some(([key, nested]) => FORBIDDEN_KEYS.has(key.toLowerCase()) || cacheContainsForbiddenFields(nested));
}

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") return Promise.reject(new Error("indexeddb_unavailable"));
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onerror = () => reject(request.error ?? new Error("indexeddb_open_failed"));
    request.onupgradeneeded = () => {
      for (const name of STORE_NAMES) {
        if (!request.result.objectStoreNames.contains(name)) request.result.createObjectStore(name, { keyPath: "cache_key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
  });
}

async function contentHash(payload: unknown): Promise<string> {
  const encoded = new TextEncoder().encode(JSON.stringify(payload));
  if (globalThis.crypto?.subtle) {
    const digest = await crypto.subtle.digest("SHA-256", encoded);
    return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
  }
  return Array.from(encoded).reduce((hash, byte) => ((hash * 33) ^ byte) >>> 0, 5381).toString(16);
}

function storeForContract(contract: CacheContract): CacheStoreName {
  if (contract === "market.bars.v1") return "market_snapshots";
  if (contract === "ops.replay.v2") return "replay_snapshots";
  if (contract === "factor.catalog.v4") return "factor_snapshots";
  return "research_snapshots";
}

export async function putResearchCache<T>(cacheKey: string, contract: CacheContract, payload: T, source: string, observedAt: string | number, expiresAt: string | number): Promise<CacheEntry<T>> {
  if (cacheContainsForbiddenFields(payload)) throw new Error("cache_payload_not_allowlisted");
  const now = new Date().toISOString();
  const entry: CacheEntry<T> = { cache_key: cacheKey, contract, schema_version: 1, payload, source, observed_at: observedAt, generated_at: now, expires_at: expiresAt, content_hash: await contentHash(payload) };
  const db = await openDatabase();
  await new Promise<void>((resolve, reject) => {
    const transaction = db.transaction(storeForContract(contract), "readwrite");
    transaction.objectStore(storeForContract(contract)).put(entry);
    transaction.oncomplete = () => resolve();
    transaction.onerror = () => reject(transaction.error ?? new Error("indexeddb_write_failed"));
  });
  db.close();
  return entry;
}

export async function getResearchCache<T>(cacheKey: string, contract: CacheContract): Promise<CacheEntry<T> | null> {
  const db = await openDatabase();
  const entry = await new Promise<CacheEntry<T> | null>((resolve, reject) => {
    const request = db.transaction(storeForContract(contract), "readonly").objectStore(storeForContract(contract)).get(cacheKey);
    request.onsuccess = () => resolve((request.result as CacheEntry<T> | undefined) ?? null);
    request.onerror = () => reject(request.error ?? new Error("indexeddb_read_failed"));
  });
  db.close();
  if (!entry || entry.contract !== contract || entry.schema_version !== 1 || cacheContainsForbiddenFields(entry.payload)) return null;
  const expectedHash = await contentHash(entry.payload);
  return expectedHash === entry.content_hash ? entry : null;
}

export async function clearResearchCache(): Promise<void> {
  const db = await openDatabase();
  await Promise.all(STORE_NAMES.map((name) => new Promise<void>((resolve, reject) => {
    const request = db.transaction(name, "readwrite").objectStore(name).clear();
    request.onsuccess = () => resolve();
    request.onerror = () => reject(request.error ?? new Error("indexeddb_clear_failed"));
  })));
  db.close();
}
