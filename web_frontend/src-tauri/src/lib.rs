mod commands;
mod diagnostics;
mod secure_store;

use commands::{clear_research_cache, read_ui_preference, save_ui_preference};
use diagnostics::get_desktop_diagnostics;
use secure_store::{delete_refresh_material, get_refresh_material, set_refresh_material};

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_process::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .invoke_handler(tauri::generate_handler![
            clear_research_cache,
            get_desktop_diagnostics,
            read_ui_preference,
            save_ui_preference,
            set_refresh_material,
            get_refresh_material,
            delete_refresh_material,
        ])
        .run(tauri::generate_context!())
        .expect("error while running quant workbench");
}
