// ghosted — Tauri host. Spawns the Python engine / login capture as sidecars
// and relays JSON. Learn more: https://tauri.app/develop/calling-rust/

use std::io::{BufRead, BufReader, Write};
use std::process::{ChildStdin, Command, Stdio};
use std::sync::Mutex;
use serde_json::Value;
use tauri::ipc::Channel;
use tauri::Manager;
use tauri::menu::{AboutMetadataBuilder, MenuBuilder, SubmenuBuilder};

// ⬇️ Filled in with your machine's paths.
const VENV_PYTHON: &str =
    "/Users/ratantejmadan/CelestaraDynamics/ProjectCodebase/ghosted/venv/bin/python3";
const RUNDIR: &str =
    "/Users/ratantejmadan/CelestaraDynamics/ProjectCodebase/ghosted/rundir";

/// Holds the stdin of the currently-running purge engine so that
/// pause/resume/stop can write control commands to it mid-run.
#[derive(Default)]
struct EngineState {
    purge_stdin: Mutex<Option<ChildStdin>>,
}

#[tauri::command]
fn greet(name: &str) -> String {
    format!("Hello, {}! You've been greeted from Rust!", name)
}

/// One-shot: run the engine with a single command, return the first event
/// whose "event" field equals `want_event`. Closing stdin makes it exit.
fn engine_oneshot(cmd_json: &str, want_event: &str) -> Result<Value, String> {
    let mut child = Command::new(VENV_PYTHON)
        .arg("-u").arg("-m").arg("ghosted.engine")
        .current_dir(RUNDIR)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn()
        .map_err(|e| format!("failed to spawn engine: {e}"))?;
    {
        let stdin = child.stdin.as_mut().ok_or("no stdin handle")?;
        stdin.write_all(cmd_json.as_bytes())
            .and_then(|_| stdin.write_all(b"\n"))
            .map_err(|e| format!("write to engine failed: {e}"))?;
    }
    drop(child.stdin.take());
    let out = child.wait_with_output().map_err(|e| format!("waiting for engine failed: {e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    for line in stdout.lines() {
        if let Ok(v) = serde_json::from_str::<Value>(line.trim()) {
            if v.get("event").and_then(|e| e.as_str()) == Some(want_event) {
                return Ok(v);
            }
        }
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    Err(format!("no '{want_event}' event returned.\nengine stderr:\n{stderr}"))
}

#[tauri::command]
fn list_threads() -> Result<Value, String> {
    engine_oneshot("{\"cmd\":\"list_threads\"}", "threads")
}

#[tauri::command]
fn check_login() -> Result<Value, String> {
    engine_oneshot("{\"cmd\":\"login_status\"}", "login")
}

#[tauri::command]
fn schedules_list() -> Result<Value, String> {
    engine_oneshot("{\"cmd\":\"schedules_list\"}", "schedules")
}

#[tauri::command]
fn schedules_save(schedules: Value) -> Result<Value, String> {
    let params = serde_json::json!({ "schedules": schedules });
    let cmd = serde_json::json!({ "cmd": "schedules_save", "params": params }).to_string();
    engine_oneshot(&cmd, "schedules")
}

fn run_login_blocking() -> Result<Value, String> {
    let mut child = Command::new(VENV_PYTHON)
        .arg("-u").arg("-m").arg("ghosted.login_browser")
        .current_dir(RUNDIR)
        .stdout(Stdio::piped()).stderr(Stdio::piped())
        .spawn().map_err(|e| format!("failed to spawn login: {e}"))?;
    let out = child.wait_with_output().map_err(|e| format!("waiting for login failed: {e}"))?;
    let stdout = String::from_utf8_lossy(&out.stdout);
    for line in stdout.lines() {
        if let Ok(v) = serde_json::from_str::<Value>(line.trim()) {
            match v.get("event").and_then(|e| e.as_str()) {
                Some("login_success") | Some("login_saved") => return Ok(v),
                Some("login_failed") => {
                    let reason = v.get("reason").and_then(|r| r.as_str()).unwrap_or("unknown");
                    return Err(format!("login failed: {reason}"));
                }
                _ => {}
            }
        }
    }
    let stderr = String::from_utf8_lossy(&out.stderr);
    Err(format!("login produced no result.\nlogin stderr:\n{stderr}"))
}

#[tauri::command]
async fn login() -> Result<Value, String> {
    tauri::async_runtime::spawn_blocking(run_login_blocking)
        .await
        .map_err(|e| format!("login task join error: {e}"))?
}

/// Streaming index — keeps stdin open until the crawl finishes (see comment).
fn run_index_blocking(limit: u32, rebuild: bool, channel: Channel<Value>) -> Result<(), String> {
    let mut child = Command::new(VENV_PYTHON)
        .arg("-u").arg("-m").arg("ghosted.engine")
        .current_dir(RUNDIR)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
        .spawn().map_err(|e| format!("failed to spawn engine: {e}"))?;
    let mut stdin = child.stdin.take().ok_or("no stdin handle")?;
    let cmd = format!(
        "{{\"cmd\":\"index\",\"params\":{{\"limit\":{},\"rebuild_cache\":{}}}}}\n",
        limit, rebuild);
    stdin.write_all(cmd.as_bytes()).and_then(|_| stdin.flush())
        .map_err(|e| format!("write to engine failed: {e}"))?;
    let stdout = child.stdout.take().ok_or("no stdout handle")?;
    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|e| format!("read failed: {e}"))?;
        let line = line.trim();
        if line.is_empty() { continue; }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            let kind = v.get("event").and_then(|e| e.as_str()).unwrap_or("").to_string();
            let _ = channel.send(v);
            if kind == "index_complete" || kind == "index_stopped" { break; }
        }
    }
    drop(stdin);
    let _ = child.wait();
    Ok(())
}

#[tauri::command]
async fn index(limit: u32, rebuild: bool, channel: Channel<Value>) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || run_index_blocking(limit, rebuild, channel))
        .await
        .map_err(|e| format!("index task join error: {e}"))?
}

/// Streaming Deep Scan: fetch message histories for the selected threads,
/// building each one's cache and emitting its counts. Same keep-stdin-open
/// pattern as index (the crawl runs on the engine's worker thread).
fn run_scan_blocking(thread_ids: Vec<String>, rebuild: bool, channel: Channel<Value>) -> Result<(), String> {
    let mut child = Command::new(VENV_PYTHON)
        .arg("-u").arg("-m").arg("ghosted.engine")
        .current_dir(RUNDIR)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
        .spawn().map_err(|e| format!("failed to spawn engine: {e}"))?;
    let mut stdin = child.stdin.take().ok_or("no stdin handle")?;
    let params = serde_json::json!({ "thread_ids": thread_ids, "rebuild_cache": rebuild });
    let cmd = serde_json::json!({ "cmd": "scan", "params": params }).to_string();
    stdin.write_all(cmd.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|e| format!("write to engine failed: {e}"))?;
    let stdout = child.stdout.take().ok_or("no stdout handle")?;
    for line in BufReader::new(stdout).lines() {
        let line = line.map_err(|e| format!("read failed: {e}"))?;
        let line = line.trim();
        if line.is_empty() { continue; }
        if let Ok(v) = serde_json::from_str::<Value>(line) {
            let kind = v.get("event").and_then(|e| e.as_str()).unwrap_or("").to_string();
            let _ = channel.send(v);
            if kind == "scan_complete" || kind == "scan_stopped" { break; }
        }
    }
    drop(stdin);
    let _ = child.wait();
    Ok(())
}

#[tauri::command]
async fn scan(thread_ids: Vec<String>, rebuild: bool, channel: Channel<Value>) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || run_scan_blocking(thread_ids, rebuild, channel))
        .await
        .map_err(|e| format!("scan task join error: {e}"))?
}

/// Streaming purge with live controls. Spawns the engine, stashes its stdin in
/// shared state (so pause/resume/stop can reach it), sends the purge command,
/// and forwards every event to the frontend until the job ends.
#[allow(clippy::too_many_arguments)]
#[tauri::command]
async fn purge(
    thread_ids: Vec<String>,
    min_delay: f64,
    max_delay: f64,
    batch_size: u32,
    pause_between_batches: f64,
    max_deletes: u32,
    max_per_hour: u32,
    rebuild: bool,
    channel: Channel<Value>,
    app: tauri::AppHandle,
) -> Result<(), String> {
    let mut child = Command::new(VENV_PYTHON)
        .arg("-u").arg("-m").arg("ghosted.engine")
        .current_dir(RUNDIR)
        .stdin(Stdio::piped()).stdout(Stdio::piped()).stderr(Stdio::inherit())
        .spawn()
        .map_err(|e| format!("failed to spawn engine: {e}"))?;

    let mut stdin = child.stdin.take().ok_or("no stdin handle")?;
    let params = serde_json::json!({
        "thread_ids": thread_ids,
        "min_delay": min_delay,
        "max_delay": max_delay,
        "batch_size": batch_size,
        "pause_between_batches": pause_between_batches,
        "max_deletes": max_deletes,
        "max_per_hour": max_per_hour,
        "rebuild_cache": rebuild,
    });
    let cmd = serde_json::json!({ "cmd": "purge", "params": params }).to_string();
    stdin.write_all(cmd.as_bytes())
        .and_then(|_| stdin.write_all(b"\n"))
        .and_then(|_| stdin.flush())
        .map_err(|e| format!("write to engine failed: {e}"))?;

    // Stash stdin so purge_control can write pause/resume/stop into it.
    {
        let state = app.state::<EngineState>();
        *state.purge_stdin.lock().unwrap() = Some(stdin);
    }

    // Read events to completion on a blocking thread.
    let stdout = child.stdout.take().ok_or("no stdout handle")?;
    let read = tauri::async_runtime::spawn_blocking(move || {
        for line in BufReader::new(stdout).lines() {
            let line = match line { Ok(l) => l, Err(_) => break };
            let line = line.trim();
            if line.is_empty() { continue; }
            if let Ok(v) = serde_json::from_str::<Value>(line) {
                let kind = v.get("event").and_then(|e| e.as_str()).unwrap_or("").to_string();
                let _ = channel.send(v);
                if matches!(kind.as_str(),
                    "job_done" | "job_stopped" | "job_capped" | "session_expired") {
                    break;
                }
            }
        }
    }).await;

    // Clear + close stdin (dropping it) so the engine's main loop exits.
    {
        let state = app.state::<EngineState>();
        *state.purge_stdin.lock().unwrap() = None;
    }
    let _ = child.wait();
    read.map_err(|e| format!("purge read task join error: {e}"))?;
    Ok(())
}

/// Send a control command ("pause" | "resume" | "stop") to the running purge.
#[tauri::command]
fn purge_control(action: String, state: tauri::State<'_, EngineState>) -> Result<(), String> {
    let mut guard = state.purge_stdin.lock().map_err(|_| "state lock poisoned")?;
    match guard.as_mut() {
        Some(stdin) => {
            let cmd = format!("{{\"cmd\":\"{}\"}}\n", action);
            stdin.write_all(cmd.as_bytes())
                .and_then(|_| stdin.flush())
                .map_err(|e| format!("write control failed: {e}"))
        }
        None => Err("no active purge to control".into()),
    }
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(EngineState::default())
        .plugin(tauri_plugin_opener::init())
        .setup(|app| {
            let handle = app.handle().clone();

            // The "About ghosted" panel: name, version, license, and links.
            let about = AboutMetadataBuilder::new()
                .name(Some("ghosted"))
                .version(Some(env!("CARGO_PKG_VERSION")))
                .copyright(Some("\u{00A9} 2026 Celestara Dynamics"))
                .license(Some("GPL-3.0"))
                .website(Some("https://github.com/ratantejmadan/ghosted"))
                .website_label(Some("GitHub"))
                .comments(Some("Bulk-unsend your Instagram DMs."))
                // credits renders in the macOS About panel, so put the
                // license + link here too to guarantee they're visible.
                .credits(Some(
                    "Bulk-unsend your Instagram DMs.\n\nLicense: GPL-3.0\n\ngithub.com/ratantejmadan/ghosted",
                ))
                .build();

            // First submenu becomes the bold app menu on macOS.
            let app_menu = SubmenuBuilder::new(&handle, "ghosted")
                .about(Some(about))
                .separator()
                .services()
                .separator()
                .hide()
                .hide_others()
                .show_all()
                .separator()
                .quit()
                .build()?;

            // Keep a standard Edit menu so copy/paste/undo work in inputs.
            let edit_menu = SubmenuBuilder::new(&handle, "Edit")
                .undo()
                .redo()
                .separator()
                .cut()
                .copy()
                .paste()
                .select_all()
                .build()?;

            let window_menu = SubmenuBuilder::new(&handle, "Window")
                .minimize()
                .close_window()
                .build()?;

            let menu = MenuBuilder::new(&handle)
                .item(&app_menu)
                .item(&edit_menu)
                .item(&window_menu)
                .build()?;

            app.set_menu(menu)?;
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            greet,
            list_threads,
            check_login,
            schedules_list,
            schedules_save,
            login,
            index,
            scan,
            purge,
            purge_control
        ])
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}