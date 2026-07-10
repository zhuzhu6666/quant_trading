import { apiRequest, type DbHealthPayload, type HealthResponse, type SystemLoadPayload } from "@/api/client";

export const getHealth = () => apiRequest<HealthResponse>("/api/health");
export const getSystemDbHealth = () => apiRequest<DbHealthPayload>("/api/system/db-health");
export const getSystemLoad = () => apiRequest<SystemLoadPayload>("/api/system/load");
export const getLogTail = (lines = 30) =>
  apiRequest<Record<string, unknown>>(`/api/logs/tail?lines=${encodeURIComponent(String(lines))}`);
export const getOpsAlerts = () => apiRequest<Record<string, unknown>>("/api/ops/alerts");
export const getOpsRecovery = () => apiRequest<Record<string, unknown>>("/api/ops/recovery");
export const getSyncStatus = () => apiRequest<Record<string, unknown>>("/api/sync/status");
export const getCtraderTokenStatus = () => apiRequest<Record<string, unknown>>("/api/ctrader/token-status");
export const getExternalDataStatus = () => apiRequest<Record<string, unknown>>("/api/data/external-status");
