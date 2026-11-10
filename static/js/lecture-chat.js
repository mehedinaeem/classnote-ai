(() => {
  "use strict";
  const form = document.getElementById("lecture-chat-form");
  if (!form) return;
  const history = document.getElementById("chat-history");
  const status = document.getElementById("chat-status");
  const error = document.getElementById("chat-error");
  const button = form.querySelector('button[type="submit"]');
  const create = (tag, className, text) => {
    const element = document.createElement(tag);
    element.className = className;
    if (text !== undefined) element.textContent = text;
    return element;
  };
  const timestamp = (value) => {
    const seconds = Math.max(0, Math.floor(Number(value)));
    return [Math.floor(seconds / 3600), Math.floor(seconds / 60) % 60, seconds % 60]
      .map((part) => String(part).padStart(2, "0")).join(":");
  };
  const pending = document.getElementById("pending-chat-jobs");
  if (pending) {
    const urls = JSON.parse(pending.textContent);
    if (urls.length) {
      const resume = async () => {
        let finished = false;
        for (const item of urls) {
          try {
            const response = await fetch(item.url, {credentials: "same-origin", cache: "no-store"});
            if (!response.ok || response.redirected) continue;
            const job = await response.json();
            if (["completed", "failed"].includes(job.status)) finished = true;
            status.textContent = job.message;
          } catch (_) { status.textContent = "Waiting to reconnect to your queued answer…"; }
        }
        if (finished) window.location.reload();
        else setTimeout(resume, 3000);
      };
      setTimeout(resume, 1000);
    }
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (button.disabled || !form.reportValidity()) return;
    const data = new FormData(form);
    button.disabled = true;
    error.hidden = true;
    status.textContent = "Searching this lecture and answering locally…";
    try {
      const response = await fetch(form.action, {
        method: "POST", credentials: "same-origin", body: data,
        headers: { "Accept": "application/json", "X-CSRFToken": data.get("csrfmiddlewaretoken") },
      });
      if (response.redirected) throw new Error("Your session expired. Sign in again before asking.");
      if (response.status === 403) throw new Error("Security check failed. Refresh this page and try again.");
      if (!response.headers.get("Content-Type")?.includes("application/json")) {
        throw new Error("The question could not be answered. Please try again.");
      }
      let result = await response.json();
      if (!response.ok) throw new Error(result.error || "The question could not be answered.");
      if (response.status === 202) {
        const url = result.status_url;
        while (true) {
          await new Promise((resolve) => setTimeout(resolve, 2000));
          const progress = await fetch(url, {credentials: "same-origin", cache: "no-store", headers: {Accept: "application/json"}});
          if (progress.redirected || !progress.ok) throw new Error("Could not check the answer. Refresh to view saved answers.");
          const job = await progress.json();
          status.textContent = job.message;
          if (job.status === "failed") throw new Error(job.error || "Local answering failed.");
          if (job.status === "completed") { result = job.result; break; }
        }
      }
      const turn = create("article", "chat-turn");
      const student = create("div", "rounded-3 p-3 bg-primary-subtle mb-2");
      student.append(create("p", "small fw-bold mb-1", "Student"), create("p", "mb-0 text-break chat-text", result.question));
      const answer = create("div", "rounded-3 border p-3 p-sm-4");
      answer.append(create("p", "small fw-bold text-primary mb-2", "AI"), create("p", "text-break chat-text", result.answer));
      if (result.sources.length) answer.append(create("h3", "h6", "Sources"));
      for (const source of result.sources) {
        const container = create("div", "border-start border-3 ps-3 mt-3");
        const play = create("button", "btn btn-outline-primary btn-sm transcript-seek", "▶ " + timestamp(source.timestamp));
        play.type = "button";
        play.dataset.seek = String(source.timestamp);
        container.append(play, create("blockquote", "small text-secondary text-break mt-2 mb-0", source.excerpt));
        answer.append(container);
      }
      turn.append(student, answer);
      document.getElementById("chat-empty")?.remove();
      history.append(turn);
      while (history.children.length > 20) history.firstElementChild.remove();
      if (form.elements.question.value.trim() === String(data.get("question")).trim()) {
        form.elements.question.value = "";
      }
      status.textContent = "Answer saved. Select a source timestamp to listen.";
    } catch (exception) {
      error.textContent = exception.message || "Connection failed. Your question has not been cleared.";
      error.hidden = false;
      status.textContent = "Your question is still available. Try again when the local services are ready.";
    } finally {
      button.disabled = false;
    }
  });
})();
