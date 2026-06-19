import { useEffect, useState, useCallback } from "react";
import { authFetch } from "@/lib/auth";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";

interface Signal {
  direction: string;
  indicators: Record<string, number>;
  dry_run: boolean;
}

interface StrategyStatus {
  running: boolean;
  broker: string;
  strategy: string;
  mode: string;
  position: { dir: string; entry: number; count: number };
  circuit_breaker: boolean;
  circuit_reason: string;
  reason: string;
  recent_signals: Signal[];
}

const INDICATOR_LABELS: Record<string, string> = {
  rsi: "RSI", di: "DI", stoch: "随机", macd: "MACD", bb: "布林", atr: "ATR",
};

const DIR_COLORS: Record<string, string> = {
  LONG: "#34C759", SHORT: "#FF3B30", FLAT: "#86868B", CLOSE: "#FF9500",
};

const DIR_LABELS: Record<string, string> = {
  LONG: "做多", SHORT: "做空", FLAT: "空仓", CLOSE: "平仓",
};

export default function StrategyCard() {
  const [data, setData] = useState<StrategyStatus | null>(null);

  const fetch = useCallback(async () => {
    try {
      const r = await authFetch("/api/live/strategy-status");
      if (r.ok) setData(await r.json());
    } catch { /* best-effort */ }
  }, []);

  useEffect(() => {
    fetch();
    const t = setInterval(fetch, 3000);
    return () => clearInterval(t);
  }, [fetch]);

  if (!data) {
    return (
      <Card padding="sm">
        <div className="text-xs text-text-secondary text-center py-4">加载因子管道状态...</div>
      </Card>
    );
  }

  const s = data;
  const lastSignal = s.recent_signals.length > 0 ? s.recent_signals[s.recent_signals.length - 1] : null;

  return (
    <Card className="flex flex-col" padding="sm">
      {/* Header: strategy + status */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${s.running ? "bg-success" : "bg-text-tertiary"}`} />
          <span className="text-xs text-text-primary font-semibold">
            {s.running ? "因子管道运行中" : "因子管道停止"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="default">{s.strategy}</Badge>
          <Badge variant={s.mode === "LIVE" ? "success" : "warning"}>
            {s.mode}
          </Badge>
        </div>
      </div>

      {/* Position */}
      <div className="flex items-center gap-3 text-xs mb-3 pb-3 border-b border-apple-divider">
        <span className="text-text-secondary">持仓</span>
        <span className="font-bold" style={{ color: DIR_COLORS[s.position.dir] ?? "#86868B" }}>
          {DIR_LABELS[s.position.dir] ?? s.position.dir}
        </span>
        {s.position.dir !== "FLAT" && (
          <span className="text-text-secondary">@{s.position.entry.toFixed(2)}</span>
        )}
        <span className="text-text-secondary ml-auto">{s.position.count} 单</span>
      </div>

      {/* Decision reason */}
      <div className="text-xs mb-3">
        <span className="text-text-secondary">状态: </span>
        <span className={s.circuit_breaker ? "text-danger font-semibold" : s.position.dir !== "FLAT" ? "text-success" : "text-text-primary"}>
          {s.reason}
        </span>
      </div>

      {/* Recent signal indicators */}
      {lastSignal && (
        <div className="text-2xs space-y-1 pt-2 border-t border-apple-divider">
          <div className="flex items-center gap-2 mb-2">
            <span className="text-text-secondary">最近信号</span>
            <span className="font-bold" style={{ color: DIR_COLORS[lastSignal.direction] ?? "#86868B" }}>
              {DIR_LABELS[lastSignal.direction] ?? lastSignal.direction}
            </span>
            {lastSignal.dry_run && <span className="text-warning text-2xs">(dry-run)</span>}
          </div>
          {lastSignal.indicators && Object.keys(lastSignal.indicators).length > 0 && (
            <div className="grid grid-cols-3 gap-x-3 gap-y-1">
              {Object.entries(lastSignal.indicators).map(([k, v]) => (
                <div key={k} className="flex justify-between">
                  <span className="text-text-secondary">{INDICATOR_LABELS[k] ?? k}</span>
                  <span className="text-text-primary font-mono">{typeof v === "number" ? v.toFixed(2) : v}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* No signal */}
      {!lastSignal && s.running && (
        <div className="text-2xs text-text-secondary pt-2 border-t border-apple-divider">等待因子信号...</div>
      )}
    </Card>
  );
}
