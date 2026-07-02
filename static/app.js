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
    countdownTimerId: 0,
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
    const cleaned = isoString.replace("Z", "");
    const dt = new Date(cleaned);
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
  function initSequenceButtons() {
    if (!els.seqGrid) return;
    const buttons = els.seqGrid.querySelectorAll(".seq-btn");
    buttons.forEach((btn) => {
      btn.addEventListener("click", () => {
        const seq = btn.getAttribute("data-seq");
        if (!seq) return;
        state.sequenceType = seq;
        buttons.forEach((b) =>
          b.setAttribute("aria-pressed", b === btn ? "true" : "false")
        );
        scheduleEstimate();
      });
    });
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
      exposure_seconds: parseInt(els.exposureSeconds?.value || "0", 10) || 0,
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
    if (cfg.exposure_seconds < 1) return "Exposure must be at least 1 second.";
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
        <span><span class="val">${numPhotos}</span> photos × <span class="val">${cfg.exposure_seconds}s</span></span>
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

    // camera pill
    if (payload?.camera) applyCameraHealth(payload);
    else refreshCameraHealth();

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
    stopExposureCountdown();
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
    // Elapsed
    if (els.teleElapsed) {
      const startedAt = session.started_at;
      if (startedAt) {
        const t0 = new Date(startedAt.replace("Z", "")).getTime();
        const elapsed = Math.max(0, Math.floor((Date.now() - t0) / 1000));
        els.teleElapsed.textContent = formatClock(elapsed);
      } else {
        els.teleElapsed.textContent = "—";
      }
    }

    // ETA
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
      els.teleExposure.textContent = sec > 0 ? `${sec}s` : "—";
    }

    // Error count from DB captures
    if (els.teleErrors) {
      const errs = (payload.captures || []).filter((c) => c.status === "error").length;
      els.teleErrors.textContent = String(errs);
      els.teleErrors.style.color = errs > 0 ? "var(--danger-bright)" : "";
    }

    // Current-exposure mini-meter + countdown
    renderExposureStrip(session, status);
  }

  function renderExposureStrip(session, status) {
    const exposure = Number(session.config?.exposure_seconds) || 0;
    const startedAt = session.current_capture_started_at;

    if (status === "running" && exposure > 0 && startedAt) {
      const t0 = new Date(startedAt.replace("Z", "")).getTime();
      const elapsed = Math.max(0, (Date.now() - t0) / 1000);
      const pct = Math.min(100, (elapsed / exposure) * 100);
      const remaining = Math.max(0, Math.ceil(exposure - elapsed));

      if (els.miniMeterFill) els.miniMeterFill.style.width = `${pct}%`;
      if (els.exposureCountdown)
        els.exposureCountdown.textContent = `${formatClock(remaining)} remaining`;
      startExposureCountdown(() => renderExposureStrip(session, status));
    } else {
      stopExposureCountdown();
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

  function startExposureCountdown(fn) {
    stopExposureCountdown();
    state.countdownTimerId = setInterval(fn, 250);
  }

  function stopExposureCountdown() {
    if (state.countdownTimerId) {
      clearInterval(state.countdownTimerId);
      state.countdownTimerId = 0;
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
    if (session.error && (session.status === "error" || session.status === "canceled")) {
      els.errorBanner.textContent = session.error;
      els.errorBanner.classList.remove("hidden");
    } else {
      els.errorBanner.classList.add("hidden");
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
        if (data.session?.status &&
            ["done", "canceled", "error", "complete"].includes(data.session.status)) {
          // Even terminal sessions on the server still get reported via
          // /status — we need to wait until the server *stops returning
          // the session* (i.e. until `current_session()` returns null).
          if (!data.session) return;
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
        showSetup();
        await pollStatus();
      }
    } catch (e) {
      showToast(e.message, "error");
      // Force-render the setup view regardless — the server may have
      // actually finished and the only thing stuck is our local view.
      showSetup();
      await pollStatus();
    }
  }

  async function newSession() {
    // Just bring the user back to the setup panel. The server may
    // still hold a terminal session, but that's fine — the next
    // start will be rejected with 409 if needed, and the user can
    // adjust the form before pressing Start again.
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
    initModeSegmented();
    initFormInputs();
    initButtons();
    syncScheduleModeFields();
    scheduleEstimate();
    refreshCameraHealth();
    // Kick off the poll loop; the first response decides which panel
    // to show, which is how we recover from "stuck on canceling" or
    // from a previous tab leaving the UI in a half-broken state.
    pollStatus();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
