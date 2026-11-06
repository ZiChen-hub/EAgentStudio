/* ---------- 状态栏、弹窗与侧边栏分段 ---------- */

/* ---------- 设置 / 状态 / 弹窗 ---------- */
let allFiles = null;
let dialogResolve = null;
let ctxTarget = null;

function applySettingsToUI() {
  $("maxSteps").value = String(settings.maxSteps);
  $("autoAllow").checked = !!settings.autoAllow;
  $("planFirst").checked = !!settings.planFirst;
  $("reviewAfterRun").checked = settings.reviewAfterRun !== false;
  $("theme").value = settings.theme;
  applyTheme(settings.theme);
  applyPanels();
  applyZoom();
  renderActiveModels();
  updateModelChip();
  updateModeChip();
}

function setStatus(text, kind = "idle") {
  $("statusText").textContent = text;
  $("statusText").title = text;
  $("statusDot").className = "status-dot" +
    (kind === "running" ? " running" : kind === "error" ? " error" : "");
}

function dialog(options) {
  return new Promise((resolve) => {
    // 已有弹窗未关闭时先把它按取消收掉，避免旧 Promise 永远悬着
    if (dialogResolve) dialogResolve(null);
    $("dialogTitle").textContent = options.title || "提示";
    $("dialogMessage").textContent = options.message || "";
    const field = $("dialogInput");
    field.classList.toggle("hidden", !options.input);
    field.value = options.value || "";
    $("dialog").classList.remove("hidden");
    if (options.input) setTimeout(() => { field.focus(); field.select(); }, 40);
    dialogResolve = (value) => {
      $("dialog").classList.add("hidden");
      dialogResolve = null;
      resolve(value);
    };
  });
}

function closeDialog(value) {
  if (dialogResolve) dialogResolve(value);
}

$("dialogOk").addEventListener("click", () => {
  const field = $("dialogInput");
  closeDialog(field.classList.contains("hidden") ? true : field.value.trim());
});
$("dialogCancel").addEventListener("click", () => closeDialog(null));
$("dialogInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") { event.preventDefault(); $("dialogOk").click(); }
});
$("dialog").addEventListener("click", (event) => {
  if (event.target === $("dialog")) closeDialog(null);
});
for (const id of ["quickOpen", "settingsModal"]) {
  $(id).addEventListener("click", (event) => { if (event.target === $(id)) $(id).classList.add("hidden"); });
}

/* ---------- 侧边栏分段 ---------- */
function switchSideTab(name) {
  document.querySelectorAll("#sideTabs button").forEach((button) => {
    button.classList.toggle("active", button.dataset.tab === name);
  });
  for (const key of ["files", "search", "git", "snapshots", "tests", "history"]) {
    $("pane-" + key).classList.toggle("hidden", key !== name);
  }
  if (name === "history") loadHistory();
  if (name === "git") loadGit();
  if (name === "snapshots") loadSnapshots();
  if (name === "tests") loadTestCommand();
  if (name === "search") setTimeout(() => $("searchInput").focus(), 40);
}

$("sideTabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) switchSideTab(button.dataset.tab);
});
