import { apiRequest, type BackendReadinessPayload } from "@/api/client";

/** Read-only governance contract shared by every dashboard domain. */
export function getBackendReadiness(): Promise<BackendReadinessPayload> {
  return apiRequest<BackendReadinessPayload>("/api/ops/backend-readiness");
}
