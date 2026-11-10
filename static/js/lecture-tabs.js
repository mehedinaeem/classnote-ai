(() => {
  "use strict";
  const navigation = document.getElementById("lecture-tabs");
  if (!navigation) return;
  const tabs = Array.from(navigation.querySelectorAll("a"));
  navigation.setAttribute("role", "tablist");
  function activate(tab, focus = false) {
    tabs.forEach((item) => {
      const selected = item === tab;
      item.setAttribute("role", "tab");
      item.setAttribute("aria-selected", String(selected));
      item.setAttribute("aria-controls", item.hash.slice(1));
      item.tabIndex = selected ? 0 : -1;
      item.classList.toggle("active", selected);
      const panel = document.getElementById(item.hash.slice(1));
      panel.hidden = !selected;
      panel.setAttribute("role", "tabpanel");
      panel.tabIndex = 0;
    });
    if (focus) tab.focus();
  }
  function selectFromHash() {
    activate(tabs.find((tab) => tab.hash === window.location.hash) || tabs[0]);
  }
  tabs.forEach((tab, index) => {
    tab.addEventListener("click", (event) => {
      event.preventDefault();
      activate(tab);
      history.replaceState(null, "", tab.hash);
    });
    tab.addEventListener("keydown", (event) => {
      let next;
      if (event.key === "ArrowRight") next = tabs[(index + 1) % tabs.length];
      if (event.key === "ArrowLeft") next = tabs[(index + tabs.length - 1) % tabs.length];
      if (event.key === "Home") next = tabs[0];
      if (event.key === "End") next = tabs[tabs.length - 1];
      if (next) {
        event.preventDefault();
        activate(next, true);
        history.replaceState(null, "", next.hash);
      }
    });
  });
  window.addEventListener("hashchange", selectFromHash);
  selectFromHash();
  document.getElementById("generate-notes-form")?.addEventListener("submit", (event) => {
    const button = event.currentTarget.querySelector("button");
    button.disabled = true;
    button.textContent = "Generating notes…";
    document.getElementById("note-generation-status").textContent =
      "Extracting and merging academic notes locally. You can leave this page; progress is saved.";
  });
})();
