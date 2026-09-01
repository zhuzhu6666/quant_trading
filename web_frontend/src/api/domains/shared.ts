import { factStateFromRaw } from "@/api/fact";

export type UnknownObject = { [key: string]: unknown };

export function object(value: unknown): UnknownObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as UnknownObject : {};
}

export function array(value: unknown): readonly unknown[] {
  return Array.isArray(value) ? value : [];
}

export function stringValue(source: UnknownObject, key: string): string | null {
  return typeof source[key] === "string" && source[key].trim() ? source[key] as string : null;
}

export function firstString(source: UnknownObject, ...keys: string[]): string | null {
  for (const key of keys) {
    const value = stringValue(source, key);
    if (value) return value;
  }
  return null;
}

export function identifierValue(source: UnknownObject, key: string): string | null {
  const value = source[key];
  if (typeof value === "string" && value.trim()) return value;
  return typeof value === "number" && Number.isFinite(value) ? String(value) : null;
}

export function numberValue(source: UnknownObject, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

export function stringOrNumberValue(source: UnknownObject, key: string): string | null {
  const value = source[key];
  if (typeof value === "string" && value.trim()) return value;
  return typeof value === "number" && Number.isFinite(value) ? String(value) : null;
}

export function numericValue(source: UnknownObject, key: string): number | null {
  const direct = numberValue(source, key);
  if (direct !== null) return direct;
  const value = source[key];
  if (typeof value !== "string" || !value.trim()) return null;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function booleanValue(source: UnknownObject, key: string): boolean | null {
  return typeof source[key] === "boolean" ? source[key] as boolean : null;
}

export function stringList(value: unknown): string[] {
  return array(value).flatMap((entry) => {
    if (typeof entry === "string" && entry.trim()) return [entry];
    const source = object(entry);
    return [firstString(source, "reason_code", "code", "blocker", "message")].filter((item): item is string => Boolean(item));
  });
}

export function timestampValue(source: UnknownObject): string | number | null {
  for (const key of ["observed_at", "updated_at", "created_at", "decision_ts", "catalog_ts", "lifecycle_updated_at", "health_updated_at", "last_action_ts", "ts"]) {
    const value = source[key];
    if ((typeof value === "string" && value.trim()) || (typeof value === "number" && Number.isFinite(value))) {
      return value as string | number;
    }
  }
  return null;
}

export function decisionBarTimestampValue(source: UnknownObject): string | number | null {
  for (const key of ["decision_bar_ts", "bar_ts", "bar_time", "signal_bar_ts", "decision_ts"]) {
    const value = source[key];
    if ((typeof value === "string" && value.trim()) || (typeof value === "number" && Number.isFinite(value))) {
      return value as string | number;
    }
  }
  return null;
}

export function arrayField(source: UnknownObject, key: string): readonly unknown[] {
  return array(source[key]);
}

export function failedFactPayload(contract: string, reasonCode: string): UnknownObject {
  return {
    _fact: {
      envelope: "fact.v1",
      contract,
      state: "error",
      source: "none",
      observed_at: null,
      generated_at: null,
      stale_after_sec: 0,
      reason_code: reasonCode,
      components: {},
    },
  };
}

export function factStatus(source: UnknownObject, key = "status") {
  return factStateFromRaw(source[key]);
}
