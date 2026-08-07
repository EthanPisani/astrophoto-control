/* =============================================================
 * AstroCap — Plate Solve & Annotate  (front-end controller)
 * =============================================================
 *
 * Dedicated page for: take 1 photo → plate solve → annotate.
 * Not part of the main session control loop — fully self-contained.
 * ============================================================= */

(() => {
  "use strict";

  // -----------------------------------------------------------------
  // Element lookups
  // -----------------------------------------------------------------
  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => document.querySelectorAll(sel);

  const els = {
    // panels
    setupPanel: $("#setupPanel"),
    progressPanel: $("#progressPanel"),
    resultsPanel: $("#resultsPanel"),

    // topbar
    cameraPill: $("#cameraPill"),
    cameraDot: $("#cameraDot"),
    cameraLabel: $("#cameraLabel"),

    // form
    solveForm: $("#solveForm"),
    targetName: $("#targetName"),
    sourceSegmented: $("#sourceSegmented"),
    captureSettings: $("#captureSettings"),
    uploadSettings: $("#uploadSettings"),
    solveExposure: $("#solveExposure"),
    solveIso: $("#solveIso"),
    searchRadius: $("#searchRadius"),
    captureBtn: $("#captureBtn"),
    fileInput: $("#fileInput"),
    fileDrop: $("#fileDrop"),
    fileLabel: $("#fileLabel"),
    hemisphereSegmented: $("#hemisphereSegmented"),

    // progress
    progressTitle: $("#progressTitle"),
    progressMsg: $("#progressMsg"),
    solveLog: $("#solveLog"),

    // results
    resultTitle: $("#resultTitle"),
    annotatedImage: $("#annotatedImage"),
    imagePlaceholder: $("#imagePlaceholder"),
    annotatedContainer: $("#annotatedContainer"),
    solveErrorBanner: $("#solveErrorBanner"),
    metaTarget: $("#metaTarget"),
    metaRa: $("#metaRa"),
    metaDec: $("#metaDec"),
    metaScale: $("#metaScale"),
    metaFieldSize: $("#metaFieldSize"),
    metaRotation: $("#metaRotation"),
    metaStarsDetected: $("#metaStarsDetected"),
    metaStarsMatched: $("#metaStarsMatched"),
    dsoGrid: $("#dsoGrid"),
    dsoSection: $("#dsoSection"),
    backBtn: $("#backBtn"),
    retakeBtn: $("#retakeBtn"),
    downloadBtn: $("#downloadBtn"),

    // toast
    toast: $("#toast"),
  };

  // -----------------------------------------------------------------
  // State
  // -----------------------------------------------------------------
  const state = {
    taskId: null,
    pollTimerId: 0,
    isRunning: false,
    sourceMode: "capture",   // "capture" | "upload"
    selectedFile: null,
    hemisphere: "north",     // "north" | "south" | "auto"
  };

  // -----------------------------------------------------------------
  // Utility
  // -----------------------------------------------------------------
  function formatHMS(totalSeconds) {
    totalSeconds = Math.max(0, Math.floor(totalSeconds || 0));
    const m = Math.floor(totalSeconds / 60);
    const s = totalSeconds % 60;
    return `${m}m ${String(s).padStart(2, "0")}s`;
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => (
      { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
    ));
  }

  function formatRa(raDeg) {
    if (!raDeg) return "—";
    const h = Math.floor(raDeg / 15);
    const m = Math.floor((raDeg / 15 - h) * 60);
    const s = (((raDeg / 15 - h) * 60) - m) * 60;
    return `${String(h).padStart(2, "0")}h ${String(m).padStart(2, "0")}m ${s.toFixed(1)}s`;
  }

  function formatDec(decDeg) {
    if (decDeg == null) return "—";
    const sign = decDeg >= 0 ? "+" : "";
    const d = Math.floor(Math.abs(decDeg));
    const m = Math.floor((Math.abs(decDeg) - d) * 60);
    const s = (((Math.abs(decDeg) - d) * 60) - m) * 60;
    return `${sign}${String(d).padStart(2, "0")}° ${String(m).padStart(2, "0")}' ${s.toFixed(1)}"`;
  }

  // -----------------------------------------------------------------
  // Toast
  // -----------------------------------------------------------------
  let toastTimer = 0;
  function showToast(msg, kind = "info", dur = 3200) {
    if (!els.toast) return;
    clearTimeout(toastTimer);
    els.toast.textContent = msg;
    els.toast.classList.remove("hidden", "is-error", "is-ok");
    if (kind === "error") els.toast.classList.add("is-error");
    if (kind === "ok") els.toast.classList.add("is-ok");
    toastTimer = setTimeout(() => els.toast.classList.add("hidden"), dur);
  }

  // -----------------------------------------------------------------
  // Camera health pill
  // -----------------------------------------------------------------
  async function refreshCameraHealth() {
    try {
      const resp = await fetch("/health/api", { cache: "no-store" });
      const data = await resp.json();
      const cam = data?.camera || {};
      els.cameraDot?.classList.remove("ok", "bad", "pending");
      if (cam.reachable) {
        els.cameraDot?.classList.add("ok");
        const model = cam.model ? `${cam.model} · connected` : "Camera connected";
        els.cameraLabel.textContent = model;
      } else {
        els.cameraDot?.classList.add("bad");
        els.cameraLabel.textContent = "Camera offline";
      }
    } catch (_) {
      els.cameraDot?.classList.remove("ok", "bad", "pending");
      els.cameraDot?.classList.add("bad");
      els.cameraLabel.textContent = "Camera offline";
    }
  }

  // -----------------------------------------------------------------
  // Source mode toggle (capture vs upload)
  // -----------------------------------------------------------------
  function initSourceMode() {
    if (!els.sourceSegmented) return;
    const btns = els.sourceSegmented.querySelectorAll(".seg-btn");
    btns.forEach((btn) => {
      btn.addEventListener("click", () => {
        const mode = btn.getAttribute("data-source");
        if (!mode) return;
        state.sourceMode = mode;
        btns.forEach((b) =>
          b.setAttribute("aria-pressed", b === btn ? "true" : "false")
        );
        syncSourceMode();
      });
    });
  }

  function syncSourceMode() {
    if (state.sourceMode === "upload") {
      els.captureSettings?.classList.add("hidden");
      els.uploadSettings?.classList.remove("hidden");
      els.captureBtn.textContent = "Upload & Solve";
    } else {
      els.captureSettings?.classList.remove("hidden");
      els.uploadSettings?.classList.add("hidden");
      els.captureBtn.textContent = "Capture & Solve";
    }
  }

  // -----------------------------------------------------------------
  // File input / drag-drop
  // -----------------------------------------------------------------
  function initFileInput() {
    if (!els.fileInput || !els.fileDrop) return;

    els.fileInput.addEventListener("change", () => {
      const f = els.fileInput.files?.[0];
      if (f) {
        state.selectedFile = f;
        els.fileLabel.textContent = f.name;
        els.fileDrop.classList.add("has-file");
      } else {
        state.selectedFile = null;
        els.fileLabel.textContent = "Drop a file here or click to browse";
        els.fileDrop.classList.remove("has-file");
      }
    });

    // Drag-and-drop visual feedback
    els.fileDrop.addEventListener("dragover", (e) => {
      e.preventDefault();
      els.fileDrop.classList.add("drag-over");
    });
    els.fileDrop.addEventListener("dragleave", () => {
      els.fileDrop.classList.remove("drag-over");
    });
    els.fileDrop.addEventListener("drop", (e) => {
      e.preventDefault();
      els.fileDrop.classList.remove("drag-over");
      const files = e.dataTransfer?.files;
      if (files?.length) {
        els.fileInput.files = files;
        state.selectedFile = files[0];
        els.fileLabel.textContent = files[0].name;
        els.fileDrop.classList.add("has-file");
      }
    });
  }

  function initHemisphereMode() {
    if (!els.hemisphereSegmented) return;
    const btns = els.hemisphereSegmented.querySelectorAll(".seg-btn");
    btns.forEach((btn) => {
      btn.addEventListener("click", () => {
        btns.forEach((b) => b.setAttribute("aria-pressed", "false"));
        btn.setAttribute("aria-pressed", "true");
        state.hemisphere = btn.getAttribute("data-hemi") || "north";
      });
    });
  }

  // -----------------------------------------------------------------
  // Panel switching
  // -----------------------------------------------------------------
  function showSetup() {
    els.setupPanel?.classList.remove("hidden");
    els.progressPanel?.classList.add("hidden");
    els.resultsPanel?.classList.add("hidden");
    els.captureBtn.disabled = false;
  }

  function showProgress() {
    els.setupPanel?.classList.add("hidden");
    els.progressPanel?.classList.remove("hidden");
    els.resultsPanel?.classList.add("hidden");
  }

  function showResults() {
    els.setupPanel?.classList.add("hidden");
    els.progressPanel?.classList.add("hidden");
    els.resultsPanel?.classList.remove("hidden");
  }

  // -----------------------------------------------------------------
  // Form → payload
  // -----------------------------------------------------------------
  function readForm() {
    const cfg = {
      target_name: (els.targetName?.value || "").trim(),
      search_radius_deg: parseFloat(els.searchRadius?.value || "1.5"),
      hemisphere: state.hemisphere,
    };
    if (state.sourceMode === "capture") {
      cfg.exposure_seconds = parseInt(els.solveExposure?.value || "0", 10) || 30;
      cfg.iso = els.solveIso?.value || "400";
    }
    return cfg;
  }

  function validateForm(cfg) {
    if (!cfg.target_name) return "Please enter a target name.";
    if (state.sourceMode === "capture") {
      if (cfg.exposure_seconds < 1) return "Exposure must be at least 1 second.";
    } else {
      if (!state.selectedFile) return "Please select a file to upload.";
    }
    if (cfg.search_radius_deg <= 0 || cfg.search_radius_deg > 10)
      return "Search radius must be between 0.1 and 10 degrees.";
    return null;
  }

  // -----------------------------------------------------------------
  // Start solve
  // -----------------------------------------------------------------
  async function startSolve() {
    if (state.isRunning) return;
    const cfg = readForm();
    const err = validateForm(cfg);
    if (err) {
      showToast(err, "error");
      return;
    }

    state.isRunning = true;
    els.captureBtn.disabled = true;
    clearLog();
    showProgress();

    if (state.sourceMode === "upload") {
      // --- Upload mode ---
      els.progressTitle.textContent = "Uploading file…";
      els.progressMsg.textContent = "Sending file to server…";
      addLogEntry("Upload", `Uploading ${state.selectedFile.name}…`);

      try {
        const fd = new FormData();
        fd.append("file", state.selectedFile);
        fd.append("target_name", cfg.target_name);
        fd.append("search_radius_deg", String(cfg.search_radius_deg));
        fd.append("hemisphere", state.hemisphere);

        const resp = await fetch("/platesolve/api/upload", {
          method: "POST",
          body: fd,
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.error) {
          throw new Error(data.error || `Upload failed (${resp.status})`);
        }

        state.taskId = data.task_id;
        addLogEntry("Sent", `Task ID: ${state.taskId}`);
        addLogEntry("Solving", "Plate solving in progress…");
        els.progressTitle.textContent = "Solving plate…";
        els.progressMsg.textContent = "Detecting stars, matching catalogues…";
        pollTask();
      } catch (exc) {
        showToast(exc.message || String(exc), "error");
        els.progressMsg.textContent = `Error: ${exc.message}`;
        els.progressTitle.textContent = "Failed";
        addLogEntry("Error", exc.message || String(exc));
        state.isRunning = false;
        els.captureBtn.disabled = false;
      }
    } else {
      // --- Live capture mode ---
      els.progressTitle.textContent = "Capturing frame…";
      els.progressMsg.textContent = "Sending capture command to camera…";
      addLogEntry("Starting", "Preparing capture request");

      try {
        const resp = await fetch("/platesolve/api/start", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(cfg),
        });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok || data.error) {
          throw new Error(data.error || `Request failed (${resp.status})`);
        }

        state.taskId = data.task_id;
        addLogEntry("Sent", `Task ID: ${state.taskId}`);
        addLogEntry("Waiting", "Capture in progress…");
        pollTask();
      } catch (exc) {
        showToast(exc.message || String(exc), "error");
        els.progressMsg.textContent = `Error: ${exc.message}`;
        els.progressTitle.textContent = "Failed";
        addLogEntry("Error", exc.message || String(exc));
        state.isRunning = false;
        els.captureBtn.disabled = false;
      }
    }
  }

  // -----------------------------------------------------------------
  // Poll task status
  // -----------------------------------------------------------------
  function pollTask() {
    clearTimeout(state.pollTimerId);
    if (!state.taskId) return;

    const poll = async () => {
      try {
        const resp = await fetch(`/platesolve/api/status/${state.taskId}`, { cache: "no-store" });
        const data = await resp.json();
        if (data.error) {
          throw new Error(data.error);
        }
        applyTaskResult(data);
      } catch (exc) {
        // network transient — retry
        state.pollTimerId = setTimeout(poll, 2000);
      }
    };
    poll();
  }

  function schedulePoll(intervalMs) {
    clearTimeout(state.pollTimerId);
    state.pollTimerId = setTimeout(pollTask, intervalMs);
  }

  // -----------------------------------------------------------------
  // Apply task result to UI
  // -----------------------------------------------------------------
  function applyTaskResult(data) {
    const status = data.status || "pending";

    // Update progress message
    const stageLabels = {
      pending: "Waiting to start…",
      capturing: "Capturing frame…",
      solving: "Solving plate…",
      done: "Complete!",
      error: "Failed",
    };
    els.progressTitle.textContent = stageLabels[status] || status;
    els.progressMsg.textContent = data.error || status;

    // Add log entries based on status
    if (status === "capturing") {
      addLogEntry("Capture", "Waiting for exposure to complete…");
    } else if (status === "solving") {
      // Add progressively more detail
      addLogEntry("Download", "Image received, processing…");
    }

    if (status === "done") {
      // Success!
      state.isRunning = false;
      els.captureBtn.disabled = false;
      showResults();
      renderResults(data);
      addLogEntry("Done", "Plate solve complete — see annotated image below");
      refreshCameraHealth();
      return;
    }

    if (status === "error") {
      state.isRunning = false;
      els.captureBtn.disabled = false;
      showResults();
      renderError(data);
      addLogEntry("Error", data.error || "Unknown error");
      refreshCameraHealth();
      return;
    }

    // Still running — poll again
    const interval = status === "capturing" ? 2000 : 1500;
    schedulePoll(interval);
  }

  // -----------------------------------------------------------------
  // Render results
  // -----------------------------------------------------------------
  function renderResults(data) {
    // Title
    els.resultTitle.textContent = data.target_name || "Plate Solve";

    // Clear error
    els.solveErrorBanner?.classList.add("hidden");
    els.solveErrorBanner.textContent = "";

    // Show image
    const imgUrl = `/platesolve/api/annotated/${state.taskId}?t=${Date.now()}`;
    if (els.annotatedImage) {
      els.annotatedImage.src = imgUrl;
      els.annotatedImage.style.display = "block";
      els.annotatedImage.onload = () => {
        els.imagePlaceholder?.classList.add("hidden");
      };
      els.annotatedImage.onerror = () => {
        els.imagePlaceholder?.classList.remove("hidden");
        els.imagePlaceholder.innerHTML = '<span>⚠ Failed to load annotated image</span>';
      };
    }

    // Metadata
    els.metaTarget.textContent = data.target_name || "—";
    els.metaRa.textContent = formatRa(data.solved_ra);
    els.metaDec.textContent = formatDec(data.solved_dec);
    els.metaScale.textContent = data.solved_scale
      ? `${data.solved_scale.toFixed(2)}"/px`
      : "—";
    els.metaFieldSize.textContent = data.field_width_arcmin
      ? `${data.field_width_arcmin.toFixed(1)}′ × ${data.field_height_arcmin.toFixed(1)}′`
      : "—";
    els.metaRotation.textContent = data.solved_rotation
      ? `${data.solved_rotation.toFixed(1)}°`
      : "—";
    els.metaStarsDetected.textContent = String(data.n_stars_detected ?? "—");
    els.metaStarsMatched.textContent = String(data.n_stars_matched ?? "—");

    // DSO annotations
    renderDsoList(data.annotations || []);
  }

  function renderError(data) {
    els.solveErrorBanner?.classList.remove("hidden");
    els.solveErrorBanner.textContent = data.error || "An unknown error occurred";
    els.resultTitle.textContent = "Plate Solve Failed";
    if (els.annotatedImage) els.annotatedImage.style.display = "none";
    if (els.imagePlaceholder) {
      els.imagePlaceholder.classList.remove("hidden");
      els.imagePlaceholder.innerHTML = '<span style="color:var(--danger-bright)">⚠ Solve failed</span>';
    }

    els.metaTarget.textContent = data.target_name || "—";
    els.metaRa.textContent = "—";
    els.metaDec.textContent = "—";
    els.metaScale.textContent = "—";
    els.metaFieldSize.textContent = "—";
    els.metaRotation.textContent = "—";
    els.metaStarsDetected.textContent = String(data.n_stars_detected ?? "—");
    els.metaStarsMatched.textContent = String(data.n_stars_matched ?? "—");
    renderDsoList([]);
  }

  function renderDsoList(annotations) {
    if (!els.dsoGrid) return;
    if (!annotations || annotations.length === 0) {
      els.dsoGrid.innerHTML = '<div class="dso-empty">No DSOs found in this field</div>';
      return;
    }

    els.dsoGrid.innerHTML = annotations
      .sort((a, b) => (a.name || "").localeCompare(b.name || ""))
      .map(
        (a) => `
        <div class="dso-tag" title="${escapeHtml(a.name)} — ${escapeHtml(a.type || "")}">
          <span class="dso-type">${escapeHtml(a.type || "")}</span>
          <span class="dso-name">${escapeHtml(a.name)}</span>
          <span class="dso-coord">${formatRa(a.ra)} ${formatDec(a.dec)}</span>
        </div>
      `
      )
      .join("");
  }

  // -----------------------------------------------------------------
  // Log
  // -----------------------------------------------------------------
  function clearLog() {
    if (!els.solveLog) return;
    els.solveLog.innerHTML = "";
  }

  function addLogEntry(action, message) {
    if (!els.solveLog) return;
    const now = new Date();
    const time = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
    const entry = document.createElement("div");
    entry.className = "log-entry";
    entry.innerHTML = `<span class="log-time">${escapeHtml(time)}</span><span class="log-action">${escapeHtml(action)}</span><span class="log-msg">${escapeHtml(message)}</span>`;
    els.solveLog.appendChild(entry);
    els.solveLog.scrollTop = els.solveLog.scrollHeight;
  }

  // -----------------------------------------------------------------
  // Event listeners
  // -----------------------------------------------------------------
  function init() {
    refreshCameraHealth();
    initSourceMode();
    initFileInput();
    initHemisphereMode();
    syncSourceMode();

    els.solveForm?.addEventListener("submit", (e) => {
      e.preventDefault();
      startSolve();
    });

    els.backBtn?.addEventListener("click", () => {
      showSetup();
    });

    els.retakeBtn?.addEventListener("click", () => {
      showSetup();
    });

    els.downloadBtn?.addEventListener("click", () => {
      const a = document.createElement("a");
      a.href = `/platesolve/api/annotated/${state.taskId}?t=${Date.now()}`;
      a.download = `platesolve_${state.taskId}.png`;
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
    });

    // Example suggestions for the target name
    const suggestions = ["M31", "M42", "M45", "M51", "M57", "M81", "M101",
                         "NGC7000", "NGC6960", "NGC1499", "IC434", "IC1396",
                         "Pleiades", "Orion Nebula", "Andromeda", "Veil Nebula"];

    els.targetName?.addEventListener("focus", function () {
      this.setAttribute("list", "targetSuggestions");
    });

    // Add datalist if not already present
    if (els.targetName && !document.getElementById("targetSuggestions")) {
      const datalist = document.createElement("datalist");
      datalist.id = "targetSuggestions";
      suggestions.forEach((s) => {
        const opt = document.createElement("option");
        opt.value = s;
        datalist.appendChild(opt);
      });
      els.targetName.parentNode.appendChild(datalist);
    }

    // Periodic camera health
    setInterval(refreshCameraHealth, 15000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();