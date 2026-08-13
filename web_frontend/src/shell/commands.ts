import type { LiveStateStore } from "@/hooks/useLiveState";
import type { ReadinessView, WorkspaceId } from "@/types/contracts";
import { workspaceLabels } from "@/i18n/zh-CN";

export type WorkbenchCommand = {
  id: string;
  label: string;
  description: string;
  shortcut?: string;
  workspace?: WorkspaceId;
  riskClass: "read-only" | "risk-increase" | "risk-reduction" | "control";
  enabled: boolean;
  disabledReason?: string;
  execute: () => void | Promise<void>;
};

export function buildCommands({ workspace, live, readiness, navigate, refreshFacts, onStop, onEmergency }: {
  workspace: WorkspaceId;
  live: LiveStateStore;
  readiness: ReadinessView | null;
  navigate: (path: string) => void;
  refreshFacts: () => void;
  onStop: () => Promise<void>;
  onEmergency: () => Promise<void>;
}): WorkbenchCommand[] {
  const go = (target: WorkspaceId) => () => navigate(`/${target}`);
  const serverGate = live.snapshot?.actionGates ?? {};
  const canStop = serverGate.stop !== false;
  const canEmergency = serverGate.emergency_close !== false;
  return [
    { id: "nav.trade-ops", label: `打开${workspaceLabels["trade-ops"].label}`, description: "实时运行、行情和动作票据", shortcut: "Ctrl/Cmd+1", workspace: "trade-ops", riskClass: "read-only", enabled: workspace !== "trade-ops", execute: go("trade-ops") },
    { id: "nav.risk-desk", label: `打开${workspaceLabels["risk-desk"].label}`, description: "服务端风险快照、裁决和执行追踪", shortcut: "Ctrl/Cmd+2", workspace: "risk-desk", riskClass: "read-only", enabled: workspace !== "risk-desk", execute: go("risk-desk") },
    { id: "nav.research", label: `打开${workspaceLabels.research.label}`, description: "K 线、回放和证据链", shortcut: "Ctrl/Cmd+3", workspace: "research", riskClass: "read-only", enabled: workspace !== "research", execute: go("research") },
    { id: "nav.governance", label: `打开${workspaceLabels.governance.label}`, description: "候选、审查、mutation 和发布审计", shortcut: "Ctrl/Cmd+4", workspace: "governance", riskClass: "read-only", enabled: workspace !== "governance", execute: go("governance") },
    { id: "nav.ops", label: `打开${workspaceLabels.ops.label}`, description: "健康、恢复、事故和桌面诊断", shortcut: "Ctrl/Cmd+5", workspace: "ops", riskClass: "read-only", enabled: workspace !== "ops", execute: go("ops") },
    { id: "facts.refresh", label: "重新读取服务端事实", description: "刷新当前 HTTP 事实，不重建实时 WS", shortcut: "Ctrl/Cmd+Shift+R", riskClass: "read-only", enabled: true, execute: refreshFacts },
    { id: "risk.stop", label: "停止新增风险", description: "提交服务端 stop action；结果以 durable/audit response 为准", riskClass: "risk-reduction", enabled: canStop, disabledReason: canStop ? undefined : "服务端动作门当前禁止", execute: onStop },
    { id: "risk.emergency", label: "紧急平仓", description: "服务端复核并执行风险缩减；未知事实不会被前端替换为零值", riskClass: "risk-reduction", enabled: canEmergency, disabledReason: canEmergency ? undefined : "服务端动作门当前禁止", execute: onEmergency },
    { id: "risk.start", label: "启动实时循环", description: "需要服务端 readiness、权限和 step-up；当前版本未在命令面板中直接执行", riskClass: "risk-increase", enabled: false, disabledReason: readiness?.readyForLiveExecution === true ? "需从交易运营的动作票据确认" : "服务端就绪度未返回允许结果", execute: () => undefined },
  ];
}
