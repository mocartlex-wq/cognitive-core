// Apply the effective theme before the first frame, including on a first visit.
(() => {
  const system = window.matchMedia("(prefers-color-scheme: dark)");
  function applyPreference() {
    let theme = system.matches ? "dark" : "light";
    try {
      const saved = localStorage.getItem("cc_theme");
      if (saved === "light" || saved === "dark") theme = saved;
    } catch {
      // Private browsers can disable storage.
    }
    document.documentElement.dataset.theme = theme;
  }

  applyPreference();
  system.addEventListener("change", applyPreference);
})();
