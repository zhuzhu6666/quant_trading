import { useEffect, useRef } from "react";
import { useAliveRef } from "./useAliveRef";

export function usePolling(fn: () => Promise<void> | void, interval: number, deps: any[] = []): void {
  const aliveRef = useAliveRef();
  const fnRef = useRef(fn);
  fnRef.current = fn;
  useEffect(() => {
    let id: ReturnType<typeof setInterval> | null = null;
    const tick = async () => {
      if (!aliveRef.current) return;
      if (typeof document !== "undefined" && document.hidden) return;
      try {
        await fnRef.current();
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
