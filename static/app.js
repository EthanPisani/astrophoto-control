/* =============================================================
 * AstroCap — Session Control  (front-end controller)
 * =============================================================
 *
 * Single-page controller for the setup + active session panels.
 * Talks to the Flask backend at /session/api/* and /health/api.
 *
 * The state machine here mirrors the server's SessionManager.
 * Whenever a server status comes back, we *fully* recompute the
 * rendered UI from that status — we never trust in-memory flags
 * across pages, polls, or recoveries. This is what makes the UI
 * robust against "stuck on canceling" / "stuck on paused" /
 * "session vanished after backend restart" kinds of edge cases:
 * the server's view of the world always wins.
 *
 * Status taxonomy (server side):
 *   idle / configured   → show setup panel
 *   running | paused    → show active panel with progress
 *   canceling           → show active panel, lock controls,
 *                          poll fast so we can flip to idle ASAP
 *   done | canceled | error → show active panel in "finished"
 *                              state with a "New Session" CTA
 * ============================================================= */

(() => {
  "use strict";

  // -----------------------------------------------------------------
  // Element lookups
  // -----------------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);

  const els = {
    // panels
    setupPanel: $("#setupPanel"),
    activePanel: $("#activePanel"),

    // topbar
    cameraPill: $("#cameraPill"),
    cameraDot: $("#cameraDot"),
    cameraLabel: $("#cameraLabel"),
    nvToggle: $("#nvToggle"),

    // form
    setupForm: $("#setupForm"),
    sessionName: $("#sessionName"),
    seqGrid: $("#seqGrid"),
    fieldShutterPreset: $("#fieldShutterPreset"),
    shutterPreset: $("#shutterPreset"),
    fieldAutoFlatExposure: $("#fieldAutoFlatExposure"),
    autoFlatExposureBtn: $("#autoFlatExposureBtn"),
    autoFlatExposureStatus: $("#autoFlatExposureStatus"),
    exposureSeconds: $("#exposureSeconds"),
    intervalSeconds: $("#intervalSeconds"),
    iso: $("#iso"),
    aperture: $("#aperture"),
    modeSegmented: $("#modeSegmented"),
    fieldNumPhotos: $("#fieldNumPhotos"),
    fieldEndTime: $("#fieldEndTime"),
    numPhotos: $("#numPhotos"),
    targetEndTime: $("#targetEndTime"),
    estimate: $("#estimate"),

    // advanced
    colorSpace: $("#colorSpace"),
    imageFormat: $("#imageFormat"),
    captureTarget: $("#captureTarget"),
    shutterSpeed: $("#shutterSpeed"),
    exposureProgram: $("#exposureProgram"),
    autoRecoverUsb: $("#autoRecoverUsb"),

    startBtn: $("#startBtn"),

    // active panel
    activeSessionName: $("#activeSessionName"),
    activeSeqBadge: $("#activeSeqBadge"),
    statusPill: $("#statusPill"),
    statusLabel: $("#statusLabel"),
    errorBanner: $("#errorBanner"),
    completedCount: $("#completedCount"),
    totalCount: $("#totalCount"),
    progressPct: $("#progressPct"),
    meterFill: $("#meterFill"),
    exposureCountdown: $("#exposureCountdown"),
    miniMeterFill: $("#miniMeterFill"),
    teleElapsed: $("#teleElapsed"),
    teleEta: $("#teleEta"),
    teleExposure: $("#teleExposure"),
    teleIso: $("#teleIso"),
    teleInterval: $("#teleInterval"),
    teleErrors: $("#teleErrors"),
    lastCapture: $("#lastCapture"),

    pauseResumeBtn: $("#pauseResumeBtn"),
    restartBtn: $("#restartBtn"),
    cancelBtn: $("#cancelBtn"),
    newSessionBtn: $("#newSessionBtn"),

    // confirm dialog
    confirmBackdrop: $("#confirmBackdrop"),
    confirmTitle: $("#confirmTitle"),
    confirmBody: $("#confirmBody"),
    confirmCancelBtn: $("#confirmCancelBtn"),
    confirmCancelStayBtn: $("#confirmCancelStayBtn"),
    confirmCancelNewSessionBtn: $("#confirmCancelNewSessionBtn"),

    // toast
    toast: $("#toast"),
  };

  // -----------------------------------------------------------------
  // Local UI state
  // -----------------------------------------------------------------
  const state = {
    sequenceType: "lights",     // lights | flats | biases | darks
    mode: "count",              // count | end_time
    estimateDebounceId: 0,
    pollTimerId: 0,
    pollIntervalMs: 3000,       // faster when active, slower when idle
    lastStatus: null,           // last server status payload
    lastSession: null,          // last seen session object
    lastSeqBadge: "",           // track sequence type for the badge
    isStarting: false,          // prevent double-start while request in flight
    confirmAction: null,        // "cancel" | "cancelAndNew" | null
    toastTimerId: 0,
    liveTickerId: 0,            // single persistent local clock, independent of polling
  };

  // -----------------------------------------------------------------
  // Utility: format helpers
  // -----------------------------------------------------------------
  const pad2 = (n) => String(n).padStart(2, "0");

  function formatHMS(totalSeconds) {
    totalSeconds = Math.max(0, Math.floor(totalSeconds || 0));
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    return h > 0 ? `${h}h ${pad2(m)}m` : `${m}m ${pad2(s)}s`;
  }

  function formatClock(totalSeconds) {
    totalSeconds = Math.max(0, Math.floor(totalSeconds || 0));
    const h = Math.floor(totalSeconds / 3600);
    const m = Math.floor((totalSeconds % 3600) / 60);
    const s = totalSeconds % 60;
    return `${pad2(h)}:${pad2(m)}:${pad2(s)}`;
  }

  function formatLocalDateTime(isoString) {
    if (!isoString) return "—";
    // Only strings with a trailing "Z" are true UTC and must keep it so
    // Date parses them as UTC (then converts to the browser's local
    // timezone for display). Strings without "Z" (e.g. the server's
    // `eta` field) are already naive server-local time and are parsed
    // as local time as-is, which is correct.
    const dt = new Date(isoString);
    if (Number.isNaN(dt.getTime())) return isoString;
    return dt.toLocaleString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      month: "short",
      day: "numeric",
    });
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  // Fastest shutter speed we accept — matches the D5300 (and most
  // consumer DSLRs) top electronic/mechanical shutter speed. Keep in
  // sync with MIN_EXPOSURE_SECONDS in app.py.
  const MIN_EXPOSURE_SECONDS = 1 / 4000;

  // Parses an exposure value that may be a plain decimal ("0.25"), a
  // whole number ("240"), or photographic fraction notation ("1/4000"),
  // which is how fast bias-frame shutter speeds get entered.
  function parseExposureInput(raw) {
    const text = String(raw ?? "").trim();
    if (!text) return 0;
    if (text.includes("/")) {
      const [numStr, denStr] = text.split("/", 2);
      const num = Number(numStr);
      const den = Number(denStr);
      if (!Number.isFinite(num) || !Number.isFinite(den) || den === 0) return 0;
      return num / den;
    }
    const val = Number(text);
    return Number.isFinite(val) ? val : 0;
  }

  // Renders an exposure duration the way a photographer would expect to
  // read it: fraction notation under 1s ("1/4000s"), otherwise seconds.
  function formatExposureSeconds(seconds) {
    const sec = Number(seconds) || 0;
    if (sec <= 0) return "—";
    if (sec >= 1) {
      // Trim trailing zeros on decimals (e.g. "4.50" -> "4.5").
      const trimmed = Number(sec.toFixed(3)).toString();
      return `${trimmed}s`;
    }
    const denominator = Math.round(1 / sec);
    return `1/${denominator}s`;
  }

  // -----------------------------------------------------------------
  // Toast notifications
  // -----------------------------------------------------------------
  function showToast(message, kind = "info", durationMs = 3200) {
    if (!els.toast) return;
    clearTimeout(state.toastTimerId);
    els.toast.textContent = message;
    els.toast.classList.remove("hidden", "is-error", "is-ok");
    if (kind === "error") els.toast.classList.add("is-error");
    if (kind === "ok") els.toast.classList.add("is-ok");
    state.toastTimerId = setTimeout(() => {
      els.toast.classList.add("hidden");
    }, durationMs);
  }

  // -----------------------------------------------------------------
  // Night Vision toggle
  // -----------------------------------------------------------------
  function initNightVision() {
    if (!els.nvToggle) return;
    const stored = localStorage.getItem("astrocap.nv") === "1";
    if (stored) {
      document.documentElement.classList.add("nv");
      els.nvToggle.setAttribute("aria-pressed", "true");
    }
    els.nvToggle.addEventListener("click", () => {
      const on = !document.documentElement.classList.contains("nv");
      document.documentElement.classList.toggle("nv", on);
      els.nvToggle.setAttribute("aria-pressed", on ? "true" : "false");
      localStorage.setItem("astrocap.nv", on ? "1" : "0");
    });
  }

  // -----------------------------------------------------------------
  // Sequence-type selector (lights / flats / biases / darks)
  // -----------------------------------------------------------------
  // Sensible default exposure when switching into a frame type, but only
  // if the user hasn't already typed something for that type — we don't
  // want to stomp on a value someone is mid-edit on.
  const SEQ_DEFAULT_EXPOSURE = {
    lights: "240",
    darks: "240",
    flats: "0.01",
    biases: "1/4000",
  };

  function initSequenceButtons() {
    if (!els.seqGrid) return;
    const buttons = els.seqGrid.querySelectorAll(".seq-btn");
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const seq = btn.getAttribute("data-seq");
        if (!seq) return;
        const previousSeq = state.sequenceType;
        state.sequenceType = seq;
        buttons.forEach((b) =>
          b.setAttribute("aria-pressed", b === btn ? "true" : "false")
        );
        syncShutterPresetVisibility();
        // Biases are always shot at a fixed fast shutter speed, so jump
        // straight to the currently-selected preset. For other frame
        // types, only apply the default if the field still holds the
        // default for the type we're leaving (i.e. the user hasn't
        // customized it).
        if (seq === "biases") {
          applyShutterPreset(els.shutterPreset?.value || "1/4000");
        } else if (
          els.exposureSeconds &&
          (els.exposureSeconds.value === "" ||
            els.exposureSeconds.value === SEQ_DEFAULT_EXPOSURE[previousSeq])
        ) {
          els.exposureSeconds.value = SEQ_DEFAULT_EXPOSURE[seq] || els.exposureSeconds.value;
        }
        scheduleEstimate();
      });
    });
  }

  function syncShutterPresetVisibility() {
    if (els.fieldShutterPreset) {
      els.fieldShutterPreset.classList.toggle("hidden", state.sequenceType !== "biases");
    }
    if (els.fieldAutoFlatExposure) {
      els.fieldAutoFlatExposure.classList.toggle("hidden", state.sequenceType !== "flats");
    }
  }

  // Applies a shutter-speed preset to both the exposure field (so
  // scheduling math works) and the advanced shutter-speed override (so
  // the camera actually uses its own fast shutter instead of a bulb
  // hold). "custom" leaves the exposure field alone for manual entry.
  function applyShutterPreset(value) {
    if (value === "custom") return;
    if (els.exposureSeconds) els.exposureSeconds.value = value;
    if (els.shutterSpeed) els.shutterSpeed.value = value;
  }

  function initShutterPreset() {
    if (!els.shutterPreset) return;
    els.shutterPreset.addEventListener("change", () => {
      applyShutterPreset(els.shutterPreset.value);
      scheduleEstimate();
    });
  }

  // -----------------------------------------------------------------
  // Flats: auto exposure via histogram bisection
  // -----------------------------------------------------------------
  function initAutoFlatExposure() {
    if (!els.autoFlatExposureBtn) return;
    els.autoFlatExposureBtn.addEventListener("click", runAutoFlatExposure);
  }

  async function runAutoFlatExposure() {
    if (!els.autoFlatExposureBtn) return;
    els.autoFlatExposureBtn.disabled = true;
    const originalLabel = els.autoFlatExposureBtn.textContent;
    els.autoFlatExposureBtn.textContent = "Metering…";
    if (els.autoFlatExposureStatus) {
      els.autoFlatExposureStatus.textContent = "Taking test shots — this can take a minute…";
    }

    try {
      const resp = await fetch("/session/api/auto_flat_exposure", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          target_fraction: 1 / 3,
          start_exposure: parseExposureInput(els.exposureSeconds?.value) || 1,
          iso: els.iso?.value || "400",
          aperture: (els.aperture?.value || "").trim() || null,
        }),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.error) {
        throw new Error(data.error || `calibration failed (${resp.status})`);
      }

      if (els.exposureSeconds) els.exposureSeconds.value = String(data.exposure_seconds);
      if (els.shutterSpeed) els.shutterSpeed.value = data.shutter_speed || "bulb";

      const pct = Math.round((data.peak_fraction || 0) * 100);
      if (els.autoFlatExposureStatus) {
        els.autoFlatExposureStatus.textContent = data.converged
          ? `Converged in ${data.iterations} shots — histogram peak at ${pct}% (target 33%).`
          : `Stopped after ${data.iterations} shots — closest peak at ${pct}% (target 33%). You can nudge exposure manually.`;
      }
      showToast(`Auto exposure: ${formatExposureSeconds(data.exposure_seconds)}`, data.converged ? "ok" : "info");
      scheduleEstimate();
    } catch (err) {
      if (els.autoFlatExposureStatus) {
        els.autoFlatExposureStatus.textContent = `⚠ ${err.message || err}`;
      }
      showToast(`Auto exposure failed: ${err.message || err}`, "error");
    } finally {
      els.autoFlatExposureBtn.disabled = false;
      els.autoFlatExposureBtn.textContent = originalLabel;
    }
  }

  // -----------------------------------------------------------------
  // Schedule mode segmented control (count | end_time)
  // -----------------------------------------------------------------
  function initModeSegmented() {
    if (!els.modeSegmented) return;
    const buttons = els.modeSegmented.querySelectorAll(".seg-btn");
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.getAttribute("data-mode");
        if (!mode) return;
        state.mode = mode;
        buttons.forEach((b) =>
          b.setAttribute("aria-pressed", b === btn ? "true" : "false")
        );
        syncScheduleModeFields();
        // re-evaluate defaults for end_time mode
        if (state.mode === "end_time") primeEndTimeDefault();
        scheduleEstimate();
      });
    });
  }

  function syncScheduleModeFields() {
    if (!els.fieldNumPhotos || !els.fieldEndTime) return;
    if (state.mode === "end_time") {
      els.fieldNumPhotos.classList.add("hidden");
      els.fieldEndTime.classList.remove("hidden");
    } else {
      els.fieldNumPhotos.classList.remove("hidden");
      els.fieldEndTime.classList.add("hidden");
    }
  }

  // When user switches to "end time" mode, propose a sensible default
  // (now + 60 min) so the estimate is immediately meaningful.
  function primeEndTimeDefault() {
    if (!els.targetEndTime) return;
    if (els.targetEndTime.value) return; // user already typed something
    const dt = new Date(Date.now() + 60 * 60 * 1000);
    // datetime-local needs YYYY-MM-DDTHH:MM in *local* time
    const isoLocal =
      dt.getFullYear() + "-" +
      pad2(dt.getMonth() + 1) + "-" +
      pad2(dt.getDate()) + "T" +
      pad2(dt.getHours()) + ":" +
      pad2(dt.getMinutes());
    els.targetEndTime.value = isoLocal;
  }

  // -----------------------------------------------------------------
  // Form -> JSON payload (matches what app.py /session/api/start expects)
  // -----------------------------------------------------------------
  function readFormConfig() {
    const cfg = {
      session_name: (els.sessionName?.value || "").trim(),
      sequence_type: state.sequenceType,
      mode: state.mode,
      exposure_seconds: parseExposureInput(els.exposureSeconds?.value),
      interval_seconds: parseInt(els.intervalSeconds?.value || "0", 10) || 0,
      iso: els.iso?.value || null,
      aperture: (els.aperture?.value || "").trim() || null,
      color_space: els.colorSpace?.value || null,
      image_format: els.imageFormat?.value || null,
      capture_target: els.captureTarget?.value || "sdram",
      exposure_program: (els.exposureProgram?.value || "").trim() || null,
      auto_recover_usb: !!els.autoRecoverUsb?.checked,
      shutter_speed: (els.shutterSpeed?.value || "bulb").trim() || "bulb",
    };

    if (state.mode === "end_time") {
      cfg.target_end_time = els.targetEndTime?.value || null;
      cfg.num_photos = null;
    } else {
      cfg.num_photos = parseInt(els.numPhotos?.value || "0", 10) || 0;
      cfg.target_end_time = null;
    }
    return cfg;
  }

  function validateFormConfig(cfg) {
    if (!cfg.session_name) return "Session name is required.";
    if (cfg.exposure_seconds <= 0) return "Exposure must be greater than 0 seconds.";
    if (cfg.exposure_seconds < MIN_EXPOSURE_SECONDS)
      return "Exposure can't be faster than the camera's fastest shutter speed (1/4000s).";
    if (cfg.interval_seconds < 0) return "Interval must be 0 or greater.";
    if (cfg.mode === "count") {
      if (!cfg.num_photos || cfg.num_photos < 1)
        return "Number of photos must be at least 1.";
    } else {
      if (!cfg.target_end_time)
        return "Please choose an end time for the session.";
    }
    return null;
  }

  // -----------------------------------------------------------------
  // Estimate (debounced POST to /session/api/estimate)
  // -----------------------------------------------------------------
  function scheduleEstimate() {
    clearTimeout(state.estimateDebounceId);
    state.estimateDebounceId = setTimeout(refreshEstimate, 280);
  }

  async function refreshEstimate() {
    const cfg = readFormConfig();
    const errMsg = validateFormConfig(cfg);
    if (errMsg) {
      renderEstimateError(errMsg);
      return;
    }

    try {
      const resp = await fetch("/session/api/estimate", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      const data = await resp.json();
      if (!resp.ok || data.error) {
        renderEstimateError(data.error || `Estimate failed (${resp.status})`);
        return;
      }
      renderEstimate(cfg, data);
    } catch (err) {
      renderEstimateError(`Network error: ${err.message || err}`);
    }
  }

  function renderEstimateError(message) {
    if (!els.estimate) return;
    els.estimate.classList.add("is-error");
    els.estimate.innerHTML = `<span>⚠ ${escapeHtml(message)}</span>`;
  }

  function renderEstimate(cfg, est) {
    if (!els.estimate) return;
    els.estimate.classList.remove("is-error");

    const numPhotos = est.num_photos ?? cfg.num_photos ?? 0;
    const duration = est.estimated_duration_s ?? 0;
    const endTime = est.estimated_end ? formatLocalDateTime(est.estimated_end) : "—";

    if (cfg.mode === "end_time") {
      // User gave an end time → tell them how many frames fit
      els.estimate.innerHTML = `
        <span>${state.sequenceType}:</span>
        <span><span class="val">${numPhotos}</span> photos achievable</span>
        <span>•</span>
        <span>~<span class="val">${formatHMS(duration)}</span> of capture time</span>
        <span>•</span>
        <span>finishes <span class="val">${escapeHtml(endTime)}</span></span>
      `;
    } else {
      // Fixed count → tell them when it ends
      els.estimate.innerHTML = `
        <span>${state.sequenceType}:</span>
        <span><span class="val">${numPhotos}</span> photos × <span class="val">${formatExposureSeconds(cfg.exposure_seconds)}</span></span>
        <span>•</span>
        <span>~<span class="val">${formatHMS(duration)}</span> total</span>
        <span>•</span>
        <span>finishes <span class="val">${escapeHtml(endTime)}</span></span>
      `;
    }
  }

  // -----------------------------------------------------------------
  // Camera health pill (top bar)
  // -----------------------------------------------------------------
  async function refreshCameraHealth() {
    try {
      const resp = await fetch("/health/api", { cache: "no-store" });
      const data = await resp.json();
      applyCameraHealth(data);
    } catch (err) {
      applyCameraHealth({ camera: { reachable: false, status: "error" } });
    }
  }

  // Camera health touches real hardware (behind the gphoto2 bridge, which
  // is effectively single-consumer), unlike /session/api/status which is
  // just a DB read. Poll it on its own slow, independent cadence — not
  // once per session-status poll — so any number of devices/tabs open at
  // once don't multiply the rate of real hardware queries. (The server
  // also short-circuits this entirely while a session is actively
  // capturing, and caches it briefly otherwise, as extra insurance.)
  const CAMERA_HEALTH_POLL_MS = 12000;
  function startCameraHealthLoop() {
    refreshCameraHealth();
    setInterval(refreshCameraHealth, CAMERA_HEALTH_POLL_MS);
  }

  function applyCameraHealth(payload) {
    if (!els.cameraPill) return;
    const cam = payload?.camera || {};
    els.cameraDot.classList.remove("ok", "bad", "pending");
    if (cam.reachable) {
      els.cameraDot.classList.add("ok");
      const model = cam.model ? `${cam.model} · connected` : "Camera connected";
      els.cameraLabel.textContent = model;
    } else {
      els.cameraDot.classList.add("bad");
      els.cameraLabel.textContent = "Camera offline";
    }
  }

  // -----------------------------------------------------------------
  // Status polling
  // -----------------------------------------------------------------
  function scheduleStatusPoll(intervalMs) {
    clearTimeout(state.pollTimerId);
    state.pollIntervalMs = intervalMs;
    state.pollTimerId = setTimeout(pollStatus, intervalMs);
  }

  async function pollStatus() {
    try {
      const resp = await fetch("/session/api/status", { cache: "no-store" });
      const data = await resp.json();
      applyStatus(data);
    } catch (err) {
      // network blip — keep the last rendered state, just try again
      console.warn("status poll failed", err);
    } finally {
      scheduleStatusPoll(state.pollIntervalMs);
    }
  }

  // -----------------------------------------------------------------
  // The single source of truth for what the UI should look like
  // right now. Re-runs on every status response.
  // -----------------------------------------------------------------
  function applyStatus(payload) {
    state.lastStatus = payload;
    const status = payload?.status || "idle";
    const session = payload?.session || null;
    state.lastSession = session;

    // Camera pill is refreshed on its own independent, slower cadence
    // (see startCameraHealthLoop) — it's a separate, hardware-touching
    // endpoint and shouldn't be re-queried on every single status poll
    // from every open device just because a session is being watched.

    if (status === "idle" || !session) {
      // No active session: show the setup panel. This is what recovers
      // us from a "stuck canceling" — once the server finishes cancel,
      // its next status response says idle and we drop back here.
      showSetup();
      // slow down polling when idle
      scheduleStatusPoll(8000);
      return;
    }

    // We have a session. Decide which panel + which controls.
    showActive();

    const isTerminal =
      status === "done" || status === "canceled" || status === "error" || status === "complete";
    const isLive = status === "running" || status === "paused" || status === "canceling";
    const isPaused = status === "paused";
    const isCanceling = status === "canceling";

    renderActiveHeader(session, status);
    renderProgress(session, payload);
    renderTelemetry(session, payload, status);
    renderLastCapture(payload.latest_capture);
    renderErrorBanner(session);
    renderControls({ status, isTerminal, isLive, isPaused, isCanceling });

    // Polling cadence: fast when live, medium when terminal so we
    // eventually notice if a new session starts in another tab/device.
    if (isCanceling) {
      scheduleStatusPoll(1000);   // poll hard so we catch the terminal flip
    } else if (isLive) {
      scheduleStatusPoll(2500);
    } else if (isTerminal) {
      scheduleStatusPoll(5000);
    } else {
      scheduleStatusPoll(4000);
    }
  }

  // -----------------------------------------------------------------
  // Panel switching
  // -----------------------------------------------------------------
  function showSetup() {
    els.setupPanel?.classList.remove("hidden");
    els.activePanel?.classList.add("hidden");
  }

  function showActive() {
    els.setupPanel?.classList.add("hidden");
    els.activePanel?.classList.remove("hidden");
  }

  // -----------------------------------------------------------------
  // Render: active session header (name + sequence badge + status pill)
  // -----------------------------------------------------------------
  function renderActiveHeader(session, status) {
    if (els.activeSessionName) {
      els.activeSessionName.textContent = session.session_name || "—";
    }
    if (els.activeSeqBadge) {
      els.activeSeqBadge.textContent = session.sequence_type || "lights";
    }
    if (els.statusPill && els.statusLabel) {
      // Map any state we don't model in CSS to a known class.
      let cls = "st-running";
      let label = status;
      switch (status) {
        case "running":   cls = "st-running";   label = "running"; break;
        case "paused":    cls = "st-paused";    label = "paused"; break;
        case "canceling": cls = "st-canceling"; label = "canceling"; break;
        case "canceled":  cls = "st-canceling"; label = "canceled"; break;
        case "done":      cls = "st-done";      label = "done"; break;
        case "complete":  cls = "st-done";      label = "complete"; break;
        case "error":     cls = "st-error";     label = "error"; break;
        default:          cls = "st-running";   label = status;
      }
      els.statusPill.className = `status-pill ${cls}`;
      els.statusLabel.textContent = label;
    }
  }

  // -----------------------------------------------------------------
  // Render: progress meter + counts
  // -----------------------------------------------------------------
  function renderProgress(session, payload) {
    const total = Number(session.total) || 0;
    const done = Number(session.completed) || 0;
    const pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;

    if (els.completedCount) els.completedCount.textContent = String(done);
    if (els.totalCount) els.totalCount.textContent = ` / ${total} frames`;
    if (els.progressPct) els.progressPct.textContent = `${pct}%`;
    if (els.meterFill) els.meterFill.style.width = `${pct}%`;
  }

  // -----------------------------------------------------------------
  // Render: telemetry cells + current-exposure strip
  // -----------------------------------------------------------------
  function renderTelemetry(session, payload, status) {
    // ETA — a point-in-time value from the server; only meaningful to
    // refresh when a fresh poll comes in (no local interpolation needed).
    if (els.teleEta) {
      const eta = payload?.eta;
      if (eta) {
        els.teleEta.textContent = formatLocalDateTime(eta);
      } else if (status === "done" || status === "complete") {
        const ca = session.completed_at;
        els.teleEta.textContent = ca ? `finished ${formatLocalDateTime(ca)}` : "—";
      } else {
        els.teleEta.textContent = "—";
      }
    }

    // Exposure per frame
    if (els.teleExposure) {
      const sec = Number(session.config?.exposure_seconds) || 0;
      els.teleExposure.textContent = sec > 0 ? formatExposureSeconds(sec) : "—";
    }

    // ISO
    if (els.teleIso) {
      const iso = session.config?.iso;
      els.teleIso.textContent = iso ? `${iso}` : "—";
    }

    // Interval between exposures
    if (els.teleInterval) {
      const intervalSec = Number(session.config?.interval_seconds) || 0;
      els.teleInterval.textContent = intervalSec > 0 ? `${intervalSec}s` : "—";
    }

    // Error count from DB captures
    if (els.teleErrors) {
      const errs = (payload.captures || []).filter((c) => c.status === "error").length;
      els.teleErrors.textContent = String(errs);
      els.teleErrors.style.color = errs > 0 ? "var(--danger-bright)" : "";
    }

    // Elapsed + current-exposure mini-meter/countdown are driven by the
    // persistent local ticker (tickLiveDisplay), not by poll cadence —
    // render one immediate pass here too so there's no visible delay
    // between a poll landing and the display reflecting it.
    renderElapsed(session);
    renderExposureStrip(session, status);
  }

  // -----------------------------------------------------------------
  // Live ticker — a single persistent local clock (started once at
  // boot) that re-renders time-based UI every 250ms from whatever
  // session/status we last received from the server. This is what
  // makes the elapsed clock and exposure countdown animate smoothly
  // *between* polls, instead of only updating once per poll response.
  // -----------------------------------------------------------------
  function tickLiveDisplay() {
    const payload = state.lastStatus;
    const session = state.lastSession;
    const status = payload?.status;
    if (!session || !status) return;

    const isLive = status === "running" || status === "paused" || status === "canceling";
    if (!isLive) return;

    renderElapsed(session);
    renderExposureStrip(session, status);
  }

  function renderElapsed(session) {
    if (!els.teleElapsed) return;
    const startedAt = session.started_at;
    if (startedAt) {
      // started_at is a true UTC timestamp (e.g. "...T14:15:00Z").
      // Do NOT strip the "Z" — Date needs it to parse as UTC instead
      // of local time, or the elapsed time comes out wrong (often
      // negative, which then gets clamped to 0) for anyone not at UTC+0.
      const t0 = new Date(startedAt).getTime();
      const elapsed = Math.max(0, Math.floor((Date.now() - t0) / 1000));
      els.teleElapsed.textContent = formatClock(elapsed);
    } else {
      els.teleElapsed.textContent = "—";
    }
  }

  function renderExposureStrip(session, status) {
    const exposure = Number(session.config?.exposure_seconds) || 0;
    const startedAt = session.current_capture_started_at;

    // Sub-second exposures (bias frames) finish faster than any
    // meaningful UI countdown could track, so just show a static
    // "capturing" state instead of a misleading/flickering timer.
    if (status === "running" && exposure > 0 && exposure < 1 && startedAt) {
      if (els.miniMeterFill) els.miniMeterFill.style.width = "100%";
      if (els.exposureCountdown)
        els.exposureCountdown.textContent = `capturing (${formatExposureSeconds(exposure)})`;
      return;
    }

    if (status === "running" && exposure > 0 && startedAt) {
      // Same UTC-parsing rule as renderElapsed above.
      const t0 = new Date(startedAt).getTime();
      const elapsed = Math.max(0, (Date.now() - t0) / 1000);
      const pct = Math.min(100, (elapsed / exposure) * 100);
      const remaining = Math.max(0, Math.ceil(exposure - elapsed));

      if (els.miniMeterFill) els.miniMeterFill.style.width = `${pct}%`;
      if (els.exposureCountdown)
        els.exposureCountdown.textContent = `${formatClock(remaining)} remaining`;
    } else {
      if (els.miniMeterFill) els.miniMeterFill.style.width = "0%";
      if (els.exposureCountdown) {
        if (status === "paused") els.exposureCountdown.textContent = "paused";
        else if (status === "canceling") els.exposureCountdown.textContent = "canceling…";
        else if (status === "done" || status === "complete")
          els.exposureCountdown.textContent = "session complete";
        else if (status === "canceled") els.exposureCountdown.textContent = "canceled";
        else if (status === "error") els.exposureCountdown.textContent = "error";
        else els.exposureCountdown.textContent = "—";
      }
    }
  }

  // -----------------------------------------------------------------
  // Render: last capture
  // -----------------------------------------------------------------
  function renderLastCapture(latest) {
    if (!els.lastCapture) return;
    if (!latest) {
      els.lastCapture.innerHTML = `<span>No frames captured yet</span>`;
      return;
    }
    const ok = latest.status === "ok";
    const dotClass = ok ? "ok-dot" : "bad-dot";
    const mark = ok ? "●" : "✕";
    const seq = latest.seq != null ? `#${String(latest.seq).padStart(4, "0")}` : "";
    const name = latest.source_name || latest.id || "—";
    const size = latest.size_bytes
      ? `${(latest.size_bytes / (1024 * 1024)).toFixed(1)} MB`
      : "";
    els.lastCapture.innerHTML = `
      <span class="${dotClass}">${mark}</span>
      <span>${escapeHtml(seq)}</span>
      <code>${escapeHtml(name)}</code>
      ${size ? `<span class="text-faint">${escapeHtml(size)}</span>` : ""}
    `;
  }

  // -----------------------------------------------------------------
  // Render: error banner
  // -----------------------------------------------------------------
  function renderErrorBanner(session) {
    if (!els.errorBanner) return;
    if (session.error) {
      const isRecovering = session.error.startsWith("recovering from:");
      els.errorBanner.classList.toggle("banner-warning", isRecovering);
      els.errorBanner.textContent = session.error;
      els.errorBanner.classList.remove("hidden");
    } else {
      els.errorBanner.classList.add("hidden");
      els.errorBanner.classList.remove("banner-warning");
      els.errorBanner.textContent = "";
    }
  }

  // -----------------------------------------------------------------
  // Render: control buttons
  //
  // This is the state machine that *was* buggy. Logic:
  //
  //   running       → Pause | Restart | Cancel                 (new hidden)
  //   paused        → Resume | Restart | Cancel                 (new hidden)
  //   canceling     → all disabled + "Canceling…"                (new hidden)
  //   done | complete → New Session (others hidden)
  //   canceled      → New Session (others hidden)
  //   error         → New Session (others hidden)
  // -----------------------------------------------------------------
  function renderControls({ status, isTerminal, isLive, isPaused, isCanceling }) {
    if (!els.pauseResumeBtn || !els.restartBtn || !els.cancelBtn || !els.newSessionBtn) return;

    // Reset to known defaults
    els.pauseResumeBtn.classList.add("hidden");
    els.restartBtn.classList.add("hidden");
    els.cancelBtn.classList.add("hidden");
    els.newSessionBtn.classList.add("hidden");

    if (isCanceling) {
      // Lock everything; server is finalizing the cancel.
      els.cancelBtn.classList.remove("hidden");
      els.cancelBtn.textContent = "Canceling…";
      els.cancelBtn.disabled = true;
      return;
    }

    if (isLive) {
      els.cancelBtn.classList.remove("hidden");
      els.cancelBtn.textContent = "Cancel";
      els.cancelBtn.disabled = false;

      els.restartBtn.classList.remove("hidden");
      els.restartBtn.textContent = "Restart";
      els.restartBtn.disabled = false;

      els.pauseResumeBtn.classList.remove("hidden");
      if (isPaused) {
        els.pauseResumeBtn.textContent = "Resume";
        els.pauseResumeBtn.disabled = false;
      } else {
        els.pauseResumeBtn.textContent = "Pause";
        els.pauseResumeBtn.disabled = false;
      }
      return;
    }

    if (isTerminal) {
      // Session is over — single CTA back to setup.
      els.newSessionBtn.classList.remove("hidden");
      els.newSessionBtn.textContent = "New Session";
      els.newSessionBtn.disabled = false;
      return;
    }

    // Unknown future state — safest default is "go back to setup"
    els.newSessionBtn.classList.remove("hidden");
    els.newSessionBtn.textContent = "New Session";
    els.newSessionBtn.disabled = false;
  }

  // -----------------------------------------------------------------
  // API calls
  // -----------------------------------------------------------------
  async function postJson(path) {
    const resp = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" } });
    let data = null;
    try { data = await resp.json(); } catch (_) { /* no body */ }
    if (!resp.ok || data?.error) {
      throw new Error(data?.error || `Request failed (${resp.status})`);
    }
    return data;
  }

  async function startSession() {
    if (state.isStarting) return;
    const cfg = readFormConfig();
    const errMsg = validateFormConfig(cfg);
    if (errMsg) {
      showToast(errMsg, "error");
      return;
    }
    state.isStarting = true;
    els.startBtn.disabled = true;
    try {
      const resp = await fetch("/session/api/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(cfg),
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok || data.error) {
        throw new Error(data.error || `Start failed (${resp.status})`);
      }
      showToast("Session started", "ok");
      // Switch the UI immediately to the active panel; the next
      // status poll will refine it.
      showActive();
      await pollStatus();
    } catch (err) {
      showToast(err.message || String(err), "error");
    } finally {
      state.isStarting = false;
      els.startBtn.disabled = false;
    }
  }

  async function pauseSession() {
    try { await postJson("/session/api/pause"); showToast("Paused", "ok"); }
    catch (e) { showToast(e.message, "error"); }
    await pollStatus();
  }

  async function resumeSession() {
    try { await postJson("/session/api/resume"); showToast("Resumed", "ok"); }
    catch (e) { showToast(e.message, "error"); }
    await pollStatus();
  }

  async function restartSession() {
    // Restart = stop + start (with same config). The server's /stop
    // is the same as cancel; we then wait for the terminal state
    // before starting a fresh session with the current form config.
    try {
      await postJson("/session/api/stop");
      showToast("Restarting…", "info");
      // Poll fast so we catch the terminal state, then start a new one.
      await waitForTerminalState(15000);
      await startSession();
    } catch (e) {
      showToast(e.message, "error");
      await pollStatus();
    }
  }

  // Poll the status endpoint until it reports idle (no active session),
  // or until `timeoutMs` elapses. Used to make "Cancel → New Session"
  // and "Restart" feel snappy without races.
  async function waitForTerminalState(timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    while (Date.now() < deadline) {
      try {
        const resp = await fetch("/session/api/status", { cache: "no-store" });
        const data = await resp.json();
        if (data.status === "idle" || !data.session) return;
        if (["done", "canceled", "error", "complete"].includes(data.session.status)) {
          return;
        }
      } catch (_) { /* keep trying */ }
      await sleep(400);
    }
    throw new Error("Timed out waiting for session to finish");
  }

  function sleep(ms) {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }

  async function cancelSession({ andStartNew = false } = {}) {
    try {
      await postJson("/session/api/stop");
      showToast(andStartNew ? "Canceling → new session…" : "Canceling…", "info");
      await waitForTerminalState(15000);
      if (andStartNew) {
        await startSession();
      } else {
        // The session now sits in a terminal ("canceled") state on the
        // server and will keep being reported by /status until it's
        // explicitly dismissed — otherwise the next poll would fetch it
        // right back and flip the UI to the active panel again.
        await postJson("/session/api/dismiss").catch(() => {});
        showSetup();
        await pollStatus();
      }
    } catch (e) {
      showToast(e.message, "error");
      // Force-render the setup view regardless — the server may have
      // actually finished and the only thing stuck is our local view.
      await postJson("/session/api/dismiss").catch(() => {});
      showSetup();
      await pollStatus();
    }
  }

  async function newSession() {
    // Tell the server we're done looking at the finished/errored session.
    // Without this, the server keeps reporting it (by design, so the
    // terminal panel can be shown) and the next background poll would
    // fetch it right back and flip the UI back to the active panel.
    await postJson("/session/api/dismiss").catch(() => {});
    showSetup();
    // Force a fresh estimate so the form is meaningful
    scheduleEstimate();
  }

  // -----------------------------------------------------------------
  // Confirm dialog
  //
  // The HTML exposes three buttons in the dialog:
  //   confirmCancelBtn            → "Stay" (close the dialog, do nothing)
  //   confirmCancelStayBtn        → "Cancel Only" (cancel, stay on active)
  //   confirmCancelNewSessionBtn  → "Cancel → New Session"
  // -----------------------------------------------------------------
  function openCancelConfirm() {
    if (!els.confirmBackdrop) return;
    const session = state.lastSession;
    const remaining = session ? Math.max(0, session.total - session.completed) : 0;
    if (els.confirmTitle) {
      els.confirmTitle.textContent = "Cancel this session?";
    }
    if (els.confirmBody) {
      els.confirmBody.innerHTML = session
        ? `This will stop the session <strong>${escapeHtml(session.session_name || "")}</strong> after the current exposure. <strong>${remaining}</strong> frame(s) will be skipped.`
        : "This will stop the running session.";
    }
    els.confirmBackdrop.classList.remove("hidden");
  }

  function closeCancelConfirm() {
    els.confirmBackdrop?.classList.add("hidden");
  }

  // -----------------------------------------------------------------
  // Wire up event listeners
  // -----------------------------------------------------------------
  function initFormInputs() {
    const reactive = [
      els.sessionName, els.exposureSeconds, els.intervalSeconds,
      els.numPhotos, els.targetEndTime, els.iso, els.aperture,
    ];
    reactive.forEach((el) => {
      if (!el) return;
      el.addEventListener("input", scheduleEstimate);
      el.addEventListener("change", scheduleEstimate);
    });
  }

  function initButtons() {
    els.setupForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      startSession();
    });

    els.pauseResumeBtn?.addEventListener("click", () => {
      const s = state.lastSession;
      if (!s) return;
      if (s.status === "paused") resumeSession();
      else if (s.status === "running") pauseSession();
    });

    els.restartBtn?.addEventListener("click", () => {
      const s = state.lastSession;
      if (!s) return;
      if (!confirm("Restart this session with the same settings? The current run will be stopped and a new one started.")) return;
      restartSession();
    });

    els.cancelBtn?.addEventListener("click", () => {
      openCancelConfirm();
    });

    els.newSessionBtn?.addEventListener("click", () => {
      newSession();
    });

    // Confirm dialog buttons
    els.confirmCancelBtn?.addEventListener("click", () => {
      // Stay — just close
      closeCancelConfirm();
    });
    els.confirmCancelStayBtn?.addEventListener("click", () => {
      closeCancelConfirm();
      cancelSession({ andStartNew: false });
    });
    els.confirmCancelNewSessionBtn?.addEventListener("click", () => {
      closeCancelConfirm();
      cancelSession({ andStartNew: true });
    });

    // Click on backdrop closes it (acts like Stay)
    els.confirmBackdrop?.addEventListener("click", (e) => {
      if (e.target === els.confirmBackdrop) closeCancelConfirm();
    });

    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && !els.confirmBackdrop?.classList.contains("hidden")) {
        closeCancelConfirm();
      }
    });
  }

  // -----------------------------------------------------------------
  // Boot
  // -----------------------------------------------------------------
  function init() {
    initNightVision();
    initSequenceButtons();
    initShutterPreset();
    initAutoFlatExposure();
    syncShutterPresetVisibility();
    initModeSegmented();
    initFormInputs();
    initButtons();
    syncScheduleModeFields();
    scheduleEstimate();
    startCameraHealthLoop();
    // Kick off the poll loop; the first response decides which panel
    // to show, which is how we recover from "stuck on canceling" or
    // from a previous tab leaving the UI in a half-broken state.
    pollStatus();
    // Kick off the local live ticker once. It runs for the lifetime of
    // the page and is a no-op whenever there's no live session, so it
    // never needs to be started/stopped per-session or per-poll.
    if (!state.liveTickerId) {
      state.liveTickerId = setInterval(tickLiveDisplay, 250);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();