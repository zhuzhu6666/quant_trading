import { useEffect, useState, useCallback } from "react";
import { authFetch } from "@/lib/auth";
import { Card } from "@/components/ui/Card";
import { Badge } from "@/components/ui/Badge";

interface V4Signal {
  direction: string;
  score: number;
  tactical_score: number;
  macro_score: number;
  n_active_factors: number;
  gate_reason: string;
}

interface V4Status {
  pipeline_active: boolean;
  engine_warm: boolean;
  buffer_size: number;
  n_attribution_trades: number;
  awe_conviction: number;
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
  recent_signals: V4Signal[];
  v4_status: V4Status;
}

const DIR_LABELS: Record<string, string> = {
  LONG: "做多", SHORT: "做空", FLAT: "空仓",
};

const DIR_COLORS: Record<string, string> = {
  LONG: "#34C759", SHORT: "#FF3B30", FLAT: "#86868B",
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
  const v4 = s.v4_status;
  const lastSignal = s.recent_signals.length > 0 ? s.recent_signals[s.recent_signals.length - 1] : null;

  return (
    <Card className="flex flex-col" padding="sm">
      {/* Header */}
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2">
          <span className={`w-2 h-2 rounded-full ${s.running ? "bg-success" : "bg-text-tertiary"}`} />
          <span className="text-xs text-text-primary font-semibold">
            {s.running ? "因子管道运行中" : "因子管道停止"}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant="default">v4</Badge>
          <Badge variant={s.mode === "LIVE" ? "success" : "warning"}>
            {s.mode}
          </Badge>
        </div>
      </div>

      {/* V4 Pipeline Status */}
      {v4 && (
        <div className="grid grid-cols-2 gap-x-3 gap-y-2 text-xs mb-3 pb-3 border-b border-apple-divider">
          <div className="flex justify-between">
            <span className="text-text-secondary">管道</span>
            <span className={v4.pipeline_active ? "text-success" : "text-text-tertiary"}>
              {v4.pipeline_active ? "活跃" : "未初始化"}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-text-secondary">引擎</span>
            <span className={v4.engine_warm ? "text-success" : "text-warning"}>
              {v4.engine_warm ? `就绪 (${v4.buffer_size})` : `预热 ${v4.buffer_size}/50`}
            </span>
          </div>
          <div className="flex justify-between">
            <span className="text-text-secondary">归因交易</span>
            <span className="text-text-primary font-mono">{v4.n_attribution_trades} 笔</span>
          </div>
          <div className="flex justify-between">
            <span className="text-text-secondary">信念分</span>
            <span className={`font-mono ${v4.awe_conviction >= 0.7 ? "text-success" : v4.awe_conviction >= 0.4 ? "text-text-primary" : "text-warning"}`}>
              {v4.awe_conviction.toFixed(2)}
            </span>
          </div>
        </div>
      )}

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
        <span className={s.circuit_breaker ? "text-danger font-semibold" : "text-text-primary"}>
          {s.reason}
        </span>
      </div>

      {/* Recent Signals (v4 format) */}
      {lastSignal && (
        <div className="text-2xs space-y-2 pt-2 border-t border-apple-divider">
          <div className="flex items-center gap-2">
            <span className="text-text-secondary">最近信号</span>
            <span className="font-bold" style={{ color: DIR_COLORS[lastSignal.direction] ?? "#86868B" }}>
              {DIR_LABELS[lastSignal.direction] ?? lastSignal.direction}
            </span>
            <span className="text-text-primary font-mono">
              score={lastSignal.score.toFixed(3)}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-x-3 gap-y-1">
            <div className="flex justify-between">
              <span className="text-text-secondary">战术层</span>
              <span className="text-text-primary font-mono">{lastSignal.tactical_score.toFixed(3)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-secondary">宏观层</span>
              <span className="text-text-primary font-mono">{lastSignal.macro_score.toFixed(3)}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-secondary">活跃因子</span>
              <span className="text-text-primary font-mono">{lastSignal.n_active_factors}</span>
            </div>
            <div className="flex justify-between">
              <span className="text-text-secondary">闸门</span>
              <span className={lastSignal.gate_reason === "passed" ? "text-success font-mono" : "text-warning font-mono"}>
                {lastSignal.gate_reason}
              </span>
            </div>
          </div>
        </div>
      )}

      {!lastSignal && s.running && (
        <div className="text-2xs text-text-secondary pt-2 border-t border-apple-divider">
          {v4?.engine_warm ? "等待闸门通过的信号..." : "因子引擎预热中..."}
        </div>
      )}
    </Card>
  );
}
