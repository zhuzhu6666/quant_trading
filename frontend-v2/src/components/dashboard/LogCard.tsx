import { useEffect, useRef, useState, useCallback } from "react";
import { authFetch } from "@/lib/auth";
import { Card } from "@/components/ui/Card";

interface LogTailResponse {
  lines: string[];
  total: number;
}

export default function LogCard() {
  const [lines, setLines] = useState<string[]>([]);
  const [autoScroll, setAutoScroll] = useState(true);
  const [paused, setPaused] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const pausedRef = useRef(false);

  const fetchLogs = useCallback(async () => {
    if (pausedRef.current) return;
    try {
      const r = await authFetch("/api/logs/tail?lines=40");
      if (r.ok) {
        const d: LogTailResponse = await r.json();
        setLines(d.lines ?? []);
      }
    } catch {
      // best-effort
    }
  }, []);

  useEffect(() => {
    fetchLogs();
    timerRef.current = setInterval(fetchLogs, 2000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, []); // stable — fetchLogs has empty deps, paused is in ref

  useEffect(() => {
    if (autoScroll && containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines, autoScroll]);

  // Keep ref in sync with state for the interval callback
  pausedRef.current = paused;
  const handleScroll = () => {
    if (!containerRef.current) return;
    const el = containerRef.current;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30;
    setAutoScroll(atBottom);
  };

  function lineStyle(l: string): { color: string; bg?: string } {
    if (l.includes("| WARNING  |") || l.includes("| WARNING |"))
      return { color: "#FF9500", bg: "rgba(255,149,0,0.06)" };
    if (l.includes("| ERROR") || l.includes("| CRITICAL"))
      return { color: "#FF3B30", bg: "rgba(255,59,48,0.06)" };
    if (l.includes("| DEBUG"))
      return { color: "#86868B" };
    if (l.includes("[catch-up]") || l.includes("[data_pull]"))
      return { color: "#0071E3" };
    return { color: "#1D1D1F" };
  }

  return (
    <Card className="flex flex-col" padding="sm" style={{ maxHeight: "400px" }}>
      <div className="flex items-center justify-between mb-2 flex-shrink-0">
        <div className="flex items-center gap-2">
          <span className="section-label">实时日志</span>
          <span className="text-2xs text-text-secondary">{lines.length} 行</span>
        </div>
        <div className="flex items-center gap-2">
          <button
            onClick={() => setPaused(!paused)}
            className={`text-2xs px-2.5 py-1 rounded-lg transition-all duration-200 ${
              paused
                ? "bg-warning-light text-warning font-medium"
                : "bg-apple-bg text-text-secondary hover:text-text-primary"
            }`}
          >
            {paused ? "▶ 继续" : "⏸ 暂停"}
          </button>
          <button
            onClick={() => {
              setAutoScroll(true);
              if (containerRef.current)
                containerRef.current.scrollTop = containerRef.current.scrollHeight;
            }}
            className="text-2xs px-2.5 py-1 rounded-lg bg-apple-bg text-text-secondary hover:text-text-primary transition-all duration-200"
          >
            ↓ 末尾
          </button>
        </div>
      </div>

      <div
        ref={containerRef}
        onScroll={handleScroll}
        className="flex-1 overflow-y-auto font-mono text-2xs leading-relaxed rounded-xl"
        style={{ background: "#FAFAFA" }}
      >
        {lines.length === 0 ? (
          <div className="text-text-secondary p-3 text-center">等待日志...</div>
        ) : (
          lines.map((l, i) => {
            const s = lineStyle(l);
            return (
              <div
                key={i}
                className="px-2 py-px whitespace-nowrap"
                style={{ color: s.color, background: s.bg }}
              >
                {l}
              </div>
            );
          })
        )}
      </div>
    </Card>
  );
}
