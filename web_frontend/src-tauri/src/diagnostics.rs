use serde::Serialize;

#[derive(Debug, Serialize)]
pub struct DesktopDiagnostics {
    pub platform: String,
    pub architecture: String,
    pub app_version: String,
    pub webview2: String,
}

#[tauri::command]
pub fn get_desktop_diagnostics() -> DesktopDiagnostics {
    DesktopDiagnostics {
        platform: std::env::consts::OS.to_string(),
        architecture: std::env::consts::ARCH.to_string(),
        app_version: env!("CARGO_PKG_VERSION").to_string(),
        webview2: std::env::var("WEBVIEW2_RUNTIME_VERSION").unwrap_or_else(|_| "runtime-managed".to_string()),
    }
}
