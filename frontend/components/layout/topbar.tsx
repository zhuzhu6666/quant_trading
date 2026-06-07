"use client";
import { useAppStore } from "@/lib/store";
import { fmtNum, fmtPct, classNames } from "@/lib/format";

export function Topbar() {
  const { snapshot, wsConnected } = useAppStore();
  const eq = snapshot?.equity ?? 0;
  const pnl = snapshot?.pnl_today ?? 0;
  const dir = snapshot?.position?.dir ?? "FLAT";
  return (
    <header className="h-14 bg-bg-card border-b border-bg-border flex items-center px-6 gap-6 sticky top-0 z-10">
      <div className="text-fg-muted text-sm">XAUUSD+</div>
      {snapshot && (
        <div className="num text-fg">
          Equity <span className={classNames("font-semibold", pnl >= 0 ? "text-up" : "text-down")}>{fmtNum(eq)}</span>
        </div>
      )}
      {snapshot && (
        <div className="num text-sm text-fg-muted">
          Today <span className={pnl >= 0 ? "text-up" : "text-down"}>{fmtPct(pnl)}</span>
        </div>
      )}
      {dir !== "FLAT" && (
        <div className="num text-sm">
          Pos <span className={dir === "LONG" ? "text-up" : "text-down"}>{dir}</span>
        </div>
      )}
      <div className="ml-auto flex items-center gap-2 text-sm">
        <span className={classNames("w-2 h-2 rounded-full", wsConnected ? "bg-up animate-pulse" : "bg-warn")} />
        <span className="text-fg-muted">{wsConnected ? "● live" : "⚠ 离线"}</span>
      </div>
    </header>
  );
}
