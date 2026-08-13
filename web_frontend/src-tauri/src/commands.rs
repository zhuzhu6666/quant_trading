use serde::{Deserialize, Serialize};
use std::fs;
use tauri::{AppHandle, Manager};

const UI_PREFERENCE_FILE: &str = "ui-preferences.json";

#[derive(Debug, Deserialize)]
pub struct UiPreferenceRequest {
    pub key: String,
    pub value: serde_json::Value,
}

#[derive(Debug, Serialize)]
pub struct UiPreferenceResponse {
    pub key: String,
    pub value: Option<serde_json::Value>,
}

fn preference_path(app: &AppHandle) -> Result<std::path::PathBuf, String> {
    let directory = app
        .path()
        .app_config_dir()
        .map_err(|error| format!("app config directory unavailable: {error}"))?;
    fs::create_dir_all(&directory).map_err(|error| format!("create preference directory: {error}"))?;
    Ok(directory.join(UI_PREFERENCE_FILE))
}

fn read_preferences(app: &AppHandle) -> Result<serde_json::Map<String, serde_json::Value>, String> {
    let path = preference_path(app)?;
    if !path.exists() {
        return Ok(serde_json::Map::new());
    }
    let content = fs::read_to_string(path).map_err(|error| format!("read preferences: {error}"))?;
    serde_json::from_str(&content).map_err(|error| format!("decode preferences: {error}"))
}

#[tauri::command]
pub fn save_ui_preference(app: AppHandle, request: UiPreferenceRequest) -> Result<(), String> {
    if request.key.trim().is_empty() || request.key.len() > 120 {
        return Err("invalid preference key".to_string());
    }
    let path = preference_path(&app)?;
    let mut preferences = read_preferences(&app)?;
    preferences.insert(request.key, request.value);
    let payload = serde_json::to_vec_pretty(&preferences)
        .map_err(|error| format!("encode preferences: {error}"))?;
    fs::write(path, payload).map_err(|error| format!("write preferences: {error}"))
}

#[tauri::command]
pub fn read_ui_preference(app: AppHandle, key: String) -> Result<UiPreferenceResponse, String> {
    let preferences = read_preferences(&app)?;
    Ok(UiPreferenceResponse {
        value: preferences.get(&key).cloned(),
        key,
    })
}

#[tauri::command]
pub fn clear_research_cache() -> Result<(), String> {
    // Research snapshots live in renderer-owned IndexedDB. This command is a
    // deliberately narrow acknowledgement so the desktop shell never gains
    // permission to inspect or delete server facts.
    Ok(())
}
