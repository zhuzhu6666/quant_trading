import { useState, type ReactNode } from "react";
import { AlertTriangle, Check, ChevronDown, ChevronRight, LoaderCircle, LockKeyhole, ShieldAlert } from "lucide-react";
import { isStepUpRequiredError, stepUpAuth } from "@/api/client";
import { FactBadge, Panel, SourceLine } from "@/design-system/primitives";
import type { FactEnvelope } from "@/api/fact";
import type { MutationResult } from "@/types/contracts";
import { uiStatus } from "@/i18n/zh-CN";

export function WorkspaceTitle({ kicker, title, description, fact }: { kicker: string; title: string; description: string; fact?: FactEnvelope }) {
  return <div className="workspace-title"><div><span className="wb-eyebrow">{kicker}</span><h1>{title}</h1><p>{description}</p></div>{fact && <FactBadge fact={fact} />}</div>;
}

export function FactPanel({ title, fact, children, className = "" }: { title: string; fact: FactEnvelope; children?: ReactNode; className?: string }) {
  return <Panel title={title} className={className}><div className="fact-panel-status"><FactBadge fact={fact} /><SourceLine fact={fact} /></div>{children}</Panel>;
}

export function InlineFact({ fact, label, value }: { fact: FactEnvelope; label: string; value?: ReactNode }) {
  return <div className={`inline-fact inline-fact-${fact.state}`}><span className="inline-fact-label">{label}</span><FactBadge fact={fact} />{value !== undefined && <span className="inline-fact-value">{fact.state === "known" || fact.state === "stale" ? value : "—"}</span>}</div>;
}

export function CollapsiblePanel({ title, children, defaultOpen = true, className = "" }: { title: string; children: ReactNode; defaultOpen?: boolean; className?: string }) {
  const [open, setOpen] = useState(defaultOpen);
  return <section className={`wb-panel collapsible-panel ${className}`}><button type="button" className="collapse-header" onClick={() => setOpen((value) => !value)} aria-expanded={open}>{open ? <ChevronDown size={15} /> : <ChevronRight size={15} />}<strong>{title}</strong></button>{open && <div className="collapse-body">{children}</div>}</section>;
}

export function ActionResult({ result }: { result: MutationResult | null }) {
  if (!result) return null;
  const tone = result.status === "committed" ? "success" : result.status === "rejected" || result.status === "aborted" ? "danger" : "warning";
  return <div className={`action-result action-result-${tone}`}><span>{result.status === "committed" ? <Check size={14} /> : <AlertTriangle size={14} />}<strong>{uiStatus(result.status)}</strong></span><span>变更：{result.mutationId ?? "未知"}</span><span>审计：{result.auditId ?? "未知"}</span><span>提交：{uiStatus(result.commitStatus)}</span>{result.reasonCode && <code>{result.reasonCode}</code>}</div>;
}

type ServerActionTicketProps = {
  title: string;
  description: string;
  riskClass: "risk-increase" | "risk-reduction" | "control";
  onSubmit: () => Promise<MutationResult>;
  disabled?: boolean;
  requiresStepUp?: boolean;
  offline?: boolean;
};

export function ServerActionTicket({ title, description, riskClass, onSubmit, disabled = false, requiresStepUp = false, offline = false }: ServerActionTicketProps) {
  const [confirming, setConfirming] = useState(false);
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [stepUpOpen, setStepUpOpen] = useState(false);
  const [password, setPassword] = useState("");
  const [result, setResult] = useState<MutationResult | null>(null);

  const locallyBlocked = offline && riskClass === "risk-increase";
  const blocked = disabled || locallyBlocked;

  const submit = async () => {
    if (blocked) return;
    setWorking(true);
    setError(null);
    try {
      setResult(await onSubmit());
      setConfirming(false);
    } catch (cause) {
      if (isStepUpRequiredError(cause)) setStepUpOpen(true);
      else setError(cause instanceof Error ? cause.message : "服务端动作失败");
    } finally {
      setWorking(false);
    }
  };

  const stepUp = async () => {
    if (blocked) return;
    setWorking(true);
    setError(null);
    try {
      await stepUpAuth(password);
      setStepUpOpen(false);
      setPassword("");
      await submit();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "二次验证失败");
    } finally {
      setWorking(false);
    }
  };

  return <Panel title={title} eyebrow="动作票据" className="action-ticket"><div className="action-ticket-copy"><span className={`command-risk ${riskClass}`}>{uiStatus(riskClass)}</span><p>{description}</p></div><div className="action-ticket-controls">{!confirming ? <button type="button" className={`ticket-button ticket-${riskClass}`} disabled={blocked || working} onClick={() => setConfirming(true)}>{locallyBlocked ? "离线禁止风险增加" : disabled ? "服务端动作门禁止" : "准备提交"}</button> : <div className="confirm-strip"><LockKeyhole size={15} /><span>确认目标、影响范围和服务器复核结果后提交</span><button type="button" className="ticket-button ticket-primary" disabled={working || blocked} onClick={() => void submit()}>{working ? <LoaderCircle className="spin" size={14} /> : "确认提交"}</button><button type="button" className="ticket-button ticket-ghost" disabled={working} onClick={() => setConfirming(false)}>取消</button></div>}</div>{requiresStepUp && <small className="ticket-note">高影响动作若被服务端要求二次验证（step-up），会在本地短暂输入后重新提交；密码不落盘。</small>}{stepUpOpen && <div className="step-up-inline"><strong>需要服务端二次验证</strong><input type="password" value={password} onChange={(event) => setPassword(event.target.value)} autoComplete="current-password" placeholder="当前密码" /><button type="button" disabled={working || !password || blocked} onClick={() => void stepUp()}>验证并重试</button></div>}{error && <div className="action-error" role="alert"><ShieldAlert size={14} />{error}</div>}<ActionResult result={result} /></Panel>;
}
