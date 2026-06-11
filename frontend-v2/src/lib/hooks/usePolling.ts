import { useEffect } from "react";
import { useAliveRef } from "./useAliveRef";

export function usePolling(fn: () => Promise<void> | void, interval: number, deps: any[] = []): void {
  const aliveRef = useAliveRef();
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      if (!aliveRef.current) return;
      // audit 2026-06-10: 切到其它 tab / 最小化窗口时停轮询 — 浏览器免费占 CPU
      // 带宽, 切回来时第一次 tick 自动重启 (setInterval 不停, 只是 tick 内部 short-circuit)
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        await fn();
      } catch {
        /* best-effort */
      }
    };
    id = setInterval(tick, interval);
    return () => {
      if (id) clearInterval(id);
    };
  }, [interval, ...deps]);
}
