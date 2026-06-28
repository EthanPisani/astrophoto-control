"""
AstroCap — Astrophotography Capture Controller
Flask backend that proxies the nikon_bulb_server gphoto2 API and manages capture sessions.

Usage:
    pip install flask requests
    python app.py

Config (edit below or set env vars):
    GPHOTO_API_BASE  — base URL of your nikon_bulb_server  (default: http://localhost:8080)
    ASTROCAP_PORT    — port this server listens on          (default: 7777)
    ASTROCAP_OUTDIR  — base directory for saved captures    (default: ./captures)
"""

import os
import time
import threading
import logging
from datetime import datetime
from pathlib import Path

import requests
from flask import Flask, render_template, jsonify, request

# ─── Config ───────────────────────────────────────────────────────────────────
GPHOTO_API_BASE = os.environ.get("GPHOTO_API_BASE", "http://localhost:8080")
OUTPUT_BASE_DIR = Path(os.environ.get("ASTROCAP_OUTDIR", "./captures"))
SERVER_PORT     = int(os.environ.get("ASTROCAP_PORT", 7777))

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(levelname)s  %(message)s")
log = logging.getLogger("astrocap")

# ─── Session state (in-memory, single active session) ─────────────────────────
_lock = threading.Lock()
_session = {
    "id":                    None,
    "status":                "idle",   # idle | running | canceling | canceled | done | error
    "total":                 0,
    "completed":             0,
    "start_time":            None,
    "output_dir":            None,
    "captures":              [],       # list of per-capture result dicts
    "cancel_requested":      False,
    "error":                 None,
    "current_capture_start": None,     # epoch float while shutter is open
    "settings":              {},
}


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _make_output_dir() -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = OUTPUT_BASE_DIR / ts
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _gphoto_get(path: str, timeout: int = 10) -> dict:
    resp = requests.get(f"{GPHOTO_API_BASE}{path}", timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def _gphoto_post(path: str, body: dict | None = None, timeout: int = 300) -> dict:
    resp = requests.post(f"{GPHOTO_API_BASE}{path}", json=body or {}, timeout=timeout)
    resp.raise_for_status()
    return resp.json()


# ─── Background capture thread ────────────────────────────────────────────────

def _run_session(settings: dict):
    """Runs entirely in a daemon thread. Updates _session in place."""
    num_photos       = settings["num_photos"]
    exposure_seconds = settings["exposure_seconds"]
    interval_seconds = settings["interval_seconds"]
    iso              = settings.get("iso") or None
    aperture         = settings.get("aperture") or None
    image_format     = settings.get("image_format", "RAW")
    capture_target   = settings.get("capture_target", "sdram")
    auto_recover     = settings.get("auto_recover", True)

    output_dir = _make_output_dir()
    log_path   = os.path.join(output_dir, "session.log")

    def _log(msg: str):
        ts   = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        log.info(msg)
        with open(log_path, "a") as fh:
            fh.write(line + "\n")

    with _lock:
        _session.update(
            output_dir=output_dir,
            start_time=time.time(),
            captures=[],
            completed=0,
            total=num_photos,
            error=None,
            status="running",
        )

    _log(f"Session start — {num_photos} × {exposure_seconds}s, interval {interval_seconds}s")
    _log(f"Output: {output_dir}")

    for i in range(num_photos):
        # ── Check cancel before each exposure ──────────────────────────────
        with _lock:
            if _session["cancel_requested"]:
                _session["status"] = "canceled"
                _log("Canceled before capture.")
                return

        _log(f"Capture {i + 1}/{num_photos} …")

        capture_body = {
            "shutter_speed":    "bulb",
            "exposure_seconds": exposure_seconds,
            # "auto_recover_usb": auto_recover,
            "capture_target":   "sdram",
        }
        if iso:
            capture_body["iso"] = iso
        # if aperture:
        #     capture_body["aperture"] = aperture
        # if image_format:
        #     capture_body["image_format"] = image_format

        with _lock:
            _session["current_capture_start"] = time.time()

        try:
            result = _gphoto_post("/api/v1/captures", capture_body)
            cap = {
                "index":       i + 1,
                "status":      "ok",
                "capture_id":  result.get("capture_id", ""),
                "saved_path":  result.get("saved_path", ""),
                "source_name": result.get("source_name", ""),
                "captured_at": result.get("captured_at", ""),
            }
            _log(f"  ✓ saved → {result.get('source_name', result.get('saved_path', ''))}")
        except requests.HTTPError as exc:
            cap = {"index": i + 1, "status": "error",
                   "error": exc.response.text if exc.response else str(exc)}
            _log(f"  ✗ HTTP {exc.response.status_code if exc.response else '?'}: {cap['error']}")
        except Exception as exc:
            cap = {"index": i + 1, "status": "error", "error": str(exc)}
            _log(f"  ✗ Exception: {exc}")

        with _lock:
            _session["captures"].append(cap)
            _session["completed"] = i + 1
            _session["current_capture_start"] = None

        # ── Interruptible interval wait ────────────────────────────────────
        if i < num_photos - 1:
            deadline = time.time() + interval_seconds
            while time.time() < deadline:
                with _lock:
                    if _session["cancel_requested"]:
                        _session["status"] = "canceled"
                        _log("Canceled during interval.")
                        return
                time.sleep(0.25)

    with _lock:
        _session["status"] = "done"
    _log("Session complete.")


# ─── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html", gphoto_base=GPHOTO_API_BASE)


# ── Session control ────────────────────────────────────────────────────────────

@app.route("/session/start", methods=["POST"])
def session_start():
    data = request.get_json(force=True)

    with _lock:
        if _session["status"] == "running":
            return jsonify(error="A session is already running."), 409
        _session["cancel_requested"] = False
        _session["id"] = None

    t = threading.Thread(target=_run_session, args=(data,), daemon=True)
    t.start()
    return jsonify(ok=True)


@app.route("/session/cancel", methods=["POST"])
def session_cancel():
    with _lock:
        if _session["status"] not in ("running", "canceling"):
            return jsonify(error="No active session."), 400
        _session["cancel_requested"] = True
        _session["status"] = "canceling"
    return jsonify(ok=True)


@app.route("/session/status")
def session_status():
    with _lock:
        snap = {
            "status":                _session["status"],
            "total":                 _session["total"],
            "completed":             _session["completed"],
            "start_time":            _session["start_time"],
            "output_dir":            _session["output_dir"],
            "captures":              list(_session["captures"]),
            "current_capture_start": _session["current_capture_start"],
            "error":                 _session["error"],
        }
    return jsonify(snap)


# ── Camera proxy ───────────────────────────────────────────────────────────────

@app.route("/camera/health")
def camera_health():
    try:
        return jsonify(_gphoto_get("/api/v1/health"))
    except requests.HTTPError as exc:
        return jsonify(error=exc.response.text), exc.response.status_code
    except Exception as exc:
        return jsonify(error=str(exc)), 503


@app.route("/camera/capabilities")
def camera_capabilities():
    try:
        return jsonify(_gphoto_get("/api/v1/camera/capabilities"))
    except requests.HTTPError as exc:
        return jsonify(error=exc.response.text), exc.response.status_code
    except Exception as exc:
        return jsonify(error=str(exc)), 503


@app.route("/camera/recover", methods=["POST"])
def camera_recover():
    try:
        return jsonify(_gphoto_post("/api/v1/recover"))
    except requests.HTTPError as exc:
        return jsonify(error=exc.response.text), exc.response.status_code
    except Exception as exc:
        return jsonify(error=str(exc)), 503


# ─── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)
    print(f"""
╔══════════════════════════════════════════════════╗
║         AstroCap — Capture Controller            ║
╠══════════════════════════════════════════════════╣
║  UI      →  http://localhost:{SERVER_PORT:<5}               ║
║  Camera  →  {GPHOTO_API_BASE:<38} ║
║  Output  →  {str(OUTPUT_BASE_DIR):<38} ║
╚══════════════════════════════════════════════════╝
""")
    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)
