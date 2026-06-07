"use client";
import { useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { FactorHealthRadar } from "@/components/charts/factor-health-radar";

interface Factor {
  // (audit v6-fix-2: real schema from /api/factor-health/latest, see factors/page.tsx)
  factor: string;
  score: number;
  status: "HEALTHY" | "WATCH" | "DECAYING";
  components: {
    mean_abs_ic: number;
    ic_stability: number;
    regime_consistency: number;
    decay_rate: number;
    independence: number;
  };
  n_obs: number;
  rolling_ic: number;
}

function flat(f: Factor) {
  return {
    name: f.factor,
    status: f.status,
    score: f.score,
    abs_ic: f.components.mean_abs_ic,
    stability: f.components.ic_stability,
    decay: f.components.decay_rate,
    regime_consistency: f.components.regime_consistency,
    independence: f.components.independence,
  };
}

export default function FactorDetailPage() {
  const params = useParams<{ name: string }>();
  const name = decodeURIComponent(params.name);
  const [factor, setFactor] = useState<Factor | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    async function load() {
      const r = await fetch("/api/factor-health/latest");
      const d = await r.json();
      const f = (d.report?.factors ?? []).find((x: Factor) => x.name === name);
      setFactor(f ?? null);
      setLoading(false);
    }
    load();
  }, [name]);

  if (loading) return <div className="text-fg-muted">加载中...</div>;
  if (!factor) return <div className="text-down">未找到因子: {name}</div>;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-4">
        <a href="/factors" className="text-fg-muted hover:text-fg text-sm">← 返回</a>
        <h1 className="text-2xl font-bold">{factor.name}</h1>
        <span className={
          factor.status === "HEALTHY" ? "text-up" :
          factor.status === "WATCH" ? "text-warn" : "text-down"
        }>{factor.status}</span>
        {/* (audit v6-fix-1: same Number.isFinite guard as factors/page.tsx. Single
          factor with NaN/insufficient data shouldn't crash the detail view.) */}
        <span className="text-fg-muted num">score {Number.isFinite(factor.score) ? factor.score.toFixed(1) : "--"}</span>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4">
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-sm mb-2">5 维评分雷达</div>
          <FactorHealthRadar metrics={factor} />
        </div>
        <div className="bg-bg-card border border-bg-border rounded p-4">
          <div className="text-fg-muted text-sm mb-2">IC 时序</div>
          <div className="text-fg-muted text-sm">
            <div className="num">abs IC: <span className="text-fg">{Number.isFinite(factor.abs_ic) ? factor.abs_ic.toFixed(4) : "--"}</span></div>
            <div className="num">stability: <span className="text-fg">{Number.isFinite(factor.stability) ? factor.stability.toFixed(3) : "--"}</span></div>
            <div className="num">decay: <span className="text-fg">{Number.isFinite(factor.decay) ? factor.decay.toFixed(3) : "--"}</span></div>
            <div className="num">regime_consistency: <span className="text-fg">{Number.isFinite(factor.regime_consistency) ? factor.regime_consistency.toFixed(3) : "--"}</span></div>
            <div className="num">independence: <span className="text-fg">{Number.isFinite(factor.independence) ? factor.independence.toFixed(3) : "--"}</span></div>
          </div>
          <div className="text-xs text-fg-muted mt-4">
            ⚠ 完整 IC 时间序列图(rolling IC)需 alpha/factor_health 历史报告,Phase 4 集成。
          </div>
        </div>
      </div>
    </div>
  );
}
