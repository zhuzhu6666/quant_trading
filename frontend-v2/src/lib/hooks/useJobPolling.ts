import { useState, useRef, useCallback } from "react";
import { authFetch } from "@/lib/auth";

interface JobProgress {
  pct: number;
  step: string;
  status: string;
}

interface UseJobPollingResult<T> {
  progress: JobProgress | null;
  result: T | null;
  done: boolean;
  error: string | null;
  start: (jobId: string) => void;
  cancel: () => void;
}

export function useJobPolling<T = any>(
  fetchJobUrl: (id: string) => string,
  options?: { interval?: number; maxPolls?: number }
): UseJobPollingResult<T> {
  const [progress, setProgress] = useState<JobProgress | null>(null);
  const [result, setResult] = useState<T | null>(null);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const aliveRef = useRef(true);
  const timerRef = useRef<number | null>(null);

  const start = useCallback(
    (jobId: string) => {
      setProgress({ pct: 0, step: "Submitted", status: "queued" });
      setResult(null);
      setDone(false);
      setError(null);
      const interval = options?.interval ?? 2000;
      const maxPolls = options?.maxPolls ?? 600;
      let count = 0;
      const poll = async () => {
        if (!aliveRef.current || count >= maxPolls) return;
        count++;
        try {
          const r = await authFetch(fetchJobUrl(jobId));
          if (!r.ok) {
            setError(`Poll failed: ${r.status}`);
            setDone(true);
            return;
          }
          const d = await r.json();
          if (!aliveRef.current) return;
          setProgress({ pct: d.progress_pct ?? 0, step: d.current_step ?? "", status: d.status });
          if (d.status === "done") {
            setResult(d.result ?? d);
            setDone(true);
            return;
          }
          if (d.status === "error") {
            setError(d.error ?? "Job failed");
            setDone(true);
            return;
          }
          if (d.status === "cancelled") {
            setDone(true);
            return;
          }
          timerRef.current = window.setTimeout(poll, interval);
        } catch (e: any) {
          if (aliveRef.current) {
            setError(e.message ?? String(e));
            setDone(true);
          }
        }
      };
      poll();
    },
    [fetchJobUrl, options?.interval, options?.maxPolls]
  );

  const cancel = useCallback(() => {
    if (timerRef.current) clearTimeout(timerRef.current);
    aliveRef.current = false;
  }, []);

  return { progress, result, done, error, start, cancel };
}
