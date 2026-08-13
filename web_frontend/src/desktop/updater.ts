import { isTauri } from "@tauri-apps/api/core";

export type DesktopUpdateInfo = {
  available: boolean;
  currentVersion: string | null;
  version: string | null;
  date: string | null;
  body: string | null;
};

export type DesktopUpdateProgress = {
  phase: "started" | "progress" | "finished";
  downloadedBytes: number;
  contentLength: number | null;
};

export async function checkForDesktopUpdate(): Promise<DesktopUpdateInfo> {
  if (!isTauri()) {
    return { available: false, currentVersion: null, version: null, date: null, body: null };
  }
  const { check } = await import("@tauri-apps/plugin-updater");
  const update = await check();
  if (!update) {
    return { available: false, currentVersion: null, version: null, date: null, body: null };
  }
  return {
    available: true,
    currentVersion: update.currentVersion,
    version: update.version,
    date: update.date ?? null,
    body: update.body ?? null,
  };
}

export async function installDesktopUpdate(
  onProgress?: (progress: DesktopUpdateProgress) => void,
): Promise<void> {
  if (!isTauri()) throw new Error("desktop_updater_unavailable");
  const [{ check }, { relaunch }] = await Promise.all([
    import("@tauri-apps/plugin-updater"),
    import("@tauri-apps/plugin-process"),
  ]);
  const update = await check();
  if (!update) throw new Error("desktop_update_not_available");
  let downloadedBytes = 0;
  await update.downloadAndInstall((event) => {
    if (event.event === "Started") {
      onProgress?.({
        phase: "started",
        downloadedBytes,
        contentLength: event.data.contentLength ?? null,
      });
    } else if (event.event === "Progress") {
      downloadedBytes += event.data.chunkLength;
      onProgress?.({ phase: "progress", downloadedBytes, contentLength: null });
    } else {
      onProgress?.({ phase: "finished", downloadedBytes, contentLength: null });
    }
  });
  await relaunch();
}
