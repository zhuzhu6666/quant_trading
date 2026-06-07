"use client";
import { useEffect, useState } from "react";

interface Bar { t: number; o: number; h: number; l: number; c: number; v: number; }

export default function MarketPage() {
  const [bars, setBars] = useState<Bar[]>([]);
  const [tf, setTf] = useState("M15");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setLoading(true);
    fetch(`/api/market/bars?symbol=XAUUSD%2B&timeframe=${tf}&limit=500`)
      .then((r) => r.json())
      .then((d) => setBars(d.bars))
      .finally(() => setLoading(false));
  }, [tf]);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">K线 / 市场数据</h1>
      <div className="flex gap-2">
        {["M5", "M15", "M30", "H1", "H4", "D1"].map((t) => (
          <button
            key={t}
            onClick={() => setTf(t)}
            className={`px-3 py-1 rounded text-sm ${tf === t ? "bg-accent text-bg" : "bg-bg-card border border-bg-border text-fg-muted"}`}
          >
            {t}
          </button>
        ))}
      </div>
      <div className="bg-bg-card border border-bg-border rounded p-4">
        {loading ? (
          <div className="text-fg-muted">加载中...</div>
        ) : (
          <div className="num text-sm text-fg-muted">
            返回 {bars.length} 根 bar
            {bars.length > 0 && (
              <div className="mt-2">
                最新: {new Date(bars[bars.length - 1].t * 1000).toLocaleString()} 收 {bars[bars.length - 1].c}
              </div>
            )}
            <div className="text-xs mt-2 text-fg-muted">⚠ 完整 TradingView LWC 渲染在 Phase 3 (Task 3.x) 实现</div>
          </div>
        )}
      </div>
    </div>
  );
}
