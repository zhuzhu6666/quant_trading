import { useQuery } from "@tanstack/react-query";
import { getBackendReadiness } from "@/api/domains/readiness";
import { queryKeys } from "@/api/queryKeys";

export function useBackendReadinessQuery() {
  return useQuery({
    queryKey: queryKeys.readiness,
    queryFn: getBackendReadiness,
    // Readiness is a page-entry/manual snapshot. Live runtime state comes
    // exclusively from /ws/state; do not create a second timer here.
    staleTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  });
}
