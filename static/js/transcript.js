(() => {
  "use strict";
  const player = document.getElementById("lecture-audio");
  const status = document.getElementById("seek-status");
  document.addEventListener("click", (event) => {
      const button = event.target.closest?.(".transcript-seek");
      if (!button) return;
      if (!player) return;
      const seconds = Number(button.dataset.seek);
      if (!Number.isFinite(seconds) || seconds < 0) return;
      const seek = () => {
        try {
          player.currentTime = seconds;
        } catch (_) {
          if (status) status.textContent = "Audio is still loading. Try the timestamp again.";
          return;
        }
        player.play().catch(() => {
          if (status) status.textContent = "Press play on the audio player to listen.";
        });
      };
      if (player.readyState >= 1) seek();
      else {
        player.addEventListener("loadedmetadata", seek, { once: true });
        player.load();
      }
  });
  const form = document.getElementById("process-lecture-form");
  form?.addEventListener("submit", () => {
    const button = form.querySelector("button");
    button.disabled = true;
    button.textContent = "Queueing local processing…";
    document.getElementById("process-help").textContent = "Processing your lecture. This may take several minutes on CPU.";
  });
})();
