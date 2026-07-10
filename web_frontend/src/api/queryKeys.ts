export const queryKeys = {
  readiness: ["backend-readiness"] as const,
  health: ["health"] as const,
  systemLoad: ["system-load"] as const,
  account: ["account"] as const,
  loopStatus: ["loop-status"] as const,
  sessionStats: ["session-stats"] as const,
  riskSummary: ["risk-summary"] as const,
  dbHealth: ["db-health"] as const,
  logs: (limit: number) => ["logs-tail", limit] as const,
};
