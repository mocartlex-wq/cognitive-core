// Use the owner's saved site preference before the first frame is painted.
try {
  const savedTheme = localStorage.getItem("cc_theme");
  if (savedTheme === "light" || savedTheme === "dark") document.documentElement.dataset.theme = savedTheme;
} catch {
  // Storage can be unavailable for an offline or private page.
}
