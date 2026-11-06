/* ---------- 快捷键与 Monaco 启动 ---------- */

/* ---------- 快捷键 ---------- */
document.addEventListener("keydown", (event) => {
  if (event.target === termInput) return;
  const meta = event.ctrlKey || event.metaKey;
  const zoom = settings.zoom || 1;
  if (meta && (event.key === "+" || event.key === "=")) { event.preventDefault(); setZoom(zoom + 0.1); return; }
  if (meta && event.key === "-") { event.preventDefault(); setZoom(zoom - 0.1); return; }
  if (meta && event.key === "0") { event.preventDefault(); setZoom(1); return; }
  if (meta && event.key.toLowerCase() === "p") { event.preventDefault(); openQuick(); }
  else if (meta && event.shiftKey && event.key.toLowerCase() === "f") { event.preventDefault(); switchSideTab("search"); }
  else if (meta && event.key.toLowerCase() === "s") { event.preventDefault(); saveActive(); }
  else if (event.key === "Escape") {
    $("quickOpen").classList.add("hidden");
    $("settingsModal").classList.add("hidden");
    $("ctxMenu").classList.add("hidden");
    $("modelEditor").classList.add("hidden");
    $("skillEditor").classList.add("hidden");
    $("ruleEditor").classList.add("hidden");
    $("mcpEditor").classList.add("hidden");
    $("modelMenu").classList.add("hidden");
    closeRefPopup();
    if (!$("dialog").classList.contains("hidden")) closeDialog(null);
  }
});

if (window.require) {
  require.config({ paths: { vs: window.MONACO_BASE } });
  require(["vs/editor/editor.main"], function () {
    if (typeof monaco !== "undefined") window.monaco = monaco;
    boot();
  });
} else {
  setTimeout(boot, 3000);
}
