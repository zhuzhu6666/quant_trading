import { useQuery } from "@tanstack/react-query";
import { getBackendReadiness } from "@/api/domains/readiness";
import { queryKeys } from "@/api/queryKeys";

export function useBackendReadinessQuery(interval = 15_000) {
  return useQuery({
    queryKey: queryKeys.readiness,
    queryFn: getBackendReadiness,
    refetchInterval: interval,
    staleTime: Math.min(5_000, interval),
  });
}
