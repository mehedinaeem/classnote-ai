(() => {
  "use strict";
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  async function poll(panel) {
    const label = panel.querySelector('[data-job-message]');
    const bar = panel.querySelector('[role="progressbar"]');
    while (true) {
      try {
        const response = await fetch(panel.dataset.statusUrl, {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}});
        if (response.redirected || !response.ok) throw new Error("Unable to check progress. Sign in or refresh this page.");
        const data = await response.json();
        label.textContent = data.message;
        const progress = Math.max(0, Math.min(100, Number(data.progress) || 0));
        if (bar) {
          bar.style.width = progress + "%";
          bar.textContent = progress + "%";
          bar.setAttribute("aria-valuenow", String(progress));
          bar.classList.toggle("bg-danger", data.status === "failed");
        }
        panel.querySelectorAll('[data-milestone]').forEach((line) => {
          const done = progress >= Number(line.dataset.milestone);
          const active = !done && progress >= Number(line.dataset.starts) && data.status !== "failed";
          line.querySelector("span").textContent = done ? "✓" : active ? "●" : "○";
          line.classList.toggle("text-primary", active);
        });
        if (["completed", "failed"].includes(data.status)) {
          window.location.reload();
          return;
        }
      } catch (error) {
        label.textContent = error.message + " Retrying…";
      }
      await sleep(3000);
    }
  }
  document.querySelectorAll('[data-background-job][data-active="true"]').forEach(poll);
})();
