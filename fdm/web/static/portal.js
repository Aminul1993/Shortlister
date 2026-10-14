(() => {
  // Shortlist page: keep the selection count, "select all" state and Create button in sync.
  const form = document.getElementById("create-form");
  if (form) {
    const boxes = [...form.querySelectorAll(".gig-check")];
    const all = document.getElementById("select-all");
    const count = document.getElementById("selected-count");
    const button = document.getElementById("create-btn");
    const sync = () => {
      const selected = boxes.filter((box) => box.checked).length;
      count.textContent = `${selected} selected`;
      button.disabled = selected === 0 || button.dataset.locked === "1";
      all.checked = selected === boxes.length;
      all.indeterminate = selected > 0 && selected < boxes.length;
    };
    all.addEventListener("change", () => {
      boxes.forEach((box) => { box.checked = all.checked; });
      sync();
    });
    boxes.forEach((box) => box.addEventListener("change", sync));
    sync();
  }

  // Long-running submits (refresh, create) show a busy label so they aren't clicked twice.
  document.querySelectorAll("form[data-busy-label]").forEach((busyForm) => {
    busyForm.addEventListener("submit", () => {
      const submit = busyForm.querySelector("button[type=submit]");
      if (submit) {
        submit.disabled = true;
        submit.textContent = busyForm.dataset.busyLabel;
      }
    });
  });
  // Coming back via the browser's back button restores the busy state from cache; reload instead.
  window.addEventListener("pageshow", (event) => { if (event.persisted) location.reload(); });

  // Job page: copy a proposal to the clipboard. Delegated, because polling replaces the item list.
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-copy]");
    const source = button && document.getElementById(button.dataset.copy);
    if (!source) return;
    try {
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = "Copied";
    } catch {
      getSelection().selectAllChildren(source); // no clipboard access: leave it selected for Ctrl+C
      button.textContent = "Selected";
    }
    setTimeout(() => { button.textContent = "Copy"; }, 2000);
  });

  // Job page: re-render the server-side item list until the job finishes.
  const items = document.getElementById("job-items");
  if (items) {
    const finished = () => items.querySelector("[data-finished='true']") !== null;
    const poll = async () => {
      if (finished()) return;
      try {
        const response = await fetch(items.dataset.src);
        if (response.ok) {
          const open = [...items.querySelectorAll("details")].map((d) => d.open);
          items.innerHTML = await response.text();
          items.querySelectorAll("details").forEach((d, i) => { d.open = open[i] || false; });
        }
      } catch {
        // Transient network error: try again on the next tick.
      }
      setTimeout(poll, 2000);
    };
    setTimeout(poll, 2000);
  }
})();
