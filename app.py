
from __future__ import annotations
 
import contextlib
import json
import logging
import os
import sys
import re
import shutil
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from datetime import timezone
import requests
from flask import Flask, jsonify, render_template, request, send_file, url_for
 
# Import plate solving module (creates background tasks)
import platesolve
import siril_annotate
use_siril_backend = False
 
GPHOTO_API_BASE = os.environ.get("GPHOTO_API_BASE", "http://localhost:8080").rstrip("/")
OUTPUT_BASE_DIR = Path(os.environ.get("ASTROCAP_OUTDIR", "./captures"))
SESSIONS_BASE_DIR = OUTPUT_BASE_DIR / "sessions"
DB_PATH = Path(os.environ.get("ASTROCAP_DB_PATH", str(OUTPUT_BASE_DIR / "astrocap.db")))
SERVER_PORT = int(os.environ.get("ASTROCAP_PORT", 7777))
HTTP_TIMEOUT = int(os.environ.get("ASTROCAP_HTTP_TIMEOUT", 20))
 
SEQUENCE_TYPES = {"lights", "darks", "flats", "biases"}
TERMINAL_CAPTURE_STATES = {"complete", "failed", "canceled", "done", "error"}
 
# Auto-recovery: how many total attempts a single frame gets (1 = no
# retry) before the whole session is given up on, and how long to back
# off between attempts (grows linearly with attempt number).
AUTO_RECOVER_MAX_ATTEMPTS = 3
AUTO_RECOVER_BACKOFF_S = 5.0
 
 
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("astrocap")
 
 
@contextlib.contextmanager
def logged_camera_call(op: str, on_step=None, **ctx: Any):
    """Wrap a single call to the camera bridge with start/outcome/timing
    logs, tagged with whatever context is passed in (session id, seq,
    capture id, etc). This is what lets us reconstruct, after the fact,
    exactly which operation was in flight when something failed and
    whether any two calls' time windows actually overlapped (proof of a
    real concurrency conflict, as opposed to a one-off hardware hiccup).
 
    `on_step`, if given, is called with a human-readable label right
    before the call so the caller can stash "what we were doing" for use
    in an error message shown to the user.
    """
    ctx_str = " ".join(f"{k}={v}" for k, v in ctx.items() if v is not None)
    label = f"{op}({ctx_str})" if ctx_str else op
    if on_step is not None:
        on_step(label)
    started = time.time()
    log.info("camera-call start  %s", label)
    try:
        yield
    except ApiFailure as exc:
        duration = time.time() - started
        log.error(
            "camera-call FAILED %s after %.2fs -> HTTP %s: %s",
            label, duration, exc.status_code, exc,
        )
        raise
    except Exception as exc:  # noqa: BLE001
        duration = time.time() - started
        log.exception("camera-call FAILED %s after %.2fs -> %s", label, duration, exc)
        raise
    else:
        duration = time.time() - started
        log.info("camera-call ok     %s (%.2fs)", label, duration)
 
 
class AstroError(Exception):
    pass
 
 
@dataclass
class ApiFailure(Exception):
    status_code: int
    payload: dict[str, Any]
 
    def __str__(self) -> str:
        message = self.payload.get("message") or self.payload.get("error") or "request failed"
        return f"HTTP {self.status_code}: {message}"
 
 
class NikonApiClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.session = requests.Session()
 
    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"
 
    def _handle_error(self, resp: requests.Response) -> None:
        try:
            payload = resp.json()
        except ValueError:
            payload = {"error": resp.text}
        raise ApiFailure(status_code=resp.status_code, payload=payload)
 
    def _json_request(self, method: str, path: str, body: Optional[dict[str, Any]] = None, timeout: int = HTTP_TIMEOUT) -> dict[str, Any]:
        resp = self.session.request(method, self._url(path), json=body, timeout=timeout)
        if not resp.ok:
            self._handle_error(resp)
        if not resp.content:
            return {}
        return resp.json()
 
    def create_capture(self, body: dict[str, Any]) -> dict[str, Any]:
        return self._json_request("POST", "/api/v1/captures", body=body, timeout=max(HTTP_TIMEOUT, 60))
 
    def get_capture(self, capture_id: str) -> dict[str, Any]:
        return self._json_request("GET", f"/api/v1/captures/{capture_id}")
 
    def list_captures(self, status: Optional[str] = None, limit: int = 20, after: Optional[str] = None) -> dict[str, Any]:
        params = []
        if status:
            params.append(("status", status))
        if limit:
            params.append(("limit", str(limit)))
        if after:
            params.append(("after", after))
        suffix = ""
        if params:
            suffix = "?" + "&".join(f"{k}={requests.utils.quote(v)}" for k, v in params)
        return self._json_request("GET", f"/api/v1/captures{suffix}")
 
    def mark_downloaded(self, capture_id: str) -> dict[str, Any]:
        return self._json_request("POST", f"/api/v1/captures/{capture_id}/downloaded")
 
    def cancel_capture(self, capture_id: str) -> dict[str, Any]:
        return self._json_request("POST", f"/api/v1/captures/{capture_id}/cancel")
 
    def delete_capture(self, capture_id: str) -> None:
        resp = self.session.delete(self._url(f"/api/v1/captures/{capture_id}"), timeout=HTTP_TIMEOUT)
        if resp.status_code == 204:
            return
        if not resp.ok:
            self._handle_error(resp)
 
    def download_capture_file(self, capture_id: str, dest_path: Path) -> None:
        with self.session.get(self._url(f"/api/v1/captures/{capture_id}/file"), stream=True, timeout=max(HTTP_TIMEOUT, 120)) as resp:
            if not resp.ok:
                self._handle_error(resp)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with dest_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        fh.write(chunk)
 
    def health(self) -> dict[str, Any]:
        return self._json_request("GET", "/api/v1/health")
 
    def capabilities(self) -> dict[str, Any]:
        return self._json_request("GET", "/api/v1/camera/capabilities")
 
    def recover(self) -> dict[str, Any]:
        return self._json_request("POST", "/api/v1/recover")
 
 
class AstroDb:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._init_schema()
 
    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    session_name TEXT NOT NULL,
                    session_slug TEXT NOT NULL,
                    sequence_type TEXT NOT NULL,
                    config_json TEXT NOT NULL,
                    output_dir TEXT NOT NULL,
                    total INTEGER NOT NULL,
                    completed INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    paused_at TEXT,
                    completed_at TEXT,
                    current_capture_id TEXT,
                    current_capture_started_at TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL
                );
 
                CREATE TABLE IF NOT EXISTS session_captures (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    seq INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    source_name TEXT,
                    local_path TEXT,
                    thumb_path TEXT,
                    size_bytes INTEGER,
                    captured_at TEXT,
                    error TEXT,
                    metadata_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
 
                CREATE INDEX IF NOT EXISTS idx_sessions_created_at ON sessions(created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_session_captures_session_seq ON session_captures(session_id, seq);
                """
            )
            self._conn.commit()
 
    def _execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur
 
    def create_session(
        self,
        session_id: str,
        session_name: str,
        session_slug: str,
        sequence_type: str,
        config: dict[str, Any],
        output_dir: str,
        total: int,
    ) -> None:
        now = utc_now_iso()
        self._execute(
            """
            INSERT INTO sessions (
                id, status, session_name, session_slug, sequence_type,
                config_json, output_dir, total, completed, created_at
            ) VALUES (?, 'configured', ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                session_id,
                session_name,
                session_slug,
                sequence_type,
                json.dumps(config),
                output_dir,
                total,
                now,
            ),
        )
 
    def update_session_fields(self, session_id: str, **fields: Any) -> None:
        if not fields:
            return
        keys = sorted(fields.keys())
        set_clause = ", ".join(f"{k} = ?" for k in keys)
        params = tuple(fields[k] for k in keys) + (session_id,)
        self._execute(f"UPDATE sessions SET {set_clause} WHERE id = ?", params)
 
    def add_capture(
        self,
        capture_id: str,
        session_id: str,
        seq: int,
        status: str,
        source_name: Optional[str],
        local_path: Optional[str],
        thumb_path: Optional[str],
        size_bytes: Optional[int],
        captured_at: Optional[str],
        error: Optional[str],
        metadata: dict[str, Any],
    ) -> None:
        self._execute(
            """
            INSERT OR REPLACE INTO session_captures (
                id, session_id, seq, status, source_name, local_path, thumb_path,
                size_bytes, captured_at, error, metadata_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                capture_id,
                session_id,
                seq,
                status,
                source_name,
                local_path,
                thumb_path,
                size_bytes,
                captured_at,
                error,
                json.dumps(metadata),
                utc_now_iso(),
            ),
        )
 
    def get_session(self, session_id: str) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
        row = cur.fetchone()
        return row_to_session(row) if row else None
 
    def get_latest_session(self) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM sessions ORDER BY created_at DESC LIMIT 1")
        row = cur.fetchone()
        return row_to_session(row) if row else None
 
    def list_sessions(self, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
        cur = self._execute(
            """
            SELECT
                s.*,
                COALESCE(SUM(CASE WHEN c.status = 'ok' THEN 1 ELSE 0 END), 0) AS ok_count,
                COALESCE(SUM(CASE WHEN c.status = 'error' THEN 1 ELSE 0 END), 0) AS error_count
            FROM sessions s
            LEFT JOIN session_captures c ON c.session_id = s.id
            GROUP BY s.id
            ORDER BY s.created_at DESC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        )
        rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = row_to_session(row)
            item["ok_count"] = int(row["ok_count"])
            item["error_count"] = int(row["error_count"])
            result.append(item)
        return result
 
    def get_session_captures(self, session_id: str, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
        cur = self._execute(
            """
            SELECT * FROM session_captures
            WHERE session_id = ?
            ORDER BY seq ASC
            LIMIT ? OFFSET ?
            """,
            (session_id, limit, offset),
        )
        rows = cur.fetchall()
        result = []
        for row in rows:
            metadata = {}
            if row["metadata_json"]:
                try:
                    metadata = json.loads(row["metadata_json"])
                except json.JSONDecodeError:
                    metadata = {}
            result.append(
                {
                    "id": row["id"],
                    "session_id": row["session_id"],
                    "seq": row["seq"],
                    "status": row["status"],
                    "source_name": row["source_name"],
                    "local_path": row["local_path"],
                    "thumb_path": row["thumb_path"],
                    "size_bytes": row["size_bytes"],
                    "captured_at": row["captured_at"],
                    "error": row["error"],
                    "metadata": metadata,
                }
            )
        return result
 
    def get_capture(self, capture_id: str) -> Optional[dict[str, Any]]:
        cur = self._execute("SELECT * FROM session_captures WHERE id = ? LIMIT 1", (capture_id,))
        row = cur.fetchone()
        if not row:
            return None
        metadata = {}
        if row["metadata_json"]:
            try:
                metadata = json.loads(row["metadata_json"])
            except json.JSONDecodeError:
                metadata = {}
        return {
            "id": row["id"],
            "session_id": row["session_id"],
            "seq": row["seq"],
            "status": row["status"],
            "source_name": row["source_name"],
            "local_path": row["local_path"],
            "thumb_path": row["thumb_path"],
            "size_bytes": row["size_bytes"],
            "captured_at": row["captured_at"],
            "error": row["error"],
            "metadata": metadata,
        }
 
 
class SessionManager:
    def __init__(self, db: AstroDb, api_client: NikonApiClient):
        self.db = db
        self.api_client = api_client
        self._lock = threading.Lock()
        self._pause_event = threading.Event()
        self._pause_event.set()
        self._cancel_event = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._active_session_id: Optional[str] = None
        self._current_step: str = ""
 
    def _call(self, op: str, **ctx: Any):
        """Shorthand for logged_camera_call that also records the step
        onto this manager, so an error handler can report what we were
        doing at the moment of failure."""
        return logged_camera_call(op, on_step=lambda label: setattr(self, "_current_step", label), **ctx)
 
    def active_session_id(self) -> Optional[str]:
        with self._lock:
            return self._active_session_id
 
    def start(self, config: dict[str, Any], total: int) -> dict[str, Any]:
        with self._lock:
            if self._worker and self._worker.is_alive():
                raise AstroError("a session is already running")
 
            session_id = str(uuid.uuid4())
            session_name = config["session_name"]
            sequence_type = config["sequence_type"]
            output_dir, slug = make_session_dir(session_name, sequence_type)
 
            self.db.create_session(
                session_id=session_id,
                session_name=session_name,
                session_slug=slug,
                sequence_type=sequence_type,
                config=config,
                output_dir=str(output_dir),
                total=total,
            )
            self.db.update_session_fields(session_id, status="running", started_at=utc_now_iso())
 
            self._pause_event.set()
            self._cancel_event.clear()
            self._active_session_id = session_id
 
            self._worker = threading.Thread(
                target=self._run_session,
                args=(session_id, config, output_dir, total),
                daemon=True,
            )
            self._worker.start()
 
        return self.db.get_session(session_id) or {}
 
    def pause(self) -> None:
        session = self.current_session()
        if not session or session["status"] not in {"running", "canceling"}:
            raise AstroError("no running session")
        self._pause_event.clear()
        self.db.update_session_fields(session["id"], status="paused", paused_at=utc_now_iso())
 
    def resume(self) -> None:
        session = self.current_session()
        if not session or session["status"] != "paused":
            raise AstroError("session is not paused")
        self._pause_event.set()
        self.db.update_session_fields(session["id"], status="running", paused_at=None)
 
    def stop(self) -> None:
        session = self.current_session()
        if not session or session["status"] not in {"running", "paused", "canceling"}:
            raise AstroError("no active session")
 
        self._cancel_event.set()
        self._pause_event.set()
        self.db.update_session_fields(session["id"], status="canceling")
 
        current_capture_id = session.get("current_capture_id")
        if current_capture_id:
            try:
                with self._call("cancel_capture(user stop)", session=session["id"], capture=current_capture_id):
                    self.api_client.cancel_capture(current_capture_id)
            except Exception as exc:  # noqa: BLE001
                log.warning("cancel capture failed for %s: %s", current_capture_id, exc)
 
    def current_session(self) -> Optional[dict[str, Any]]:
        active_id = self.active_session_id()
        if active_id:
            session = self.db.get_session(active_id)
            if session:
                return session
 
        latest = self.db.get_latest_session()
        if not latest:
            return None
 
        # 🚨 IMPORTANT FIX: do NOT stick UI on terminal sessions
        if latest["status"] in TERMINAL_CAPTURE_STATES:
            return None
 
        return latest
 
    def _attempt_camera_recovery(self, session_id: str, seq: int, reason: str) -> bool:
        """Best-effort attempt to recover the camera bridge after a failed
        frame (e.g. a USB fault or a busy-camera conflict), so a transient
        hiccup doesn't have to take down the whole session. Swallows its
        own failures — the retry that follows will find out soon enough
        whether the camera is actually usable again."""
        log.warning(
            "session %s seq=%s attempting camera recovery (reason: %s)",
            session_id, seq, reason,
        )
        try:
            with self._call("recover(auto)", session=session_id, seq=seq):
                self.api_client.recover()
            log.info("session %s seq=%s camera recovery call succeeded", session_id, seq)
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("session %s seq=%s camera recovery call failed: %s", session_id, seq, exc)
            return False
 
    def _run_session(self, session_id: str, config: dict[str, Any], output_dir: Path, total: int) -> None:
        exposure_seconds = int(config["exposure_seconds"])
        interval_seconds = int(config["interval_seconds"])
        capture_body = build_capture_request(config)
        auto_recover = bool(config.get("auto_recover_usb"))
        self._current_step = ""
        seq = 0
 
        log.info(
            "session %s worker starting: total=%s exposure=%ss interval=%ss auto_recover=%s",
            session_id, total, exposure_seconds, interval_seconds, auto_recover,
        )
 
        try:
            for seq in range(1, total + 1):
                if self._cancel_event.is_set():
                    self.db.update_session_fields(session_id, status="canceled", completed_at=utc_now_iso())
                    return
 
                while not self._pause_event.wait(timeout=0.5):
                    if self._cancel_event.is_set():
                        self.db.update_session_fields(session_id, status="canceled", completed_at=utc_now_iso())
                        return
 
                frame_attempt = 0
                while True:
                    frame_attempt += 1
                    try:
                        with self._call("create_capture", session=session_id, seq=seq, attempt=frame_attempt):
                            accepted = self.api_client.create_capture(capture_body)
                        capture_id = str(accepted.get("capture_id") or "")
                        if not capture_id:
                            raise AstroError("backend did not return capture_id")
 
                        self.db.update_session_fields(
                            session_id,
                            current_capture_id=capture_id,
                            current_capture_started_at=utc_now_iso(),
                        )
 
                        record = self._wait_for_capture(
                            capture_id, exposure_seconds, session_id=session_id, seq=seq,
                        )
                        state = record.get("status")
                        source_name = record.get("source_name") or ""
 
                        if state != "complete":
                            err = record.get("error") or f"capture ended with status: {state}"
                            self.db.add_capture(
                                capture_id=capture_id,
                                session_id=session_id,
                                seq=seq,
                                status="error",
                                source_name=source_name or None,
                                local_path=None,
                                thumb_path=None,
                                size_bytes=record.get("size_bytes"),
                                captured_at=record.get("completed_at") or utc_now_iso(),
                                error=err,
                                metadata=record,
                            )
                            raise AstroError(err)
 
                        local_name = choose_local_filename(seq, capture_id, source_name)
                        local_path = output_dir / local_name
                        with self._call("download_capture_file", session=session_id, seq=seq, capture=capture_id):
                            self.api_client.download_capture_file(capture_id, local_path)
                        try:
                            with self._call("mark_downloaded", session=session_id, seq=seq, capture=capture_id):
                                self.api_client.mark_downloaded(capture_id)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("mark_downloaded failed for %s: %s", capture_id, exc)
 
                        thumb_path = maybe_make_thumbnail(local_path, output_dir / "thumbs")
                        self.db.add_capture(
                            capture_id=capture_id,
                            session_id=session_id,
                            seq=seq,
                            status="ok",
                            source_name=source_name or local_name,
                            local_path=str(local_path),
                            thumb_path=str(thumb_path) if thumb_path else None,
                            size_bytes=local_path.stat().st_size if local_path.exists() else record.get("size_bytes"),
                            captured_at=record.get("completed_at") or utc_now_iso(),
                            error=None,
                            metadata=record,
                        )
                        self.db.update_session_fields(
                            session_id,
                            completed=seq,
                            current_capture_id=None,
                            current_capture_started_at=None,
                            error=None,
                        )
                        break  # frame succeeded — leave the retry loop
 
                    except Exception as exc:  # noqa: BLE001
                        if self._cancel_event.is_set():
                            self.db.update_session_fields(
                                session_id,
                                status="canceled",
                                completed_at=utc_now_iso(),
                                current_capture_id=None,
                                current_capture_started_at=None,
                            )
                            return
 
                        can_retry = auto_recover and frame_attempt < AUTO_RECOVER_MAX_ATTEMPTS
                        log.warning(
                            "session %s frame seq=%s attempt %s/%s failed: %s (auto_recover=%s)",
                            session_id, seq, frame_attempt, AUTO_RECOVER_MAX_ATTEMPTS, exc, auto_recover,
                        )
 
                        if not can_retry:
                            # Out of attempts (or auto-recover disabled) —
                            # give up on this frame the way the session
                            # always has: end the whole session in error.
                            # The outer except blocks below build the final
                            # error message (using self._current_step) and
                            # log the full traceback.
                            raise
 
                        self.db.update_session_fields(
                            session_id,
                            current_capture_id=None,
                            current_capture_started_at=None,
                            error=(
                                f"recovering from: {exc} "
                                f"(attempt {frame_attempt}/{AUTO_RECOVER_MAX_ATTEMPTS})"
                            ),
                        )
                        self._attempt_camera_recovery(session_id, seq, reason=str(exc))
 
                        backoff = AUTO_RECOVER_BACKOFF_S * frame_attempt
                        waited = 0.0
                        while waited < backoff:
                            if self._cancel_event.is_set():
                                break
                            time.sleep(0.5)
                            waited += 0.5
                        # loop back around and retry the same seq
 
                if seq < total:
                    waited = 0.0
                    while waited < interval_seconds:
                        if self._cancel_event.is_set():
                            self.db.update_session_fields(session_id, status="canceled", completed_at=utc_now_iso())
                            return
                        if not self._pause_event.is_set():
                            self._pause_event.wait()
                        time.sleep(0.25)
                        waited += 0.25
 
            self.db.update_session_fields(session_id, status="done", completed_at=utc_now_iso())
            log.info("session %s completed: %s/%s frames", session_id, total, total)
        except ApiFailure as exc:
            step = self._current_step or "unknown step"
            err = f"[{step}] {exc}"
            log.error("session %s FAILED at step=%s seq=%s: %s", session_id, step, seq, exc)
            self.db.update_session_fields(
                session_id,
                status="error",
                error=err,
                completed_at=utc_now_iso(),
                current_capture_id=None,
                current_capture_started_at=None,
            )
        except Exception as exc:  # noqa: BLE001
            step = self._current_step or "unknown step"
            err = f"[{step}] {exc}"
            log.exception("session %s FAILED unexpectedly at step=%s seq=%s: %s", session_id, step, seq, exc)
            self.db.update_session_fields(
                session_id,
                status="error",
                error=err,
                completed_at=utc_now_iso(),
                current_capture_id=None,
                current_capture_started_at=None,
            )
        finally:
            with self._lock:
                # NOTE: we deliberately do NOT clear self._active_session_id
                # here. If we did, current_session() would immediately start
                # falling through to the "latest session" fallback below,
                # which hides any session in a terminal state (done/error/
                # canceled). Since a poll can land a few milliseconds after
                # this thread finishes, that was hiding the just-finished
                # session before the UI ever got a chance to render the
                # terminal panel (error banner, final frame count, "New
                # Session" button) — the session would just vanish and the
                # user would be dropped back on the setup screen with no
                # explanation, sometimes instantly, sometimes a few frames
                # in, depending on exactly when the poll happened to land.
                #
                # Instead, the session stays "active" (and visible) even
                # after it reaches a terminal status, until the user
                # explicitly dismisses it (see dismiss()) or starts a new
                # session (see start(), which reassigns active_session_id).
                self._cancel_event.clear()
                self._pause_event.set()
            log.info("session %s worker exiting (last step=%s)", session_id, self._current_step or "n/a")
 
    def dismiss(self) -> None:
        """Explicitly release the current session once the user has seen
        its terminal state and clicked 'New Session'. Only releases if the
        session has actually finished — never dismisses a live session."""
        with self._lock:
            session_id = self._active_session_id
            if not session_id:
                return
            session = self.db.get_session(session_id)
            if session and session["status"] in TERMINAL_CAPTURE_STATES:
                self._active_session_id = None
 
    def _wait_for_capture(
        self, capture_id: str, exposure_seconds: int,
        session_id: Optional[str] = None, seq: Optional[int] = None,
    ) -> dict[str, Any]:
        timeout = max(90, exposure_seconds * 2 + 60)
        deadline = time.time() + timeout
        cancel_sent = False
        poll_n = 0
 
        while time.time() < deadline:
            if self._cancel_event.is_set() and not cancel_sent:
                try:
                    with self._call("cancel_capture(loop)", session=session_id, seq=seq, capture=capture_id):
                        self.api_client.cancel_capture(capture_id)
                except Exception as exc:  # noqa: BLE001
                    log.warning("cancel request failed for %s: %s", capture_id, exc)
                cancel_sent = True
 
            poll_n += 1
            with self._call("get_capture", session=session_id, seq=seq, capture=capture_id, poll=poll_n):
                record = self.api_client.get_capture(capture_id)
            if record.get("status") in TERMINAL_CAPTURE_STATES:
                return record
            time.sleep(2)
 
        raise AstroError(f"capture {capture_id} timed out")
 
 
def parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
 
 
def utc_now_iso() -> str:
    return datetime.utcnow().isoformat(timespec="seconds") + "Z"
 
 
def row_to_session(row: sqlite3.Row) -> dict[str, Any]:
    config = {}
    try:
        config = json.loads(row["config_json"]) if row["config_json"] else {}
    except json.JSONDecodeError:
        config = {}
 
    return {
        "id": row["id"],
        "status": row["status"],
        "session_name": row["session_name"],
        "session_slug": row["session_slug"],
        "sequence_type": row["sequence_type"],
        "config": config,
        "output_dir": row["output_dir"],
        "total": row["total"],
        "completed": row["completed"],
        "started_at": row["started_at"],
        "paused_at": row["paused_at"],
        "completed_at": row["completed_at"],
        "current_capture_id": row["current_capture_id"],
        "current_capture_started_at": row["current_capture_started_at"],
        "error": row["error"],
        "created_at": row["created_at"],
    }
 
 
def slugify(value: str) -> str:
    value = (value or "").strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or "session"
 
 
from datetime import datetime, timedelta
from pathlib import Path
 
# Assuming these are defined elsewhere in your module
# from slugify import slugify
# SESSIONS_BASE_DIR = Path("/path/to/sessions")
 
def get_session_date(dt: datetime | None = None) -> str:
    """
    Returns the session date string (YYYYMMDD) for a given datetime.
    
    Session Definition:
    - Starts at 17:00 (5 PM)
    - Ends at 11:00 (11 AM) the next day
    - The label corresponds to the *starting* date of that window.
    
    Examples:
    - 2026-07-02 16:59 -> "20260701" (Previous night's session)
    - 2026-07-02 17:00 -> "20260702" (Tonight's session starts)
    - 2026-07-03 10:59 -> "20260702" (Still tonight's session)
    - 2026-07-03 11:00 -> "20260703" (Daytime / Next session window)
    """
    if dt is None:
        dt = datetime.now()
    
    # If before 11 AM, we belong to the session that started yesterday at 5 PM
    if dt.hour < 15:
        session_start_date = dt.date() - timedelta(days=1)
    # If 5 PM or later, we belong to the session starting today at 5 PM
    elif dt.hour >= 17:
        session_start_date = dt.date()
    # Gap period (11:00 - 16:59): 
    # Technically between sessions. We assign it to the *upcoming* night's session date
    # so morning runs (before 11) and evening setup (after 5) share the same folder 
    # if the user works continuously, but distinct from the previous morning.
    else:
        session_start_date = dt.date()
 
    return session_start_date.strftime("%Y%m%d")
 
 
def make_session_dir(session_name: str, sequence_type: str) -> tuple[Path, str]:
    """
    Creates/returns the capture directory for a session.
    
    Guarantees the same Path is returned for the same `session_name` 
    if called within the same 5PM-11AM session window.
    """
    # 1. Determine the session date string (e.g., "20260702")
    timestamp = get_session_date()
    
    # 2. Create consistent slug
    slug = slugify(session_name)
    
    # 3. Construct root dir: SESSIONS_BASE_DIR / "20260702_mysession"
    root_dir = SESSIONS_BASE_DIR / f"{timestamp}_{slug}"
    
    # 4. Construct capture subdir: ... / "lights" or "darks" etc.
    capture_dir = root_dir / sequence_type
    
    # 5. Ensure directory exists
    capture_dir.mkdir(parents=True, exist_ok=True)
    
    return capture_dir, f"{timestamp}_{slug}"
 
 
def choose_local_filename(seq: int, capture_id: str, source_name: str) -> str:
    if source_name:
        safe_name = Path(source_name).name
        return f"{seq:04d}_{safe_name}"
    return f"{seq:04d}_{capture_id}.bin"
 
 
def maybe_make_thumbnail(source_path: Path, thumbs_dir: Path) -> Optional[Path]:
    suffix = source_path.suffix.lower()
    if suffix not in {".jpg", ".jpeg", ".png", ".webp"}:
        return None
    try:
        from PIL import Image  # type: ignore
    except Exception:  # noqa: BLE001
        return None
 
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    thumb_path = thumbs_dir / f"{source_path.stem}.jpg"
    try:
        with Image.open(source_path) as img:
            img.thumbnail((480, 480))
            img.convert("RGB").save(thumb_path, "JPEG", quality=84)
        return thumb_path
    except Exception as exc:  # noqa: BLE001
        log.warning("thumbnail generation failed for %s: %s", source_path, exc)
        return None
 
 
def estimate_schedule(
    mode: str,
    now_epoch: float,
    exposure_seconds: int,
    interval_seconds: int,
    num_photos: Optional[int],
    target_end_time: Optional[str],
) -> dict[str, Any]:
    if exposure_seconds <= 0:
        raise AstroError("exposure_seconds must be greater than zero")
    if interval_seconds < 0:
        raise AstroError("interval_seconds must be zero or greater")
 
    if mode == "end_time":
        if not target_end_time:
            raise AstroError("target_end_time is required for end_time mode")
        try:
            dt = datetime.fromisoformat(target_end_time)
        except ValueError as exc:
            raise AstroError("target_end_time must be ISO datetime-local format") from exc
 
        target_epoch = dt.timestamp()
        available_seconds = max(0, int(target_epoch - now_epoch))
        per_cycle = exposure_seconds + interval_seconds
        achievable = (available_seconds + interval_seconds) // per_cycle if per_cycle > 0 else 0
        achievable = max(0, int(achievable))
        duration = achievable * exposure_seconds + max(0, achievable - 1) * interval_seconds
        return {
            "mode": "end_time",
            "num_photos": achievable,
            "estimated_duration_s": duration,
            "estimated_end": datetime.fromtimestamp(now_epoch + duration).isoformat(timespec="seconds"),
            "target_end": dt.isoformat(timespec="seconds"),
        }
 
    count = num_photos or 0
    if count < 1:
        raise AstroError("num_photos must be at least 1")
 
    duration = count * exposure_seconds + max(0, count - 1) * interval_seconds
    return {
        "mode": "count",
        "num_photos": count,
        "estimated_duration_s": duration,
        "estimated_end": datetime.fromtimestamp(now_epoch + duration).isoformat(timespec="seconds"),
        "target_end": None,
    }
 
 
def build_capture_request(config: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "shutter_speed": config.get("shutter_speed") or "bulb",
        "exposure_seconds": int(config["exposure_seconds"]),
        "capture_target": config.get("capture_target") or "sdram",
    }
 
    optional_fields = [
        "iso"
    ]
    for field in optional_fields:
        value = config.get(field)
        if value not in (None, ""):
            payload[field] = value
 
    if "iso" in payload:
        payload["iso"] = str(payload["iso"])
 
    return payload
 
 
def disk_stats(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    captures_total = 0
    if SESSIONS_BASE_DIR.exists():
        for entry in SESSIONS_BASE_DIR.rglob("*"):
            if entry.is_file():
                captures_total += entry.stat().st_size
 
    return {
        "total_gb": round(usage.total / (1024 ** 3), 2),
        "used_gb": round(usage.used / (1024 ** 3), 2),
        "free_gb": round(usage.free / (1024 ** 3), 2),
        "captures_dir_gb": round(captures_total / (1024 ** 3), 3),
    }
 
 
app = Flask(__name__, static_folder="static", template_folder="templates")
api_client = NikonApiClient(GPHOTO_API_BASE)
db = AstroDb(DB_PATH)
session_manager = SessionManager(db, api_client)
STARTED_AT = time.time()
 
 
@app.route("/")
def index() -> str:
    return render_template("index.html", gphoto_base=GPHOTO_API_BASE)
 
 
@app.route("/session/api/estimate", methods=["POST"])
def session_estimate() -> tuple[Any, int] | Any:
    data = request.get_json(silent=True) or {}
    mode = (data.get("mode") or "count").strip().lower()
    try:
        estimate = estimate_schedule(
            mode=mode,
            now_epoch=time.time(),
            exposure_seconds=parse_int(data.get("exposure_seconds"), 0),
            interval_seconds=parse_int(data.get("interval_seconds"), 0),
            num_photos=parse_int(data.get("num_photos"), 0),
            target_end_time=data.get("target_end_time"),
        )
        return jsonify(estimate)
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 400
 
 
@app.route("/session/api/start", methods=["POST"])
def session_start() -> tuple[Any, int] | Any:
    data = request.get_json(silent=True) or {}
 
    session_name = (data.get("session_name") or "").strip()
    sequence_type = (data.get("sequence_type") or "lights").strip().lower()
    mode = (data.get("mode") or "count").strip().lower()
    exposure_seconds = parse_int(data.get("exposure_seconds"), 0)
    interval_seconds = parse_int(data.get("interval_seconds"), 0)
 
    if not session_name:
        return jsonify({"error": "session_name is required"}), 400
    if sequence_type not in SEQUENCE_TYPES:
        return jsonify({"error": "sequence_type must be one of lights,darks,flats,biases"}), 400
 
    try:
        estimate = estimate_schedule(
            mode=mode,
            now_epoch=time.time(),
            exposure_seconds=exposure_seconds,
            interval_seconds=interval_seconds,
            num_photos=parse_int(data.get("num_photos"), 0),
            target_end_time=data.get("target_end_time"),
        )
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 400
 
    total = int(estimate["num_photos"])
    if total < 1:
        return jsonify({"error": "configured values result in zero captures"}), 400
 
    config = {
        "session_name": session_name,
        "sequence_type": sequence_type,
        "mode": mode,
        "num_photos": total,
        "target_end_time": data.get("target_end_time"),
        "exposure_seconds": exposure_seconds,
        "interval_seconds": interval_seconds,
        "iso": data.get("iso"),
        "aperture": data.get("aperture"),
        "color_space": data.get("color_space"),
        "image_format": data.get("image_format"),
        "capture_target": data.get("capture_target"),
        "exposure_program": data.get("exposure_program"),
        "auto_recover_usb": data.get("auto_recover_usb"),
        "shutter_speed": data.get("shutter_speed") or "bulb",
    }
 
    try:
        session = session_manager.start(config, total=total)
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 409
 
    return jsonify({"ok": True, "session": session, "estimate": estimate})
 
 
@app.route("/session/api/pause", methods=["POST"])
def session_pause() -> tuple[Any, int] | Any:
    try:
        session_manager.pause()
        return jsonify({"ok": True})
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 400
 
 
@app.route("/session/api/resume", methods=["POST"])
def session_resume() -> tuple[Any, int] | Any:
    try:
        session_manager.resume()
        return jsonify({"ok": True})
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 400
 
 
@app.route("/session/api/stop", methods=["POST"])
def session_stop() -> tuple[Any, int] | Any:
    try:
        session_manager.stop()
        return jsonify({"ok": True})
    except AstroError as exc:
        return jsonify({"error": str(exc)}), 400
 
 
@app.route("/session/api/dismiss", methods=["POST"])
def session_dismiss() -> Any:
    session_manager.dismiss()
    return jsonify({"ok": True})
 
 
@app.route("/session/api/status")
def session_status() -> Any:
    session = session_manager.current_session()
    if not session:
        return jsonify({
            "status": "idle",
            "session": None,
            "captures": [],
        })
 
    captures = db.get_session_captures(session["id"], limit=200, offset=0)
    latest_capture = captures[-1] if captures else None
 
    progress = 0.0
    if session["total"] > 0:
        progress = session["completed"] / float(session["total"])
 
    eta = None
    started_at = session.get("started_at")
    if started_at and session["status"] in {"running", "paused", "canceling"}:
        try:
            started_dt = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
            if started_dt.tzinfo is None:
                started_dt = started_dt.replace(tzinfo=timezone.utc)
            elapsed = max(1, int(time.time() - started_dt.timestamp()))
            done = max(1, session["completed"])
            avg = elapsed / done
            remaining = max(0, session["total"] - session["completed"])
            eta = datetime.fromtimestamp(time.time() + remaining * avg).isoformat(timespec="seconds")
        except Exception:  # noqa: BLE001
            eta = None
 
    return jsonify(
        {
            "status": session["status"],
            "session": session,
            "captures": captures,
            "progress": progress,
            "eta": eta,
            "latest_capture": latest_capture,
        }
    )
 
 
@app.route("/gallery/api")
def gallery_api() -> Any:
    session_id = request.args.get("session_id")
    limit = parse_int(request.args.get("limit"), 100)
    offset = parse_int(request.args.get("offset"), 0)
 
    if session_id:
        captures = db.get_session_captures(session_id, limit=limit, offset=offset)
    else:
        latest = db.get_latest_session()
        captures = db.get_session_captures(latest["id"], limit=limit, offset=offset) if latest else []
 
    return jsonify({"items": captures})
 
 
@app.route("/gallery/thumb/<capture_id>")
def gallery_thumb(capture_id: str) -> Any:
    cap = db.get_capture(capture_id)
    if not cap or not cap.get("thumb_path"):
        return jsonify({"error": "thumbnail not available"}), 404
    path = Path(cap["thumb_path"])
    if not path.exists():
        return jsonify({"error": "thumbnail file missing"}), 404
    return send_file(path)
 
 
@app.route("/gallery/full/<capture_id>")
def gallery_full(capture_id: str) -> Any:
    cap = db.get_capture(capture_id)
    if not cap or not cap.get("local_path"):
        return jsonify({"error": "capture file not available"}), 404
    path = Path(cap["local_path"])
    if not path.exists():
        return jsonify({"error": "capture file missing"}), 404
    return send_file(path, as_attachment=False)
 
 
@app.route("/history/api")
def history_api() -> Any:
    limit = parse_int(request.args.get("limit"), 50)
    offset = parse_int(request.args.get("offset"), 0)
    sessions = db.list_sessions(limit=limit, offset=offset)
    return jsonify({"items": sessions})
 
 
@app.route("/history/api/<session_id>")
def history_detail(session_id: str) -> Any:
    session = db.get_session(session_id)
    if not session:
        return jsonify({"error": "session not found"}), 404
    captures = db.get_session_captures(session_id, limit=2000, offset=0)
    return jsonify({"session": session, "captures": captures})
 
 
_camera_health_cache_lock = threading.Lock()
_camera_health_cache: dict[str, Any] = {"expires_at": 0.0, "state": None}
CAMERA_HEALTH_CACHE_TTL_S = 5.0
 
 
def get_camera_health() -> dict[str, Any]:
    """Query (or return a cached) camera health snapshot.
 
    Camera hardware behind the gphoto2 bridge is effectively single-consumer
    — issuing a health check while a real capture is in flight can conflict
    with it ("camera is busy with another operation"). Two safeguards:
 
    1. Callers should skip this entirely while a session is actively
       capturing (see health_api below) and infer reachability from that.
    2. When idle, this result is cached for a few seconds so that any
       number of devices/tabs polling /health/api concurrently collapse
       into a single upstream hardware query instead of each issuing
       their own.
    """
    now = time.time()
    with _camera_health_cache_lock:
        cached = _camera_health_cache
        if cached["state"] is not None and now < cached["expires_at"]:
            return cached["state"]
 
    try:
        with logged_camera_call("health"):
            camera = api_client.health()
        state = {
            "reachable": True,
            "model": camera.get("camera_model"),
            "status": camera.get("status", "ok"),
        }
    except ApiFailure as exc:
        state = {"reachable": False, "model": None, "status": "error", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        state = {"reachable": False, "model": None, "status": "error", "error": str(exc)}
 
    with _camera_health_cache_lock:
        _camera_health_cache["state"] = state
        _camera_health_cache["expires_at"] = now + CAMERA_HEALTH_CACHE_TTL_S
    return state
 
 
@app.route("/health/api")
def health_api() -> tuple[Any, int] | Any:
    session = session_manager.current_session()
    is_capturing = bool(session and session["status"] in {"running", "paused", "canceling"})
 
    if is_capturing:
        # A session already owns the camera and is actively proving it's
        # reachable via real captures — skip the redundant hardware round
        # trip so N devices watching the same session never compete with
        # it for camera access.
        camera_state = {"reachable": True, "model": None, "status": "capturing"}
        status_code = 200
    else:
        camera_state = get_camera_health()
        status_code = 200 if camera_state["reachable"] else 503
 
    payload = {
        "camera": camera_state,
        "disk": disk_stats(OUTPUT_BASE_DIR),
        "current_session": session,
        "server_uptime_s": int(time.time() - STARTED_AT),
    }
    return jsonify(payload), status_code
 
 
@app.route("/health/recover", methods=["POST"])
def health_recover() -> tuple[Any, int] | Any:
    session = session_manager.current_session()
    is_capturing = bool(session and session["status"] in {"running", "paused", "canceling"})
    if is_capturing:
        log.warning(
            "recover requested while session %s is %s — this WILL conflict with the "
            "active capture; the camera bridge can only serve one operation at a time",
            session["id"], session["status"],
        )
    try:
        with logged_camera_call("recover", session=session["id"] if session else None):
            return jsonify(api_client.recover())
    except ApiFailure as exc:
        return jsonify(exc.payload), exc.status_code
    except Exception as exc:  # noqa: BLE001
        return jsonify({"error": str(exc)}), 503
 
 
@app.route("/camera/capabilities")
def camera_capabilities() -> tuple[Any, int] | Any:
    try:
        return jsonify(api_client.capabilities())
    except ApiFailure as exc:
        return jsonify(exc.payload), exc.status_code
 
 
@app.route("/platesolve")
def platesolve_page() -> str:
    return render_template("platesolve.html")
 



@app.route("/platesolve/api/start", methods=["POST"])
def platesolve_start() -> tuple[Any, int] | Any:
    """Start a plate-solving task: capture 1 frame + solve + annotate."""
    data = request.get_json(silent=True) or {}
    target_name = (data.get("target_name") or "").strip()
    exposure_seconds = parse_int(data.get("exposure_seconds"), 30)
    iso = (data.get("iso") or "400").strip()
    search_radius_deg = float(data.get("search_radius_deg") or 1.5)

    if not target_name:
        return jsonify({"error": "target_name is required (e.g. M31, NGC7000)"}), 400
    if exposure_seconds < 1:
        return jsonify({"error": "exposure_seconds must be >= 1"}), 400

    try:
        if use_siril_backend:
            task_id = siril_annotate.start_platesolve_siril(
                target_name=target_name,
                exposure_seconds=exposure_seconds,
                iso=iso,
                search_radius_deg=search_radius_deg,
            )
        else:
            task_id = platesolve.start_platesolve(
                target_name=target_name,
                exposure_seconds=exposure_seconds,
                iso=iso,
                search_radius_deg=search_radius_deg,
            )
        return jsonify({"ok": True, "task_id": task_id})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/platesolve/api/upload", methods=["POST"])
def platesolve_upload() -> tuple[Any, int] | Any:
    """Upload a file and plate-solve it (no live capture)."""
    target_name = (request.form.get("target_name") or "").strip()
    search_radius_deg = float(request.form.get("search_radius_deg") or 1.5)

    if not target_name:
        return jsonify({"error": "target_name is required"}), 400

    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    f = request.files["file"]
    if not f.filename:
        return jsonify({"error": "Empty filename"}), 400

    # Save uploaded file to aoutput_png_path temp location
    upload_dir = OUTPUT_BASE_DIR / "platesolve_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    safe_name = Path(f.filename).name
    dest = upload_dir / f"{uuid.uuid4().hex}_{safe_name}"
    f.save(str(dest))

    try:
        if use_siril_backend:
            task_id = siril_annotate.start_platesolve_from_file_siril(
                file_path=str(dest),
                target_name=target_name,
                search_radius_deg=search_radius_deg,
            )
        else:
            task_id = platesolve.start_platesolve_from_file(
                file_path=str(dest),
                target_name=target_name,
                search_radius_deg=search_radius_deg,
            )
        return jsonify({"ok": True, "task_id": task_id, "filename": safe_name})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.route("/platesolve/api/status/<task_id>")
def platesolve_status(task_id: str) -> Any:
    if use_siril_backend:
        result = siril_annotate.get_task_result(task_id)
    else:
        result = platesolve.get_task_result(task_id)
    if result is None:
        return jsonify({"error": "task not found"}), 404
    return jsonify(result)


@app.route("/platesolve/api/annotated/<task_id>")
def platesolve_annotated(task_id: str) -> Any:
    if use_siril_backend:
        result = siril_annotate.get_task_result(task_id)
    else:
        result = platesolve.get_task_result(task_id)
    if not result or not result.get("annotated_path"):
        return jsonify({"error": "annotated image not available"}), 404
    path = Path(result["annotated_path"])
    if not path.exists():
        return jsonify({"error": "annotated file missing"}), 404
    return send_file(path, mimetype="image/png")


@app.route("/platesolve/api/capture_raw/<task_id>")
def platesolve_capture_raw(task_id: str) -> Any:
    if use_siril_backend:
        result = siril_annotate.get_task_result(task_id)
    else:
        result = platesolve.get_task_result(task_id)
    if not result or not result.get("local_path"):
        return jsonify({"error": "capture file not available"}), 404
    path = Path(result["local_path"])
    if not path.exists():
        return jsonify({"error": "capture file missing"}), 404
    return send_file(path)


if __name__ == "__main__":
    OUTPUT_BASE_DIR.mkdir(parents=True, exist_ok=True)
    SESSIONS_BASE_DIR.mkdir(parents=True, exist_ok=True)
    if "--siril" in sys.argv:
        use_siril_backend = True
        print("Using Siril backend for plate-solving and annotation")
    print(
        f"""
AstroCap listening on http://0.0.0.0:{SERVER_PORT}
Remote backend: {GPHOTO_API_BASE}
Capture storage: {SESSIONS_BASE_DIR}
SQLite DB: {DB_PATH}
"""
    )

    app.run(host="0.0.0.0", port=SERVER_PORT, debug=False, threaded=True)
