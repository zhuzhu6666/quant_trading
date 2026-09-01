import { apiRequest } from "@/api/client";
import { readFact } from "@/api/fact";
import type { FactEnvelope } from "@/api/fact";
import type {
  AlertsView,
  IncidentControlView,
  OpsHealth,
  OpsLogSource,
  OpsLogTail,
  ReadinessDimension,
  ReadinessView,
  RecoveryView,
  SystemLoadView,
} from "@/types/contracts";
import {
  array,
  booleanValue,
  firstString,
  numberValue,
  object,
  stringList,
  stringValue,
} from "@/api/domains/shared";

function readinessDimension(name: string, value: unknown, blockers: unknown, fact: FactEnvelope): ReadinessDimension {
  const source = typeof value === "boolean" ? { ready: value } : object(value);
  const dimensionBlockers = stringList(blockers);
  const dimensionFact = readFact(source, "ops.backend-readiness.v2");
  return {
    name,
    ready: booleanValue(source, "ready") ?? booleanValue(source, "ok") ?? (typeof value === "boolean" ? value : null),
    reasonCode: firstString(source, "reason_code", "reason", "status") ?? dimensionBlockers[0] ?? dimensionFact.reason_code ?? fact.reason_code,
    observedAt: source.observed_at as string | number | null ?? dimensionFact.observed_at ?? fact.observed_at ?? fact.generated_at,
  };
}

export function decodeReadiness(payload: unknown): ReadinessView {
  const source = object(payload);
  const readiness = object(source.readiness);
  const dimensionsPayload = object(source.readiness_dimensions);
  const dimensionBlockers = object(dimensionsPayload.blockers);
  const fact = readFact(source, "ops.backend-readiness.v2");
  const dimensionSpecs: [string, string, string][] = [
    ["live execution", "ready_for_live_execution", "live_execution"],
    ["live alpha", "ready_for_live_alpha", "live_alpha"],
    ["autonomous mutation", "ready_for_autonomous_mutation", "autonomous_mutation"],
    ["release", "ready_for_release", "release"],
  ];
  const dimensions = [
    ...dimensionSpecs.map(([name, flag, legacy]) => {
      const value = dimensionsPayload[flag] ?? source[flag] ?? readiness[legacy];
      return [name, value, dimensionBlockers[flag] ?? dimensionBlockers[legacy]] as const;
    }),
  ].filter(([, value]) => value !== undefined).map(([name, value, blockers]) => readinessDimension(name, value, blockers, fact));
  const blockers = [
    ...stringList(source.blockers),
    ...dimensionSpecs.flatMap(([, flag, legacy]) => stringList(dimensionBlockers[flag] ?? dimensionBlockers[legacy])),
  ].filter((value, index, values) => values.indexOf(value) === index);
  return {
    fact,
    dimensions,
    blockers,
    readyForFrontend: booleanValue(source, "ready_for_frontend"),
    readyForLiveExecution: booleanValue(source, "ready_for_live_execution"),
    readyForLiveAlpha: booleanValue(source, "ready_for_live_alpha"),
    readyForAutonomousMutation: booleanValue(source, "ready_for_autonomous_mutation"),
    readyForRelease: booleanValue(source, "ready_for_release"),
  };
}

export const getReadinessView = () => apiRequest<unknown>("/api/ops/backend-readiness").then(decodeReadiness);

function decodeHealth(payload: unknown): OpsHealth {
  const source = object(payload);
  return {
    fact: readFact(source, "system.health.v2"),
    source: {
      status: stringValue(source, "status"),
      db: stringValue(source, "db"),
      ctrader: stringValue(source, "ctrader"),
      serverTime: stringValue(source, "server_time"),
      uptimeSeconds: numberValue(source, "uptime_seconds"),
    },
  };
}

export function decodeSystemLoad(payload: unknown): SystemLoadView {
  const source = object(payload);
  const cpu = object(source.cpu);
  const memory = object(source.memory);
  const disk = object(source.disk);
  return {
    ok: booleanValue(source, "ok"),
    observedAt: numberValue(source, "ts"),
    cpu: {
      percent: numberValue(cpu, "percent"),
      load1: numberValue(cpu, "load1"),
      load5: numberValue(cpu, "load5"),
      load15: numberValue(cpu, "load15"),
      cores: numberValue(cpu, "cores"),
    },
    memory: {
      percent: numberValue(memory, "percent"),
      totalBytes: numberValue(memory, "total_bytes"),
      availableBytes: numberValue(memory, "available_bytes"),
      usedBytes: numberValue(memory, "used_bytes"),
    },
    disk: {
      path: stringValue(disk, "path"),
      percent: numberValue(disk, "percent"),
      totalBytes: numberValue(disk, "total_bytes"),
      freeBytes: numberValue(disk, "free_bytes"),
      usedBytes: numberValue(disk, "used_bytes"),
    },
  };
}

function decodeRecovery(payload: unknown): RecoveryView {
  const source = object(payload);
  return {
    fact: readFact(source, "ops.auto-recovery.v2"),
    source: {
      status: stringValue(source, "status"),
      registered: booleanValue(source, "registered"),
      running: booleanValue(source, "running"),
      loopHealthy: booleanValue(source, "loop_healthy"),
      schedulerHealthy: booleanValue(source, "scheduler_healthy"),
      failures: numberValue(source, "failures"),
      lastCheck: numberValue(source, "last_check"),
      restartAttempts: numberValue(source, "restart_attempts"),
    },
  };
}

function decodeAlerts(payload: unknown): AlertsView {
  const source = object(payload);
  const delivery = object(source.delivery);
  return {
    fact: readFact(source, "ops.alerts.v2"),
    source: {
      status: stringValue(source, "status"),
      configStatus: stringValue(source, "config_status"),
      rulesActive: numberValue(source, "rules_active"),
      deliveryStatus: stringValue(delivery, "status"),
      deliveryRegistered: booleanValue(delivery, "registered"),
    },
  };
}

export function decodeLogTail(payload: unknown, defaultSource: OpsLogSource = "backend"): OpsLogTail {
  const source = object(payload);
  const rawSource = stringValue(source, "source");
  const resolvedSource: OpsLogSource = rawSource === "backend" || rawSource === "live_loop" || rawSource === "alerts" || rawSource === "debug"
    ? rawSource
    : defaultSource;
  const lines = array(source.lines).filter((line): line is string => typeof line === "string");
  return {
    source: resolvedSource,
    file: stringValue(source, "file"),
    lines: [...lines],
    total: numberValue(source, "total") ?? lines.length,
    sizeBytes: numberValue(source, "size_bytes"),
    observedAt: numberValue(source, "observed_at"),
  };
}

export const getHealth = () => apiRequest<unknown>("/api/health").then(decodeHealth);
export const getSystemLoad = () => apiRequest<unknown>("/api/system/load").then(decodeSystemLoad);
export const getLogTail = (source: OpsLogSource = "backend", lines = 240) => {
  const params = new URLSearchParams({ source, lines: String(lines) });
  return apiRequest<unknown>(`/api/logs/tail?${params.toString()}`).then((payload) => decodeLogTail(payload, source));
};
function decodeIncidentControl(payload: unknown): IncidentControlView {
  const source = object(payload);
  const status = object(source.incident_control);
  const latch = object(status.local_safety_latch ?? source.local_safety_latch);
  return {
    fact: readFact(source, "ops.incident-control.v2"),
    effectiveMode: firstString(status, "effective_mode", "mode") ?? firstString(source, "effective_mode", "mode"),
    configuredMode: firstString(status, "configured_mode", "mode") ?? stringValue(source, "configured_mode"),
    localSafetyLatch: booleanValue(status, "local_safety_latch") ?? booleanValue(source, "local_safety_latch") ?? booleanValue(latch, "active"),
  };
}
export const getIncidentControl = () => apiRequest<unknown>("/api/ops/incident-control").then(decodeIncidentControl);
export const getRecovery = () => apiRequest<unknown>("/api/ops/recovery").then(decodeRecovery);
export const getAlerts = () => apiRequest<unknown>("/api/ops/alerts").then(decodeAlerts);
