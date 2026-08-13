import type { FactState } from "@/api/fact";
import type { WorkspaceId } from "@/types/contracts";

export const workspaceLabels: Record<WorkspaceId, { label: string; english: string; hint: string; kicker: string }> = {
  "trade-ops": { label: "交易运营", english: "Trade Ops", hint: "市场与执行", kicker: "01 / 市场工作台" },
  "risk-desk": { label: "风险台", english: "Risk Desk", hint: "风险裁决", kicker: "02 / 风险权威" },
  research: { label: "研究实验室", english: "Research Lab", hint: "证据画布", kicker: "03 / 证据画布" },
  governance: { label: "治理中心", english: "Governance", hint: "审查与提交", kicker: "04 / 控制平面" },
  ops: { label: "运维中心", english: "Ops", hint: "健康与恢复", kicker: "05 / 运维" },
};

export const factStateLabels: Record<FactState, string> = {
  known: "已确认",
  stale: "已过期",
  unknown: "未知",
  error: "错误",
};

const statusLabels: Readonly<Record<string, string>> = {
  connected: "已连接",
  connecting: "连接中",
  offline: "离线",
  "auth-failed": "认证失败",
  online: "在线",
  running: "运行中",
  stopped: "已停止",
  blocked: "已阻止",
  accepting: "允许",
  "server says accepting": "服务端允许",
  "server returned": "服务端已返回",
  "server only": "仅服务端",
  "server-owned": "服务端负责",
  "server-gated": "服务端门控",
  "server projection": "服务端投影",
  "read-only": "只读",
  "risk-reduction": "风险收紧",
  "risk-increase": "风险增加",
  control: "控制",
  allow: "允许",
  block: "阻止",
  unknown: "未知",
  known: "已确认",
  stale: "已过期",
  error: "错误",
  pending: "处理中",
  committed: "已提交",
  rejected: "已拒绝",
  aborted: "已中止",
  not_committed: "未提交",
  "browser fallback": "浏览器回退",
  active: "已启用",
  inactive: "未启用",
  completed: "已完成",
  cancelled: "已取消",
  reviewed: "已复核",
  bridge_ready: "可桥接",
  recorded: "已记录",
  aggregate: "汇总",
  good_win: "优质盈利",
  good_loss: "优质亏损",
  bad_loss: "不良亏损",
  lucky_win: "偶然盈利",
  normal: "正常",
  only_close: "仅允许平仓",
  no_new_risk: "禁止新增风险",
};

export function uiStatus(value: string | null | undefined): string {
  if (!value) return "未知";
  const normalized = value.trim().toLowerCase();
  return statusLabels[value] ?? statusLabels[normalized] ?? value;
}

export function uiFactState(state: FactState): string {
  return factStateLabels[state];
}

export function uiDirection(value: string | null | undefined): string {
  if (value === "long") return "多头";
  if (value === "short") return "空头";
  return "未知";
}

export function uiDecision(value: string | null | undefined): string {
  if (value === "allow") return "允许";
  if (value === "block") return "阻止";
  return "未知";
}

export function uiMode(value: string | null | undefined): string {
  if (value === "no_new_risk") return "禁止新增风险";
  if (value === "only_close") return "仅允许平仓";
  return uiStatus(value);
}
