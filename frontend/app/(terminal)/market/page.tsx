"use client";
import { useEffect, useState } from "react";
import { authFetch } from "@/lib/auth";
import { Candlestick, CandleBar } from "@/components/charts/candlestick";

export default function MarketPage() {
  const [bars, setBars] = useState<CandleBar[]>([]);
  const [tf, setTf] = useState("M15");
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    // (audit v5 fix B-7: AbortController so switching tf quickly does not let
    // an old fetch overwrite the new tf's data.)
    const ctrl = new AbortController();
    setLoading(true);
    authFetch(`/api/market/bars?symbol=XAUUSD%2B&timeframe=${tf}&limit=500`, { signal: ctrl.signal })
      .then((r) => r.json())
      .then((d) => setBars(d.bars as CandleBar[]))
      .catch((e) => {
        if (e.name !== "AbortError") console.error("bars fetch failed", e);
      })
      .finally(() => { if (!ctrl.signal.aborted) setLoading(false); });
    return () => ctrl.abort();
  }, [tf]);

  const last = bars.length > 0 ? bars[bars.length - 1] : null;

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
      <div className="bg-bg-card border border-bg-border rounded p-4 space-y-2">
        <div className="flex items-center justify-between text-sm">
          <div className="text-fg-muted">
            {tf} · {loading ? "加载中..." : `${bars.length} 根 bar`}
          </div>
          {last && (
            <div className="num text-fg-muted">
              最新 {new Date(last.t * 1000).toLocaleString()} · 收{" "}
              <span className={last.c >= last.o ? "text-up" : "text-down"}>{last.c}</span>
            </div>
          )}
        </div>
        {bars.length > 0 ? (
          <Candlestick bars={bars} height={480} />
        ) : (
          <div className="text-fg-muted text-center py-12">{loading ? "加载中..." : "无数据"}</div>
        )}
      </div>
    </div>
  );
}
