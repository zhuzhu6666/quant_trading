use serde::{Deserialize, Serialize};

const SERVICE_NAME: &str = "quant-trading-workbench";

#[derive(Debug, Deserialize)]
pub struct RefreshMaterialRequest {
    pub account: String,
    pub material: String,
}

#[derive(Debug, Serialize)]
pub struct RefreshMaterialResponse {
    pub account: String,
    pub material: Option<String>,
}

fn entry(account: &str) -> Result<keyring::Entry, String> {
    if account.trim().is_empty() || account.len() > 160 {
        return Err("invalid credential account".to_string());
    }
    keyring::Entry::new(SERVICE_NAME, account).map_err(|error| format!("credential entry: {error}"))
}

#[tauri::command]
pub fn set_refresh_material(request: RefreshMaterialRequest) -> Result<(), String> {
    if request.material.is_empty() || request.material.len() > 16_384 {
        return Err("invalid refresh material".to_string());
    }
    entry(&request.account)?.set_password(&request.material).map_err(|error| format!("credential write: {error}"))
}

#[tauri::command]
pub fn get_refresh_material(account: String) -> Result<RefreshMaterialResponse, String> {
    let material = match entry(&account)?.get_password() {
        Ok(value) => Some(value),
        Err(keyring::Error::NoEntry) => None,
        Err(error) => return Err(format!("credential read: {error}")),
    };
    Ok(RefreshMaterialResponse { account, material })
}

#[tauri::command]
pub fn delete_refresh_material(account: String) -> Result<(), String> {
    match entry(&account)?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(error) => Err(format!("credential delete: {error}")),
    }
}
