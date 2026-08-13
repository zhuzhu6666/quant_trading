import { useCallback, useEffect, useState } from "react";
import type { WorkspaceId } from "@/types/contracts";

export type LayoutState = {
  layout_version: 2;
  workspace_id: WorkspaceId;
  sidebar_collapsed: boolean;
  updated_at: string;
};

const STORAGE_KEY = "quant.ui.layout.v2";
const WORKSPACE_IDS: readonly WorkspaceId[] = ["trade-ops", "risk-desk", "research", "governance", "ops"];

export function defaultLayout(workspace: WorkspaceId): LayoutState {
  return {
    layout_version: 2,
    workspace_id: workspace,
    sidebar_collapsed: false,
    updated_at: new Date().toISOString(),
  };
}

function isObject(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function decodeLayout(value: unknown, workspace: WorkspaceId): LayoutState {
  const defaults = defaultLayout(workspace);
  if (!isObject(value)) return defaults;
  return {
    ...defaults,
    workspace_id: workspace,
    sidebar_collapsed: typeof value.sidebar_collapsed === "boolean" ? value.sidebar_collapsed : defaults.sidebar_collapsed,
    updated_at: typeof value.updated_at === "string" ? value.updated_at : defaults.updated_at,
  };
}

function readLayouts(): Partial<Record<WorkspaceId, LayoutState>> {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY);
    if (!raw) return {};
    const value = JSON.parse(raw) as unknown;
    if (!isObject(value)) return {};
    const layouts: Partial<Record<WorkspaceId, LayoutState>> = {};
    for (const workspace of WORKSPACE_IDS) {
      if (workspace in value) layouts[workspace] = decodeLayout(value[workspace], workspace);
    }
    return layouts;
  } catch {
    return {};
  }
}

function saveLayouts(layouts: Partial<Record<WorkspaceId, LayoutState>>): void {
  try {
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(layouts));
  } catch {
    // UI preferences are best effort and never affect facts or actions.
  }
}

export function useLayoutPreference(workspace: WorkspaceId) {
  const [layout, setLayout] = useState<LayoutState>(() => readLayouts()[workspace] ?? defaultLayout(workspace));

  useEffect(() => {
    setLayout(readLayouts()[workspace] ?? defaultLayout(workspace));
  }, [workspace]);

  const updateLayout = useCallback((patch: Partial<LayoutState>) => {
    setLayout((previous) => {
      const next = decodeLayout({ ...previous, ...patch }, workspace);
      next.updated_at = new Date().toISOString();
      const layouts = readLayouts();
      layouts[workspace] = next;
      saveLayouts(layouts);
      return next;
    });
  }, [workspace]);

  return { layout, updateLayout };
}
