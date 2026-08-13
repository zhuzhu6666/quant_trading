import * as DialogPrimitive from "@radix-ui/react-dialog";
import * as PopoverPrimitive from "@radix-ui/react-popover";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";
import { factAgeSeconds, type FactEnvelope } from "@/api/fact";
import { formatAgeSeconds, formatTimestamp } from "@/api/time";
import { factStateLabels } from "@/i18n/zh-CN";

export function Panel({ children, className = "", title, eyebrow }: { children: ReactNode; className?: string; title?: string; eyebrow?: string }) {
  return (
    <section className={`wb-panel ${className}`}>
      {(eyebrow || title) && <header className="wb-panel-header">{eyebrow && <span className="wb-eyebrow">{eyebrow}</span>}{title && <h2>{title}</h2>}</header>}
      {children}
    </section>
  );
}

export function FactBadge({ fact, label, compact = false }: { fact: FactEnvelope; label?: string; compact?: boolean }) {
  const stateLabel = factStateLabels[fact.state];
  const timingTitle = `观测 ${formatTimestamp(fact.observed_at, "未知")} · 年龄 ${formatAgeSeconds(factAgeSeconds(fact))}`;
  return (
    <span className={`fact-badge fact-${fact.state}`} title={[timingTitle, fact.reason_code].filter(Boolean).join(" · ")}>
      <span className="fact-badge-dot" aria-hidden="true" />
      <span>{label ? `${label} · ` : ""}{stateLabel}</span>
      {fact.reason_code && !compact && <code>{fact.reason_code}</code>}
    </span>
  );
}

export function SourceLine({ fact }: { fact: FactEnvelope }) {
  const age = factAgeSeconds(fact);
  return (
    <div className="source-line">
      <span>来源：{fact.source || "无"}</span>
      <span>观测：{formatTimestamp(fact.observed_at, "—")}</span>
      <span>年龄：{formatAgeSeconds(age)}</span>
    </div>
  );
}

export function MetricValue({ value, unit = "", unavailable = "—" }: { value: number | string | null; unit?: string; unavailable?: string }) {
  return <span className="metric-value">{value === null || value === "" ? unavailable : `${value}${unit}`}</span>;
}

export function EmptyFact({ fact, message = "事实待确认" }: { fact: FactEnvelope; message?: string }) {
  return <div className={`empty-fact empty-fact-${fact.state}`}><FactBadge fact={fact} /><span>{message}</span>{fact.reason_code && <code>{fact.reason_code}</code>}</div>;
}

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogTitle = DialogPrimitive.Title;
export const DialogClose = DialogPrimitive.Close;

export function DialogSurface({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <DialogPrimitive.Portal><DialogPrimitive.Overlay className="wb-dialog-overlay" /><DialogPrimitive.Content className={`wb-dialog-content ${className}`}>{children}</DialogPrimitive.Content></DialogPrimitive.Portal>;
}

export const Popover = PopoverPrimitive.Root;
export const PopoverTrigger = PopoverPrimitive.Trigger;
export function PopoverSurface({ children, className = "" }: { children: ReactNode; className?: string }) {
  return <PopoverPrimitive.Portal><PopoverPrimitive.Content sideOffset={8} className={`wb-popover-content ${className}`}>{children}<PopoverPrimitive.Arrow className="wb-popover-arrow" /></PopoverPrimitive.Content></PopoverPrimitive.Portal>;
}

export const Tabs = TabsPrimitive.Root;
export const TabsList = TabsPrimitive.List;
export const TabsTrigger = TabsPrimitive.Trigger;
export const TabsContent = TabsPrimitive.Content;

export function Tooltip({ children, content }: { children: ReactNode; content: string }) {
  return <TooltipPrimitive.Provider delayDuration={300}><TooltipPrimitive.Root><TooltipPrimitive.Trigger asChild>{children}</TooltipPrimitive.Trigger><TooltipPrimitive.Portal><TooltipPrimitive.Content className="wb-tooltip" sideOffset={6}>{content}</TooltipPrimitive.Content></TooltipPrimitive.Portal></TooltipPrimitive.Root></TooltipPrimitive.Provider>;
}
