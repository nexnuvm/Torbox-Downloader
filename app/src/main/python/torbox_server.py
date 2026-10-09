import os
import sys
import json
import time
import sqlite3
import subprocess
import threading
import traceback
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen
import urllib.error

HOST = "127.0.0.1"  # localhost only -- the WebView is the sole client now, no LAN exposure
PORT = 8080
RPC_PORT = 6800
RPC_URL = f"http://127.0.0.1:{RPC_PORT}/jsonrpc"

# DOWNLOAD_DIR, DB_PATH and ARIA2C_PATH have no meaningful equivalent of
# Termux's home directory on Android -- they're injected from Kotlin via
# set_download_dir() / set_db_path() / set_aria2c_path() (see
# DownloadService.kt) before start_server() is called, rather than being
# hardcoded module constants.
DOWNLOAD_DIR = None
DB_PATH = None
ARIA2C_PATH = "aria2c"  # overwritten by set_aria2c_path(); this default only
                         # matters if the app is ever run outside Android.

# Set by set_bridge() -- see the "ANDROID BRIDGE" section below. Replaces
# every termux-wake-lock / termux-notification / termux-wifi-connectioninfo
# subprocess call with a direct, same-process method call on this object.
_bridge = None

# Tracks the currently-running aria2c Popen handle (if any), guarded by
# db_lock since it's read/written from both the worker thread and HTTP
# handler threads (cancel/clear requests). This replaces the Termux
# version's `pkill -f rpc-listen-port=...` approach: Android has no `pkill`
# binary available by default, and tracking the exact process object we
# launched is more precise anyway -- a command-line substring match could in
# principle hit an unrelated process sharing that RPC port string.
_active_proc = None
_proc_lock = threading.Lock()  # separate from db_lock -- this guards process
                                # state, not SQLite access, and the two
                                # shouldn't block on each other

def _set_active_proc(proc):
    global _active_proc
    with _proc_lock:
        _active_proc = proc

def _clear_active_proc():
    global _active_proc
    with _proc_lock:
        _active_proc = None

def _get_active_proc():
    with _proc_lock:
        return _active_proc

live_status = {
    "active": False,
    "paused": False,
    "current_file": "",
    "current_item_id": None,
    "download_speed": "0 B/s",
    "progress": 0,
    "completed_size": "0 B",
    "total_size": "0 B",
    "eta": "--",
    "log": []
}

db_lock = threading.Lock()

# ==============================================================================
# --- DATABASE PERSISTENCE ENGINE ---
# ==============================================================================

def init_db():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute('''
                    CREATE TABLE IF NOT EXISTS queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        api_key TEXT,
                        dl_type TEXT DEFAULT 'torrents',
                        torrent_id TEXT,
                        file_id TEXT,
                        output_name TEXT,
                        expected_size INTEGER DEFAULT 0,
                        status TEXT DEFAULT 'pending',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                ''')

                c.execute("PRAGMA table_info(queue)")
                columns = [col[1] for col in c.fetchall()]
                if "expected_size" not in columns:
                    c.execute("ALTER TABLE queue ADD COLUMN expected_size INTEGER DEFAULT 0")
                if "dl_type" not in columns:
                    c.execute("ALTER TABLE queue ADD COLUMN dl_type TEXT DEFAULT 'torrents'")

                c.execute('''
                    CREATE TABLE IF NOT EXISTS system_state (
                        key TEXT PRIMARY KEY,
                        value TEXT
                    )
                ''')
                c.execute("INSERT OR IGNORE INTO system_state (key, value) VALUES ('paused', 'false')")
        finally:
            conn.close()

def get_state(key, default="false"):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT value FROM system_state WHERE key=?", (key,))
            row = c.fetchone()
            return row[0] if row else default
        finally:
            conn.close()

def set_state(key, value):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("REPLACE INTO system_state (key, value) VALUES (?, ?)", (key, str(value)))
        finally:
            conn.close()

def db_add_items(items):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                for item in items:
                    safe_name = os.path.basename(item['output_name'])
                    dl_type = item.get('dl_type', 'torrents')
                    c.execute('''
                        INSERT INTO queue (api_key, dl_type, torrent_id, file_id, output_name, expected_size, status)
                        VALUES (?, ?, ?, ?, ?, ?, 'pending')
                    ''', (
                        item['api_key'], dl_type, item['torrent_id'], item.get('file_id', ''),
                        safe_name, int(item.get('expected_size', 0) or 0)
                    ))
        finally:
            conn.close()

def db_get_next_item():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute('''SELECT id, api_key, dl_type, torrent_id, file_id, output_name, expected_size
                             FROM queue WHERE status='pending' ORDER BY id ASC LIMIT 1''')
                row = c.fetchone()
                if row:
                    c.execute("UPDATE queue SET status='downloading' WHERE id=?", (row[0],))
                    return {
                        "id": row[0], "api_key": row[1], "dl_type": row[2] or "torrents",
                        "torrent_id": row[3], "file_id": row[4], "output_name": row[5],
                        "expected_size": row[6]
                    }
                return None
        finally:
            conn.close()

def db_update_status(item_id, status):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("UPDATE queue SET status=? WHERE id=?", (status, item_id))
        finally:
            conn.close()

def kill_active_aria2():
    proc = _get_active_proc()
    if proc and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        except Exception:
            pass
    _clear_active_proc()

    current_file = live_status.get("current_file")
    if current_file:
        aria_file = os.path.join(DOWNLOAD_DIR, current_file + ".aria2")
        if os.path.exists(aria_file):
            try:
                os.remove(aria_file)
            except OSError:
                pass

    live_status["active"] = False
    live_status["current_file"] = ""
    live_status["current_item_id"] = None
    live_status["progress"] = 0
    live_status["download_speed"] = "0 B/s"
    live_status["eta"] = "--"

def db_clear_queue():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("DELETE FROM queue WHERE status='pending' OR status='downloading'")
        finally:
            conn.close()

    kill_active_aria2()

def db_get_queue_summary():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT count(*) FROM queue WHERE status='pending'")
            pending = c.fetchone()[0]
            c.execute("SELECT count(*) FROM queue WHERE status='completed'")
            completed = c.fetchone()[0]
            return pending, completed
        finally:
            conn.close()

def db_remove_item(item_id):
    active_cancelled = False
    if live_status.get("current_item_id") == item_id:
        kill_active_aria2()
        active_cancelled = True

    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("DELETE FROM queue WHERE id=?", (item_id,))
                return c.rowcount > 0 or active_cancelled
        finally:
            conn.close()

def db_requeue_failed_item(item_id):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("UPDATE queue SET status='pending' WHERE id=? AND status LIKE 'failed%'", (item_id,))
                return c.rowcount > 0
        finally:
            conn.close()

def db_get_pending_list():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute('''SELECT id, output_name, torrent_id, dl_type, created_at FROM queue
                         WHERE status='pending' ORDER BY id ASC''')
            rows = c.fetchall()
            return [{"id": r[0], "output_name": r[1], "torrent_id": r[2], "dl_type": r[3], "created_at": r[4]} for r in rows]
        finally:
            conn.close()

def db_get_history_list(limit=25):
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute('''SELECT id, output_name, status, created_at FROM queue
                         WHERE status='completed' OR status LIKE 'failed%'
                         ORDER BY id DESC LIMIT ?''', (limit,))
            rows = c.fetchall()
            return [{"id": r[0], "output_name": r[1], "status": r[2], "created_at": r[3]} for r in rows]
        finally:
            conn.close()

def db_get_stuck_downloading_items():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            c = conn.cursor()
            c.execute("SELECT id, output_name FROM queue WHERE status='downloading'")
            return c.fetchall()
        finally:
            conn.close()

def db_reset_stuck_downloading_items():
    with db_lock:
        conn = sqlite3.connect(DB_PATH)
        try:
            with conn:
                c = conn.cursor()
                c.execute("UPDATE queue SET status='pending' WHERE status='downloading'")
        finally:
            conn.close()


# ==============================================================================
# --- SYSTEM UTILITIES ---
# ==============================================================================

def log(msg):
    live_status["log"].append(msg)
    if len(live_status["log"]) > 100:
        live_status["log"].pop(0)
    print(f"[TorBox] {msg}")

def check_wifi():
    """Delegates to AndroidBridge.isOnWifi() -- a direct ConnectivityManager
    call, same process, no subprocess involved at all. This entirely removes
    the class of bug the Termux version had here: there is no companion
    process for Android's battery optimizer to suspend, and no Location
    permission requirement (termux-wifi-connectioninfo needed Location to be
    on; ConnectivityManager does not)."""
    if _bridge is None:
        return True  # bridge not wired up (e.g. running outside Android) -- don't block
    try:
        return bool(_bridge.isOnWifi())
    except Exception as e:
        log(f"[!] Wi-Fi check failed: {e}")
        return False

def send_notification(title, content, action_file=None):
    """Delegates to AndroidBridge.sendNotification(). action_file is kept in
    the signature for call-site compatibility with the Termux version but is
    currently unused -- see the "open file on tap" TODO in AndroidBridge.kt
    if you want that back."""
    if _bridge is None:
        return
    try:
        _bridge.sendNotification(title, content)
    except Exception as e:
        log(f"[!] Notification failed: {e}")

def acquire_wake_lock():
    """Direct replacement for the old termux-wake-lock subprocess call --
    delegates to AndroidBridge's PowerManager.WakeLock. Same-process call,
    cannot hang the way the Termux companion-process version could."""
    if _bridge is None:
        return
    try:
        _bridge.acquireWakeLock()
    except Exception as e:
        log(f"[!] Wake lock acquire failed: {e}")

def release_wake_lock():
    if _bridge is None:
        return
    try:
        _bridge.releaseWakeLock()
    except Exception as e:
        log(f"[!] Wake lock release failed: {e}")

def format_bytes(bytes_num):
    bytes_num = float(bytes_num)
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes_num < 1024.0:
            return f"{bytes_num:.2f} {unit}"
        bytes_num /= 1024.0
    return f"{bytes_num:.2f} PB"

def rpc_call(method, params=None):
    payload = {"jsonrpc": "2.0", "id": "torbox", "method": method, "params": params or []}
    req = Request(RPC_URL, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=3) as response:
            return json.loads(response.read().decode("utf-8"))
    except Exception:
        return None

def run_startup_integrity_check():
    stuck = db_get_stuck_downloading_items()
    if stuck:
        db_reset_stuck_downloading_items()
        for _item_id, output_name in stuck:
            log(f"[integrity] Requeued download interrupted by a previous shutdown: '{output_name}'")

    for item in db_get_pending_list():
        output_name = item["output_name"]
        out_file = os.path.join(DOWNLOAD_DIR, output_name)
        aria_file = out_file + ".aria2"

        if os.path.exists(aria_file) and not os.path.exists(out_file):
            try:
                os.remove(aria_file)
                log(f"[integrity] Removed orphaned .aria2 control file for '{output_name}'.")
            except OSError as e:
                log(f"[integrity] Could not remove orphaned .aria2 file for '{output_name}': {e}")


# ==============================================================================
# --- DOWNLOAD WORKER ENGINE ---
# ==============================================================================

def process_download_item(item, max_retries=50):
    item_id = item["id"]
    api_key = item["api_key"]
    dl_type = item.get("dl_type", "torrents")
    torrent_id = item["torrent_id"]
    file_id = str(item.get("file_id") or "")
    output_name = os.path.basename(item["output_name"])
    expected_size = item.get("expected_size", 0)

    live_status["current_file"] = output_name
    live_status["current_item_id"] = item_id
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)
    out_file = os.path.join(DOWNLOAD_DIR, output_name)
    aria_file = out_file + ".aria2"

    if os.path.exists(out_file) and not os.path.exists(aria_file):
        size_ok = True
        if expected_size:
            try:
                size_ok = os.path.getsize(out_file) == expected_size
            except OSError:
                size_ok = False
        if size_ok:
            log(f"SKIP: '{output_name}' is already fully downloaded.")
            db_update_status(item_id, "completed")
            send_notification("Download Skipped", f"Already complete: {output_name}", out_file)
            return True
        else:
            log(f"[!] '{output_name}' exists but size mismatch -- re-downloading.")

    acquire_wake_lock()
    send_notification("TorBox Downloader Active", f"Processing: {output_name}")

    retry_count = 0

    while retry_count < max_retries:
        if get_state("paused") == "true":
            log("Queue is PAUSED by user. Waiting...")
            send_notification("TorBox Queue Paused", "Downloads are paused.")
            while get_state("paused") == "true":
                time.sleep(3)
            log("Queue RESUMED by user.")

        if not check_wifi():
            log("[!] Wi-Fi disconnected. Pausing and retrying in 20s...")
            send_notification("Download Paused", f"Waiting for Wi-Fi: {output_name}")
            for _ in range(20):
                if get_state("paused") == "true": break
                time.sleep(1)
            continue

        log(f"Fetching fresh link for '{output_name}' ({dl_type.upper()} Attempt {retry_count + 1}/{max_retries})...")

        if dl_type == "webdl":
            request_url = f"https://api.torbox.app/v1/api/webdl/requestdl?token={api_key}&web_id={torrent_id}"
            if file_id and file_id not in ("undefined", "null", "None", ""):
                request_url += f"&file_id={file_id}"
        else:
            request_url = f"https://api.torbox.app/v1/api/torrents/requestdl?token={api_key}&torrent_id={torrent_id}&file_id={file_id}"

        # urllib replaces curl entirely here -- no external binary, no
        # Python-level timeout needed to backstop a hang in curl itself, and
        # HTTPError/URLError give us the status code and body directly
        # instead of needing to text-split a combined header+body blob.
        success, fresh_url, detail = False, "", "Unknown API error"
        body_clean = ""
        status_line = "HTTP Error"
        try:
            req = Request(request_url, headers={"User-Agent": "TorBoxDownloader-Android/1.0"})
            with urlopen(req, timeout=15) as response:
                body_clean = response.read().decode("utf-8", errors="replace").strip()
                status_line = f"HTTP {response.status}"
        except urllib.error.HTTPError as e:
            body_clean = e.read().decode("utf-8", errors="replace").strip()
            status_line = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            log(f"[!] Network error fetching link for '{output_name}': {e.reason}. Retrying in 15s...")
            retry_count += 1
            for _ in range(15):
                if get_state("paused") == "true": break
                time.sleep(1)
            continue
        except Exception as e:
            log(f"[!] Unexpected error fetching link for '{output_name}': {e}. Retrying in 15s...")
            retry_count += 1
            for _ in range(15):
                if get_state("paused") == "true": break
                time.sleep(1)
            continue

        if body_clean.startswith("http://") or body_clean.startswith("https://"):
            success = True
            fresh_url = body_clean
        else:
            try:
                resp_json = json.loads(body_clean)
                success = resp_json.get("success", False)
                data = resp_json.get("data", "")
                # "data" can be a plain URL string OR a dict like {"link": "..."} /
                # {"url": "..."} depending on endpoint. Checking isinstance() here
                # (rather than "if not data") is required: a *populated* dict is
                # truthy, so a falsiness check alone would never unwrap it, leaving
                # fresh_url as a dict object that later crashes subprocess.Popen()
                # and silently kills the whole background worker thread.
                if isinstance(data, dict):
                    fresh_url = data.get("link") or data.get("url") or ""
                elif isinstance(data, str):
                    fresh_url = data
                else:
                    fresh_url = ""
                detail = resp_json.get("detail") or resp_json.get("message") or resp_json.get("error") or detail
            except Exception:
                detail = f"{status_line} -> Response: '{body_clean[:80]}'"

        # Defense in depth: even if the parsing above has a gap we haven't
        # anticipated, never let a non-string fresh_url reach aria2c/Popen.
        if fresh_url and not isinstance(fresh_url, str):
            detail = f"Unexpected non-string download URL from TorBox API: {type(fresh_url).__name__}"
            fresh_url = ""
            success = False

        if not success or not fresh_url:
            log(f"[!] TorBox API Error: {detail}. Retrying in 30s...")
            retry_count += 1
            for _ in range(30):
                if get_state("paused") == "true": break
                time.sleep(1)
            continue

        if not check_wifi():
            continue

        log(f"Starting aria2c download: {output_name}")
        aria_cmd = [
            ARIA2C_PATH, "--enable-rpc=true", f"--rpc-listen-port={RPC_PORT}",
            "-c", "-x8", "-s8", "--min-split-size=10M", "--disk-cache=64M",
            "--file-allocation=none", "--timeout=20", "--connect-timeout=15",
            "--lowest-speed-limit=5K", "--async-dns=true", "--max-tries=10", "--retry-wait=5",
            "-o", output_name, "-d", DOWNLOAD_DIR, fresh_url
        ]

        try:
            proc = subprocess.Popen(aria_cmd)
            _set_active_proc(proc)  # tracked so kill_active_aria2() can terminate it directly (see below)
        except FileNotFoundError:
            log(f"[!] FATAL: aria2c binary not found at '{ARIA2C_PATH}'. "
                f"Check set_aria2c_path() was called with the correct jniLibs path.")
            db_update_status(item_id, "failed_no_aria2c")
            release_wake_lock()
            return False
        except Exception as e:
            # Catches anything else that could prevent aria2c from launching
            # (e.g. a malformed argument) instead of letting it propagate and
            # kill the worker thread.
            log(f"[!] FATAL: Could not start aria2c for '{output_name}': {e}")
            db_update_status(item_id, "failed_aria2c_launch")
            release_wake_lock()
            return False

        time.sleep(1.5)

        active_res = rpc_call("aria2.tellActive")
        gid = active_res["result"][0]["gid"] if (active_res and "result" in active_res and len(active_res["result"]) > 0) else None

        wifi_dropped = False
        notify_counter = 0
        gid_retry_counter = 0
        aria_task_status = None   # set to "complete" / "error" / "removed" once aria2 reports a terminal state
        aria_error_msg = ""

        # IMPORTANT: aria2c is launched with --enable-rpc=true, which makes it
        # run as a persistent RPC *server* -- it does NOT exit on its own once
        # its one queued download finishes. It just stays alive holding the
        # RPC port open. That means proc.poll() alone can NEVER be trusted as
        # a "download finished" signal: using it as the sole loop condition
        # (as this used to) made the loop run forever after every successful
        # download, silently freezing the whole queue. Completion is instead
        # detected from aria2's own per-task "status" field below, and we then
        # explicitly shut the RPC server down ourselves.
        while True:
            if proc.poll() is not None:
                # aria2c exited unexpectedly on its own (crash, OOM-kill, a
                # host that forcibly closes it, etc.) before we ever observed
                # a terminal status via RPC -- fall back to its exit code.
                log(f"[debug] aria2c process exited on its own (poll={proc.poll()}) before a terminal "
                    f"RPC status was observed for '{output_name}'.")
                break

            if live_status.get("current_item_id") != item_id:
                log(f"Download for '{output_name}' was cancelled by user.")
                if gid: rpc_call("aria2.pause", [gid])
                proc.terminate()
                proc.wait()
                _clear_active_proc()
                release_wake_lock()
                return False

            if get_state("paused") == "true":
                log("Pause requested. Stopping active aria2c instance...")
                if gid: rpc_call("aria2.pause", [gid])
                proc.terminate()
                proc.wait()
                break

            if not check_wifi():
                log("[!] Wi-Fi dropped mid-download. Stopping aria2c...")
                send_notification("Download Paused", f"Wi-Fi dropped: {output_name}")
                if gid: rpc_call("aria2.pause", [gid])
                proc.terminate()
                proc.wait()
                wifi_dropped = True
                break

            if not gid:
                # The initial aria2.tellActive right after launch can miss the
                # gid if aria2c's RPC server was still starting up. Without a
                # gid we have no way to see a terminal status at all, which
                # would silently reproduce this exact bug -- so keep retrying
                # to pick it up rather than relying on proc.poll() forever.
                gid_retry_counter += 1
                if gid_retry_counter % 3 == 0:
                    active_res = rpc_call("aria2.tellActive")
                    gid = active_res["result"][0]["gid"] if (active_res and "result" in active_res and len(active_res["result"]) > 0) else None

            if gid:
                status_res = rpc_call("aria2.tellStatus", [gid])
                if status_res and "result" in status_res:
                    st = status_res["result"]
                    aria_state = st.get("status", "")
                    completed = int(st.get("completedLength", 0))
                    total = int(st.get("totalLength", 0))
                    speed = int(st.get("downloadSpeed", 0))

                    live_status["completed_size"] = format_bytes(completed)
                    live_status["total_size"] = format_bytes(total)
                    live_status["download_speed"] = f"{format_bytes(speed)}/s"

                    if total > 0:
                        pct = round((completed / total) * 100, 1)
                        live_status["progress"] = pct
                        rem_bytes = total - completed
                        eta_sec = int(rem_bytes / speed) if speed > 0 else 0
                        live_status["eta"] = f"{eta_sec // 60}m {eta_sec % 60}s" if eta_sec > 0 else "Calculating..."

                        notify_counter += 1
                        if notify_counter % 5 == 0:
                            send_notification("Downloading TorBox File", f"{output_name} ({pct}% - {live_status['download_speed']})")

                    if aria_state in ("complete", "error", "removed"):
                        aria_task_status = aria_state
                        aria_error_msg = st.get("errorMessage", "")
                        log(f"[debug] aria2 reports task '{aria_state}' for '{output_name}'. Shutting down aria2c RPC server...")
                        rpc_call("aria2.shutdown")
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                            proc.wait()
                        break

            time.sleep(1)

        log(f"[debug] aria2c process for '{output_name}' has exited (poll={proc.poll()}), "
            f"aria_task_status={aria_task_status}.")
        _clear_active_proc()  # covers every remaining exit path from the loop above

        if wifi_dropped or get_state("paused") == "true":
            continue

        # Success is now determined from aria2's own reported task status
        # (the reliable signal, since we may be the ones who just killed the
        # process after observing "complete" -- its exit code at that point
        # reflects termination-by-signal, not success). Only fall back to the
        # process's own exit code in the rare case it exited on its own
        # without us ever seeing a terminal RPC status.
        if aria_task_status == "complete" or (aria_task_status is None and proc.returncode == 0):
            log(f"[debug] checking for leftover .aria2 control file...")
            if os.path.exists(aria_file):
                log(f"[debug] removing leftover .aria2 control file...")
                try:
                    os.remove(aria_file)
                except OSError as e:
                    # aria2c itself deletes this file on success too, so a
                    # race here (file vanishes between the exists() check and
                    # remove()) is expected and harmless -- just don't let it
                    # raise and get caught by the outer safety net instead.
                    log(f"[debug] .aria2 control file already gone (expected race): {e}")
            log(f"[debug] updating DB status to 'completed'...")
            db_update_status(item_id, "completed")
            log(f"[debug] DB updated. Logging SUCCESS...")
            live_status["progress"] = 100
            log(f"SUCCESS: Completed '{output_name}'")
            log(f"[debug] sending completion notification...")
            send_notification("Download Complete!", f"Saved: {output_name}", out_file)
            log(f"[debug] notification sent. Releasing wake lock...")
            release_wake_lock()
            log(f"[debug] wake lock released. Returning True for '{output_name}'.")
            return True
        elif aria_task_status in ("error", "removed"):
            log(f"[!] aria2 reported an error for '{output_name}': {aria_error_msg or 'unknown error'}. Retrying in 30s...")
            retry_count += 1
            for _ in range(30):
                if get_state("paused") == "true": break
                time.sleep(1)
        elif aria_task_status is None and proc.returncode == 9:
            log("[!] FATAL: Storage full.")
            db_update_status(item_id, "failed_storage")
            send_notification("Download Failed", "Storage Full!")
            release_wake_lock()
            return False
        else:
            log(f"[!] Download interrupted (Exit code {proc.returncode}). Retrying in 30s...")
            retry_count += 1
            for _ in range(30):
                if get_state("paused") == "true": break
                time.sleep(1)

    release_wake_lock()
    return False

def watchdog_loop(worker_thread, check_interval=20, stall_threshold=45):
    """Diagnostic safety net: if the worker thread shows no forward progress
    for longer than stall_threshold seconds while live_status["active"] is
    still True, dump that thread's exact current Python stack frame into the
    log. A genuine hang (blocked on a call with no timeout, a deadlocked lock,
    etc.) never raises an exception, so nothing else here can report where
    it's actually stuck -- this makes that visible directly instead of
    guessing. Progress is tracked via completed_size/progress (which update
    every second during a normal active download) rather than log-line count,
    since normal downloading doesn't call log() every second and would
    otherwise look identical to a real stall. Safe to remove once the freeze
    is confirmed fixed."""
    last_fingerprint = (len(live_status["log"]), live_status.get("completed_size"), live_status.get("progress"))
    stalled_since = None

    while True:
        time.sleep(check_interval)
        current_fingerprint = (len(live_status["log"]), live_status.get("completed_size"), live_status.get("progress"))

        if live_status.get("active") and current_fingerprint == last_fingerprint:
            if stalled_since is None:
                stalled_since = time.time()
            elif time.time() - stalled_since >= stall_threshold:
                frame = sys._current_frames().get(worker_thread.ident)
                if frame:
                    stack = "".join(traceback.format_stack(frame))
                    log(f"[watchdog] Worker thread stalled for {stall_threshold}s+ with no progress. "
                        f"Current stack:\n{stack}")
                else:
                    log("[watchdog] Worker thread stalled but its frame could not be inspected "
                        "(thread may have just exited).")
                stalled_since = time.time()  # avoid re-logging every single interval
        else:
            stalled_since = None

        last_fingerprint = current_fingerprint

def master_queue_loop():
    log("Master persistent queue engine initialized.")

    while True:
        item = db_get_next_item()
        if item:
            live_status["active"] = True
            try:
                process_download_item(item)
            except Exception as e:
                # Safety net: process_download_item should handle its own
                # failure modes internally, but if anything unexpected slips
                # through here, mark just this one item failed and keep the
                # loop alive -- without this, an unhandled exception silently
                # kills this background thread and the ENTIRE queue stops
                # processing forever (the server/UI keep running, which is
                # why that failure mode is so easy to miss).
                log(f"[!] FATAL: Unexpected error processing '{item.get('output_name')}': {e}")
                db_update_status(item["id"], "failed_unexpected_error")
                release_wake_lock()
        else:
            live_status["active"] = False
            live_status["current_file"] = ""
            live_status["current_item_id"] = None
            live_status["progress"] = 0
            live_status["download_speed"] = "0 B/s"
            live_status["eta"] = "--"
            time.sleep(3)


# ==============================================================================
# --- WEB INTERFACE (HTML/CSS/JS) ---
# ==============================================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>TorBox Downloader</title>

    <link rel="icon" type="image/png" href="https://torbox.app/favicon.ico">
    <link rel="apple-touch-icon" href="https://torbox.app/favicon.ico">

    <style>
        :root { --bg: #121212; --card: #1e1e1e; --accent: #3b82f6; --text: #e0e0e0; --dim: #a0a0a0; }
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: 16px; }
        .card { background: var(--card); border-radius: 12px; padding: 16px; margin-bottom: 16px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { font-size: 1.1rem; margin-top: 0; color: var(--accent); }
        label { display: block; font-size: 0.85rem; color: var(--dim); margin-top: 8px; }
        input[type="text"], input[type="password"], select { width: 100%; box-sizing: border-box; padding: 10px; border-radius: 6px; border: 1px solid #333; background: #2a2a2a; color: #fff; margin-top: 4px; font-size: 0.95rem; }
        button { width: 100%; padding: 10px; border-radius: 6px; border: none; background: var(--accent); color: #fff; font-weight: bold; cursor: pointer; margin-top: 12px; font-size: 0.95rem; }
        button.stop { background: #ef4444; }
        button.pause { background: #f59e0b; }
        button.resume { background: #10b981; }
        .file-item { display: flex; align-items: center; padding: 10px 0; border-bottom: 1px solid #333; font-size: 0.9rem; }
        .file-item:last-child { border-bottom: none; }
        .file-item input[type="checkbox"] { width: 20px; height: 20px; margin-right: 12px; accent-color: var(--accent); }
        #logs { font-family: monospace; font-size: 0.8rem; background: #000; padding: 10px; border-radius: 6px; height: 120px; overflow-y: auto; white-space: pre-wrap; color: #4ade80; }
        .controls-row { display: flex; gap: 8px; margin-bottom: 8px; }
        .controls-row button { margin-top: 0; }
        .progress-container { background: #2a2a2a; border-radius: 8px; height: 18px; width: 100%; overflow: hidden; margin: 12px 0 6px 0; }
        .progress-bar { background: var(--accent); height: 100%; width: 0%; transition: width 0.4s ease; }
        .metrics-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 8px; font-size: 0.8rem; color: var(--dim); }
        .list-row { display: flex; align-items: center; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #333; font-size: 0.85rem; gap: 8px; }
        .list-row:last-child { border-bottom: none; }
        .list-row-main { flex: 1; min-width: 0; }
        .list-row-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .list-row-meta { font-size: 0.72rem; color: var(--dim); margin-top: 2px; }
        .badge { display: inline-block; font-size: 0.68rem; padding: 2px 8px; border-radius: 10px; margin-right: 6px; white-space: nowrap; }
        .badge.completed { background: rgba(16,185,129,0.2); color: #4ade80; }
        .badge.failed { background: rgba(239,68,68,0.2); color: #f87171; }
        .badge.type { background: rgba(59,130,246,0.2); color: #60a5fa; }
        .btn-sm { width: auto; padding: 5px 12px; margin: 0; font-size: 0.75rem; border-radius: 5px; flex-shrink: 0; }
        .btn-sm.remove { background: #ef4444; }
        .btn-sm.retry { background: #3b82f6; }
        .empty-msg { color: var(--dim); font-size: 0.85rem; padding: 4px 0; }
    </style>
</head>
<body>
    <div class="card">
        <h2>Configuration</h2>
        <label>TorBox API Key</label>
        <input type="password" id="apiKey" placeholder="Paste your API key here">
    </div>

    <div class="card">
        <h2>Fetch Files</h2>
        <label>Download Type</label>
        <select id="dlType">
            <option value="torrents">Torrent</option>
            <option value="webdl">WebDL (Debrid)</option>
        </select>

        <label>Item ID</label>
        <input type="text" id="torrentId" placeholder="e.g. 1783690">
        <button onclick="fetchFileList()">Get File List</button>
    </div>

    <div class="card" id="fileListCard" style="display:none;">
        <h2>Available Files</h2>
        <div class="controls-row">
            <button onclick="selectAll(true)" style="background:#4b5563;">Select All</button>
            <button onclick="selectAll(false)" style="background:#4b5563;">Deselect All</button>
        </div>
        <div id="fileList"></div>
        <button onclick="startBatchDownload()">Add Selected to Persistent Queue</button>
    </div>

    <div class="card">
        <h2>Live Queue Status</h2>
        <div style="font-size:0.9rem; font-weight:bold;" id="currentFileName">No active download</div>
        <div class="progress-container">
            <div class="progress-bar" id="progressBar"></div>
        </div>
        <div class="metrics-grid">
            <div>Speed: <strong id="speedVal" style="color:#fff;">0 B/s</strong></div>
            <div>Progress: <strong id="pctVal" style="color:#fff;">0%</strong></div>
            <div>Downloaded: <strong id="sizeVal" style="color:#fff;">0 / 0</strong></div>
            <div>ETA: <strong id="etaVal" style="color:#fff;">--</strong></div>
            <div>Pending in DB: <strong id="pendingVal" style="color:#4ade80;">0</strong></div>
            <div>Completed in DB: <strong id="completedVal" style="color:#3b82f6;">0</strong></div>
        </div>
        <div class="controls-row" style="margin-top:12px;">
            <button id="pauseBtn" class="pause" onclick="togglePause(true)">Pause Queue</button>
            <button id="resumeBtn" class="resume" onclick="togglePause(false)" style="display:none;">Resume Queue</button>
        </div>
        <button class="stop" onclick="clearPendingQueue()">Clear Pending Queue</button>
    </div>

    <div class="card">
        <h2>Pending Queue</h2>
        <div id="pendingList"><div class="empty-msg">Loading...</div></div>
    </div>

    <div class="card">
        <h2>History</h2>
        <div id="historyList"><div class="empty-msg">Loading...</div></div>
    </div>

    <div class="card">
        <h2>System Output</h2>
        <div id="logs">Ready</div>
    </div>

<script>
    let currentFiles = [];

    document.addEventListener("DOMContentLoaded", () => {
        const savedKey = localStorage.getItem("torbox_api_key");
        if (savedKey) document.getElementById("apiKey").value = savedKey;

        document.getElementById("apiKey").addEventListener("input", (e) => {
            localStorage.setItem("torbox_api_key", e.target.value);
        });

        setInterval(pollStatus, 1000);
        setInterval(loadQueueDetails, 3000);
        loadQueueDetails();
    });

    function escapeHtml(s) {
        const div = document.createElement("div");
        div.textContent = s;
        return div.innerHTML;
    }

    function statusLabel(status) {
        if (status === "completed") return { cls: "completed", text: "Completed" };
        const map = {
            failed_storage: "Storage Full",
            failed_no_curl: "curl Missing",
            failed_no_aria2c: "aria2c Missing",
            failed_aria2c_launch: "aria2c Launch Error",
            failed_unexpected_error: "Unexpected Error"
        };
        return { cls: "failed", text: map[status] || "Failed" };
    }

    function renderPendingList(items) {
        const el = document.getElementById("pendingList");
        if (!items || items.length === 0) {
            el.innerHTML = '<div class="empty-msg">No pending items.</div>';
            return;
        }
        el.innerHTML = items.map(it => `
            <div class="list-row">
                <div class="list-row-main">
                    <div class="list-row-name">${escapeHtml(it.output_name)}</div>
                    <div class="list-row-meta"><span class="badge type">${escapeHtml((it.dl_type || "torrent").toUpperCase())}</span> ID: ${escapeHtml(it.torrent_id)} &middot; queued ${escapeHtml(it.created_at || "")}</div>
                </div>
                <button class="btn-sm remove" onclick="removeItem(${it.id})">Remove</button>
            </div>
        `).join("");
    }

    function renderHistoryList(items) {
        const el = document.getElementById("historyList");
        if (!items || items.length === 0) {
            el.innerHTML = '<div class="empty-msg">No history yet.</div>';
            return;
        }
        el.innerHTML = items.map(it => {
            const lbl = statusLabel(it.status);
            const retryBtn = lbl.cls === "failed"
                ? `<button class="btn-sm retry" onclick="retryItem(${it.id})">Retry</button>`
                : "";
            return `
            <div class="list-row">
                <div class="list-row-main">
                    <div class="list-row-name">${escapeHtml(it.output_name)}</div>
                    <div class="list-row-meta"><span class="badge ${lbl.cls}">${lbl.text}</span>${escapeHtml(it.created_at || "")}</div>
                </div>
                ${retryBtn}
            </div>`;
        }).join("");
    }

    async function loadQueueDetails() {
        try {
            const res = await fetch("/api/queue_details");
            const data = await res.json();
            renderPendingList(data.pending);
            renderHistoryList(data.history);
        } catch (e) {}
    }

    async function removeItem(id) {
        await fetch("/api/remove_item", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id })
        });
        loadQueueDetails();
        pollStatus();
    }

    async function retryItem(id) {
        await fetch("/api/retry_item", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ id })
        });
        loadQueueDetails();
        pollStatus();
    }

    async function fetchFileList() {
        const key = document.getElementById("apiKey").value.trim();
        const tid = document.getElementById("torrentId").value.trim();
        const type = document.getElementById("dlType").value;
        if (!key || !tid) return alert("Please enter API Key and Item ID.");

        document.getElementById("logs").innerText = `Fetching file list from TorBox (${type.toUpperCase()})...`;
        try {
            const res = await fetch(`/api/list?id=${tid}&key=${key}&type=${type}`);
            const data = await res.json();

            if (!data.success) {
                document.getElementById("logs").innerText = "Error: " + (data.detail || "Failed to fetch list");
                return;
            }

            let rawList = data.data.files || data.data || [];
            if (!Array.isArray(rawList) && typeof rawList === 'object') {
                rawList = rawList.files || [rawList];
            }

            currentFiles = rawList.map((f, i) => {
                if (typeof f === 'string') return { id: i, name: f, size: 0 };
                return {
                    id: f.id !== undefined ? f.id : i,
                    name: f.name || f.filename || f.title || `WebDL_File_${tid}`,
                    size: f.size || f.filesize || 0
                };
            });

            const listContainer = document.getElementById("fileList");
            listContainer.innerHTML = "";

            currentFiles.forEach((f, idx) => {
                const div = document.createElement("div");
                div.className = "file-item";
                div.innerHTML = `
                    <input type="checkbox" id="chk_${idx}" value="${f.id}" checked>
                    <label for="chk_${idx}" style="flex:1; margin:0; cursor:pointer;">
                        <div><strong>${escapeHtml(f.name)}</strong></div>
                        <div style="font-size:0.75rem; color: var(--dim);">${f.size ? (f.size / (1024*1024)).toFixed(2) + " MB" : "Size unknown"}</div>
                    </label>
                `;
                listContainer.appendChild(div);
            });

            document.getElementById("fileListCard").style.display = "block";
            document.getElementById("logs").innerText = `Found ${currentFiles.length} file(s). Select the ones you wish to download.`;
        } catch(e) {
            document.getElementById("logs").innerText = "Error fetching file list: " + e.message;
        }
    }

    function selectAll(check) {
        currentFiles.forEach((_, idx) => {
            const chk = document.getElementById(`chk_${idx}`);
            if (chk) chk.checked = check;
        });
    }

    async function startBatchDownload() {
        const key = document.getElementById("apiKey").value.trim();
        const tid = document.getElementById("torrentId").value.trim();
        const type = document.getElementById("dlType").value;

        const selectedItems = [];
        currentFiles.forEach((f, idx) => {
            const chk = document.getElementById(`chk_${idx}`);
            if (chk && chk.checked) {
                selectedItems.push({
                    api_key: key,
                    dl_type: type,
                    torrent_id: tid,
                    file_id: String(f.id),
                    output_name: f.name,
                    expected_size: f.size || 0
                });
            }
        });

        if (selectedItems.length === 0) return alert("Select at least one file.");

        await fetch("/api/batch_download", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ items: selectedItems })
        });
        pollStatus();
        loadQueueDetails();
    }

    async function togglePause(pauseState) {
        await fetch("/api/pause", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ paused: pauseState })
        });
        pollStatus();
    }

    async function clearPendingQueue() {
        if (confirm("Clear active download and all pending queue items?")) {
            await fetch("/api/clear", { method: "POST" });
            pollStatus();
            loadQueueDetails();
        }
    }

    async function pollStatus() {
        try {
            const res = await fetch("/api/status");
            const data = await res.json();

            const logsDiv = document.getElementById("logs");
            const isScrolledToBottom = logsDiv.scrollHeight - logsDiv.clientHeight <= logsDiv.scrollTop + 20;
            logsDiv.innerText = data.log.join("\\n") || "Idle";
            if (isScrolledToBottom) {
                logsDiv.scrollTop = logsDiv.scrollHeight;
            }

            document.getElementById("pendingVal").innerText = data.pending_count;
            document.getElementById("completedVal").innerText = data.completed_count;

            if (data.paused) {
                document.getElementById("pauseBtn").style.display = "none";
                document.getElementById("resumeBtn").style.display = "block";
            } else {
                document.getElementById("pauseBtn").style.display = "block";
                document.getElementById("resumeBtn").style.display = "none";
            }

            if (data.active) {
                document.getElementById("currentFileName").innerText = data.current_file || "Downloading...";
                document.getElementById("progressBar").style.width = data.progress + "%";
                document.getElementById("speedVal").innerText = data.download_speed;
                document.getElementById("pctVal").innerText = data.progress + "%";
                document.getElementById("sizeVal").innerText = `${data.completed_size} / ${data.total_size}`;
                document.getElementById("etaVal").innerText = data.eta;
            } else {
                document.getElementById("currentFileName").innerText = data.paused ? "Queue Paused" : "No active download";
                document.getElementById("progressBar").style.width = "0%";
                document.getElementById("speedVal").innerText = "0 B/s";
                document.getElementById("pctVal").innerText = "0%";
                document.getElementById("sizeVal").innerText = "0 / 0";
                document.getElementById("etaVal").innerText = "--";
            }
        } catch(e) {}
    }
</script>
</body>
</html>
"""


# ==============================================================================
# --- HTTP SERVER ROUTER ---
# ==============================================================================

class RequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        return

    def _send_json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_html(self, status, html):
        body = html.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_raw_json_text(self, status, text):
        try:
            json.loads(text)
        except Exception:
            self._send_json(502, {"success": False, "detail": "Upstream did not return valid JSON."})
            return
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)

        if parsed.path == "/":
            self._send_html(200, HTML_TEMPLATE)
            return

        if parsed.path.startswith("/api/"):
            if parsed.path == "/api/list":
                qs = parse_qs(parsed.query)
                tid = qs.get("id", [""])[0]
                key = qs.get("key", [""])[0]
                dl_type = qs.get("type", ["torrents"])[0]

                if not tid or not key:
                    self._send_json(400, {"success": False, "detail": "Missing item id or API key."})
                    return

                list_url = f"https://api.torbox.app/v1/api/{dl_type}/mylist?id={tid}&bypass_cache=true"
                try:
                    req = Request(list_url, headers={"Authorization": f"Bearer {key}"})
                    with urlopen(req, timeout=15) as response:
                        body = response.read().decode("utf-8", errors="replace")
                    self._send_raw_json_text(200, body)
                except urllib.error.HTTPError as e:
                    body = e.read().decode("utf-8", errors="replace")
                    self._send_raw_json_text(e.code, body)
                except Exception as e:
                    self._send_json(502, {"success": False, "detail": f"Request to TorBox failed: {e}"})
                return

            if parsed.path == "/api/status":
                pending_count, completed_count = db_get_queue_summary()
                resp = {
                    **live_status,
                    "paused": get_state("paused") == "true",
                    "pending_count": pending_count,
                    "completed_count": completed_count
                }
                self._send_json(200, resp)
                return

            if parsed.path == "/api/queue_details":
                resp = {
                    "pending": db_get_pending_list(),
                    "history": db_get_history_list()
                }
                self._send_json(200, resp)
                return

        self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if not self.path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", 0) or 0)
        try:
            raw = self.rfile.read(length) if length > 0 else b"{}"
            body = json.loads(raw) if raw else {}
        except Exception:
            self._send_json(400, {"error": "invalid JSON body"})
            return

        if self.path == "/api/batch_download":
            items = body.get("items", [])
            required_keys = ("api_key", "torrent_id", "output_name")
            if not isinstance(items, list) or not items:
                self._send_json(400, {"error": "no items provided"})
                return
            for item in items:
                if not isinstance(item, dict) or not all(k in item and item[k] for k in required_keys):
                    self._send_json(400, {"error": f"each item needs non-empty {required_keys}"})
                    return
                if not os.path.basename(str(item["output_name"]).strip()):
                    self._send_json(400, {"error": "output_name resolves to an empty filename"})
                    return
            db_add_items(items)
            log(f"Added {len(items)} file(s) to persistent SQLite queue.")
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/api/pause":
            pause_state = body.get("paused", True)
            set_state("paused", "true" if pause_state else "false")
            log(f"Queue pause state updated: {pause_state}")
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/api/clear":
            db_clear_queue()
            log("Cleared pending queue database and terminated active download.")
            self._send_json(200, {"status": "ok"})
            return

        if self.path == "/api/remove_item":
            item_id = body.get("id")
            if item_id is None:
                self._send_json(400, {"error": "missing id"})
                return
            removed = db_remove_item(item_id)
            if removed:
                log(f"Removed item #{item_id} from queue.")
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(409, {"status": "not_removed", "detail": "Item not found in queue."})
            return

        if self.path == "/api/retry_item":
            item_id = body.get("id")
            if item_id is None:
                self._send_json(400, {"error": "missing id"})
                return
            requeued = db_requeue_failed_item(item_id)
            if requeued:
                log(f"Requeued item #{item_id} for another attempt.")
                self._send_json(200, {"status": "ok"})
            else:
                self._send_json(409, {"status": "not_requeued", "detail": "Item is not in a failed state."})
            return

        self._send_json(404, {"error": "not found"})


# ==============================================================================
# --- ANDROID ENTRY POINTS ---
# ==============================================================================
# Called from Kotlin (DownloadService.kt) in this order, once, before the
# server starts:
#   set_bridge(AndroidBridge(context))
#   set_download_dir(bridge.getDownloadDir())
#   set_db_path(bridge.getDbPath())
#   set_aria2c_path("<nativeLibraryDir>/libaria2c.so")
#   start_server()
#
# There is no __main__ block or OS-signal handling here (no equivalent of
# SIGTERM in the Termux sense) -- the service lifecycle is managed by
# Android: start_server() is called once from a background Thread in
# DownloadService.onStartCommand(), and that thread simply lives for as long
# as the foreground service does. stop_server() is available for a clean
# shutdown if you want to call it from DownloadService.onDestroy().

_http_server = None

def set_bridge(bridge):
    global _bridge
    _bridge = bridge

def set_download_dir(path):
    global DOWNLOAD_DIR
    DOWNLOAD_DIR = path
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

def set_db_path(path):
    global DB_PATH
    DB_PATH = path

def set_aria2c_path(path):
    global ARIA2C_PATH
    ARIA2C_PATH = path

def start_server():
    global _http_server

    if DOWNLOAD_DIR is None or DB_PATH is None:
        raise RuntimeError("set_download_dir() and set_db_path() must be called before start_server()")

    init_db()
    run_startup_integrity_check()

    t = threading.Thread(target=master_queue_loop, daemon=True)
    t.start()

    watchdog = threading.Thread(target=watchdog_loop, args=(t,), daemon=True)
    watchdog.start()

    ThreadingHTTPServer.allow_reuse_address = True
    _http_server = ThreadingHTTPServer((HOST, PORT), RequestHandler)
    log(f"TorBox server running at http://{HOST}:{PORT}")

    # Blocks forever -- this is why DownloadService.kt calls start_server()
    # from its own background Thread rather than its main thread.
    _http_server.serve_forever()

def stop_server():
    global _http_server
    log("Shutting down server and releasing locks...")
    release_wake_lock()
    kill_active_aria2()
    if _http_server is not None:
        _http_server.shutdown()
        _http_server.server_close()
        _http_server = None
        sys.exit(0)
