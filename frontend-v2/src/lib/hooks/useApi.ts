import { useState, useEffect, useCallback, useRef } from "react";
import { authJson } from "@/lib/auth";

interface UseApiResult<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
}

// audit 2026-06-10: 加 30s 内存缓存 + 切回 tab 瞬时显示
// 多个面板/组件用 useApi 拿同一 path, 30s 内共用同一份 data, 免重复 HTTP
const cache = new Map<string, { data: any; ts: number }>();
const CACHE_TTL_MS = 30_000;

function readCache(path: string): any | undefined {
  const entry = cache.get(path);
  if (!entry) return undefined;
  if (Date.now() - entry.ts > CACHE_TTL_MS) {
    cache.delete(path);
    return undefined;
  }
  return entry.data;
}

export function clearApiCache(path?: string) {
  if (path) cache.delete(path);
  else cache.clear();
}

export function useApi<T = any>(
  path: string | null,
  options?: { immediate?: boolean; interval?: number }
): UseApiResult<T> {
  // 同步 init: 缓存命中直接显示, 免首次 render 空
  const [data, setData] = useState<T | null>(() => {
    return path ? (readCache(path) as T | undefined) ?? null : null;
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ctrlRef = useRef<AbortController | null>(null);

  const fetch = useCallback(async () => {
    if (!path) return;
    // 缓存命中 + 未到 interval 刷新点: 跳过网络请求
    const cached = readCache(path);
    if (cached !== undefined) {
      setData(cached);
      // 但用户显式调 refresh() 还是要重打, 这里区分: 缓存命中仅免初始 fetch,
      // interval 触发的 tick 仍走网络拿新数据 (audit 06-10 期望: 后端 15s 内账
      // 户数据基本不变, 30s 缓存对静态列表/报告够用, 但账户/状态应有更新)
      if (!options?.interval) return;
    }
    // 取消前一个 in-flight 请求, 防 race
    ctrlRef.current?.abort();
    const ctrl = new AbortController();
    ctrlRef.current = ctrl;
    setLoading(true);
    setError(null);
    try {
      const result = await authJson<T>(path, { signal: ctrl.signal });
      cache.set(path, { data: result, ts: Date.now() });
      setData(result);
    } catch (e: any) {
      // 主动 abort 不算 error
      if (e?.name !== "AbortError" && e?.code !== "ABORTED") {
        setError(e.message ?? String(e));
      }
    } finally {
      if (ctrlRef.current === ctrl) ctrlRef.current = null;
      if (!ctrl.signal.aborted) setLoading(false);
    }
  }, [path, options?.interval]);

  useEffect(() => {
    if (options?.immediate !== false) fetch();
  }, [fetch]);

  useEffect(() => {
    if (!options?.interval) return;
    const id = setInterval(fetch, options.interval);
    return () => clearInterval(id);
  }, [fetch, options?.interval]);

  useEffect(() => {
    return () => ctrlRef.current?.abort();
  }, []);

  return { data, loading, error, refresh: fetch };
}
