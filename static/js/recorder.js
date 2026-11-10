(() => {
  "use strict";
  const form = document.getElementById("recording-form");
  if (!form) return;
  const el = (id) => document.getElementById(id);
  const start = el("record-start"), pause = el("record-pause");
  const resume = el("record-resume"), stop = el("record-stop"), save = el("record-save");
  const preview = el("recording-preview"), level = el("audio-level");
  const message = el("recorder-message"), status = el("recording-status");
  const maxBytes = Number(form.dataset.maxBytes);
  let recorder, stream, audioContext, animation, timer;
  let chunks = [], bytes = 0, blob = null, objectUrl = null;
  let mode = "ready", elapsed = 0, startedAt = 0, dirty = false;
  let recordingError = false, limitReached = false;

  function tell(text, error = false) {
    message.textContent = text;
    message.className = error ? "alert alert-warning" : "alert alert-info";
  }
  function state(next, label) {
    mode = next;
    status.textContent = label;
    start.disabled = !["ready", "stopped"].includes(next);
    pause.disabled = next !== "recording";
    resume.disabled = next !== "paused";
    stop.disabled = !["recording", "paused"].includes(next);
    save.disabled = next !== "stopped" || !blob || blob.size > maxBytes;
    form.elements.course.disabled = ["requesting", "recording", "paused", "stopping", "saving"].includes(next);
    form.elements.title.disabled = form.elements.course.disabled;
    start.textContent = blob ? "Record Again" : "Start Recording";
  }
  function updateTimer() {
    const ms = elapsed + (mode === "recording" ? performance.now() - startedAt : 0);
    const seconds = Math.floor(ms / 1000);
    const hours = Math.floor(seconds / 3600);
    el("recording-timer").textContent =
      (hours ? String(hours).padStart(2, "0") + ":" : "") +
      String(Math.floor(seconds / 60) % 60).padStart(2, "0") + ":" +
      String(seconds % 60).padStart(2, "0");
  }
  function freezeTimer() {
    if (mode === "recording") elapsed += performance.now() - startedAt;
    updateTimer();
  }
  function releaseMicrophone() {
    clearInterval(timer);
    cancelAnimationFrame(animation);
    if (stream) stream.getTracks().forEach((track) => track.stop());
    stream = null;
    if (audioContext) audioContext.close().catch(() => {});
    audioContext = null;
    level.value = 0;
  }
  function meter() {
    // Meter failure must never prevent recording.
    try {
      const Context = window.AudioContext || window.webkitAudioContext;
      if (!Context) return;
      audioContext = new Context();
      audioContext.resume().catch(() => {});
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 256;
      audioContext.createMediaStreamSource(stream).connect(analyser);
      const samples = new Uint8Array(analyser.fftSize);
      const draw = () => {
        analyser.getByteTimeDomainData(samples);
        let sum = 0;
        for (const sample of samples) sum += ((sample - 128) / 128) ** 2;
        level.value = mode === "recording" ? Math.min(1, Math.sqrt(sum / samples.length) * 4) : 0;
        animation = requestAnimationFrame(draw);
      };
      draw();
    } catch (_) { level.value = 0; }
  }
  function endRecording() {
    if (!recorder || recorder.state === "inactive") return;
    freezeTimer();
    state("stopping", "Finishing recording…");
    recorder.stop();
  }
  const extension = (type) => {
    const container = type.split(";")[0].toLowerCase();
    return { "audio/webm": "webm", "audio/ogg": "ogg", "audio/mp4": "mp4",
      "video/mp4": "mp4", "audio/wav": "wav", "audio/mpeg": "mp3" }[container];
  };
  if (!window.isSecureContext) {
    tell("Microphone recording requires HTTPS or localhost. Open this site over HTTPS on your phone, or upload an existing file.", true);
    return;
  }
  if (!window.MediaRecorder || !navigator.mediaDevices?.getUserMedia) {
    tell("This browser does not support microphone recording. Try a current browser or use Upload an existing file.", true);
    return;
  }
  state("ready", "Ready");
  start.addEventListener("click", async () => {
    if (!["ready", "stopped"].includes(mode) || !form.reportValidity()) return;
    if (blob && !window.confirm("Discard this recording and start again? Download a backup first if needed.")) return;
    state("requesting", "Waiting for microphone permission…");
    tell("Allow microphone access to begin recording.");
    try {
      stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const types = ["audio/webm;codecs=opus", "audio/mp4", "audio/ogg;codecs=opus", "audio/webm"];
      const mimeType = typeof MediaRecorder.isTypeSupported === "function" ?
        types.find((type) => MediaRecorder.isTypeSupported(type)) : null;
      recorder = mimeType ? new MediaRecorder(stream, { mimeType, audioBitsPerSecond: 64000 }) :
        new MediaRecorder(stream);
      if (!extension(recorder.mimeType)) throw new Error("Unsupported recording format");
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      objectUrl = null;
      preview.removeAttribute("src");
      el("preview-area").hidden = true;
      chunks = []; bytes = 0; blob = null; elapsed = 0;
      recordingError = false; limitReached = false;
      recorder.addEventListener("dataavailable", (event) => {
        if (event.data.size) {
          chunks.push(event.data);
          bytes += event.data.size;
          if (bytes > maxBytes) {
            limitReached = true;
            endRecording();
          }
        }
      });
      recorder.addEventListener("pause", () => {
        freezeTimer();
        state("paused", "Paused");
        updateTimer();
      });
      recorder.addEventListener("resume", () => {
        startedAt = performance.now();
        state("recording", "Recording");
      });
      recorder.addEventListener("error", () => {
        recordingError = true;
        tell("Recording was interrupted. Preview and download any captured audio before trying again.", true);
        endRecording();
      });
      recorder.addEventListener("stop", () => {
        // Final dataavailable is delivered before stop.
        freezeTimer();
        blob = new Blob(chunks, { type: recorder.mimeType || chunks[0]?.type });
        chunks = [];
        releaseMicrophone();
        state("stopped", blob.size ? "Recording stopped" : "No audio captured");
        updateTimer();
        if (!blob.size) {
          tell("No audio was captured. Please try again.", true);
          return;
        }
        objectUrl = URL.createObjectURL(blob);
        preview.src = objectUrl;
        const download = el("recording-download");
        download.href = objectUrl;
        download.download = "lecture." + extension(blob.type);
        el("preview-area").hidden = false;
        if (limitReached || blob.size > maxBytes) {
          tell("This recording exceeded the upload limit. Download a backup, then make a shorter recording.", true);
        } else if (!recordingError) {
          tell("Preview your recording, then select Save Lecture. Keep this page open until saving completes.");
        }
      });
      stream.getAudioTracks().forEach((track) => track.addEventListener("ended", () => {
        recordingError = true;
        tell("Microphone disconnected. Preview any captured audio before saving.", true);
        endRecording();
      }));
      recorder.start(1000);
      dirty = true;
      startedAt = performance.now();
      state("recording", "Recording");
      tell("Recording locally. Keep this page open and your phone awake.");
      timer = setInterval(updateTimer, 250);
      meter();
    } catch (error) {
      releaseMicrophone();
      state(blob ? "stopped" : "ready", blob ? "Recording stopped" : "Ready");
      const denied = ["NotAllowedError", "SecurityError"].includes(error.name);
      tell(denied ? "Microphone permission was denied. Allow access in browser settings and try again." :
        "Unable to start the microphone or recording format. Check your microphone or upload an existing audio file.", true);
    }
  });
  pause.addEventListener("click", () => {
    if (recorder?.state === "recording") {
      pause.disabled = true;
      recorder.pause();
    }
  });
  resume.addEventListener("click", () => {
    if (recorder?.state === "paused") {
      resume.disabled = true;
      recorder.resume();
    }
  });
  stop.addEventListener("click", endRecording);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (mode !== "stopped" || !blob || blob.size > maxBytes || !form.reportValidity()) return;
    // Build before disabling course/title controls.
    const data = new FormData(form);
    data.append("audio_file", blob, "lecture." + extension(blob.type));
    state("saving", "Saving lecture…");
    tell("Uploading your recording. Please keep this page open.");
    try {
      const response = await fetch(form.dataset.uploadUrl, {
        method: "POST", credentials: "same-origin", body: data,
        headers: { "X-CSRFToken": data.get("csrfmiddlewaretoken"), "Accept": "application/json" },
      });
      if (response.redirected) throw new Error("Your session expired. Download a backup before logging in again.");
      if (response.status === 403) throw new Error("Security check failed. Download a backup before refreshing and logging in again.");
      const result = await response.json();
      if (!response.ok) {
        const errors = Object.values(result.errors || {}).flat().map((error) => error.message);
        throw new Error(result.error || errors.join(" ") || "Unable to save. Please try again.");
      }
      dirty = false;
      state("saved", "Lecture saved");
      window.location.assign(result.url);
    } catch (error) {
      state("stopped", "Recording not saved");
      tell(error instanceof SyntaxError ? "The server could not save this recording. Download a backup and try again." :
        error.message || "Connection failed. Your recording is still available; try saving again.", true);
    }
  });
  window.addEventListener("beforeunload", (event) => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  window.addEventListener("pagehide", () => {
    releaseMicrophone();
    if (objectUrl) URL.revokeObjectURL(objectUrl);
  });
})();
