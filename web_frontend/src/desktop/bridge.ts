import { invoke, isTauri } from "@tauri-apps/api/core";

export type DesktopDiagnostics = {
  platform: string;
  architecture: string;
  app_version: string;
  webview2: string;
};

export async function readDesktopDiagnostics(): Promise<DesktopDiagnostics | null> {
  try {
    return await invoke<DesktopDiagnostics>("get_desktop_diagnostics");
  } catch {
    return null;
  }
}

export async function clearDesktopResearchCache(): Promise<boolean> {
  try {
    await invoke("clear_research_cache");
    return true;
  } catch {
    return false;
  }
}

const REFRESH_MATERIAL_ACCOUNT = "active-session";

export async function storeRefreshMaterial(material: string): Promise<boolean> {
  if (!isTauri() || !material) return false;
  try {
    await invoke("set_refresh_material", { request: { account: REFRESH_MATERIAL_ACCOUNT, material } });
    return true;
  } catch {
    return false;
  }
}

export async function readRefreshMaterial(): Promise<string | null> {
  if (!isTauri()) return null;
  try {
    const result = await invoke<{ material: string | null }>("get_refresh_material", { account: REFRESH_MATERIAL_ACCOUNT });
    return result.material ?? null;
  } catch {
    return null;
  }
}

export async function deleteRefreshMaterial(): Promise<void> {
  if (!isTauri()) return;
  try {
    await invoke("delete_refresh_material", { account: REFRESH_MATERIAL_ACCOUNT });
  } catch {
    // Credential cleanup is best effort after the server revokes the session.
  }
}
