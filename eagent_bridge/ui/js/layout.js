/* ---------- 面板折叠、引导与缩放 ---------- */

/* ---------- 面板折叠 / 引导 / 缩放 ---------- */
function applySizes() {
  const layout = document.querySelector(".layout");
  const sizes = { ...DEFAULT_SETTINGS.layout, ...(settings.layout || {}) };
  layout.style.setProperty("--side-w", sizes.sidebar + "px");
  layout.style.setProperty("--chat-w", sizes.chat + "px");
  layout.style.setProperty("--term-h", sizes.terminal + "px");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function startResize(event, target) {
  event.preventDefault();
  const startX = event.clientX;
  const startY = event.clientY;
  const base = { ...DEFAULT_SETTINGS.layout, ...(settings.layout || {}) };
  settings.layout = settings.layout || { ...DEFAULT_SETTINGS.layout };

  const move = (moveEvent) => {
    if (target === "sidebar") {
      settings.layout.sidebar = clamp(base.sidebar + (moveEvent.clientX - startX), 180, 460);
    } else if (target === "chat") {
      settings.layout.chat = clamp(base.chat - (moveEvent.clientX - startX), 280, 560);
    } else {
      settings.layout.terminal = clamp(base.terminal - (moveEvent.clientY - startY), 120, 420);
    }
    applySizes();
  };
  const up = () => {
    document.removeEventListener("pointermove", move);
    document.removeEventListener("pointerup", up);
    persistSettings();
    if (state.editor) state.editor.layout();
  };
  document.addEventListener("pointermove", move);
  document.addEventListener("pointerup", up);
}

$("resizeSidebar").addEventListener("pointerdown", (event) => startResize(event, "sidebar"));
$("resizeChat").addEventListener("pointerdown", (event) => startResize(event, "chat"));
$("resizeTerminal").addEventListener("pointerdown", (event) => startResize(event, "terminal"));

function applyPanels() {
  applySizes();
  const panels = settings.panels || {};
  const layout = document.querySelector(".layout");
  layout.classList.toggle("sidebar-closed", !panels.sidebar);
  layout.classList.toggle("chat-closed", !panels.chat);
  layout.classList.toggle("terminal-closed", !panels.terminal);
  document.querySelector(".sidebar").classList.toggle("collapsed", !panels.sidebar);
  document.querySelector(".chat").classList.toggle("collapsed", !panels.chat);
  document.querySelector(".terminal").classList.toggle("collapsed", !panels.terminal);
  $("handleSidebar").textContent = panels.sidebar ? "<" : ">";
  $("handleChat").textContent = panels.chat ? ">" : "<";
  $("handleTerminal").textContent = panels.terminal ? "▾" : "▴";
  if (panels.terminal) {
    setTimeout(() => $("termInput").focus(), 60);
  }
}

function togglePanel(name) {
  settings.panels[name] = !settings.panels[name];
  persistSettings();
  applyPanels();
  if (state.editor) setTimeout(() => state.editor.layout(), 80);
}

$("handleSidebar").addEventListener("click", () => togglePanel("sidebar"));
$("handleChat").addEventListener("click", () => togglePanel("chat"));
$("handleTerminal").addEventListener("click", () => togglePanel("terminal"));

const ONBOARD_STEPS = [
  {
    title: "欢迎使用",
    body:
      '<ul class="onboard-list">' +
      "<li><b>文件</b>：左侧文件树，点顶栏「文件」折叠或展开，右键可新建、重命名、删除</li>" +
      "<li><b>聊天</b>：右侧 Agent 面板，用自然语言描述任务即可</li>" +
      "<li><b>终端</b>：底部 PowerShell，点一下就能直接输入命令</li>" +
      "<li><b>编辑器</b>：点击文件打开，Ctrl+S 保存，改动前会让你确认 diff</li>" +
      "</ul>",
  },
  {
    title: "引用：把上下文精准喂给 Agent",
    body:
      '<ul class="onboard-list">' +
      "<li><code>/</code> 引用技能：把一段固定的工作方法注入这次任务</li>" +
      "<li><code>@</code> 引用文件或目录：让 Agent 直接读到内容</li>" +
      "<li><code>//</code> 引用 MCP 服务：优先使用指定桌面工具</li>" +
      "<li><code>&amp;</code> 引用代码符号：只取这个函数或类的源码</li>" +
      "</ul>",
  },
  {
    title: "快捷键",
    body:
      '<ul class="onboard-list">' +
      "<li><b>Ctrl+P</b> 快速打开文件 · <b>Ctrl+Shift+F</b> 全局搜索</li>" +
      "<li><b>F12</b> 跳转定义 · <b>Shift+F12</b> 查找引用</li>" +
      "<li><b>Ctrl+Enter</b> 发送任务（运行中发送会自动排队）· <b>Ctrl+S</b> 保存</li>" +
      "<li><b>Ctrl+加减号</b> 缩放界面 · <b>Esc</b> 关闭弹层</li>" +
      "</ul>",
  },
  {
    title: "模型与安全",
    body:
      '<ul class="onboard-list">' +
      "<li><b>模型</b>：网页 AI（不消耗接口积分）、OpenAI 兼容接口、Ollama 任选，可再配一个备用模型</li>" +
      "<li><b>确认</b>：改文件和跑命令前会弹 diff 或命令确认，也可以把常用命令加进白名单</li>" +
      "<li><b>计划</b>：开启「先出计划」后，Agent 会先给步骤，确认后才动手</li>" +
      "<li><b>回滚</b>：每次任务改过的文件都有快照，可在侧栏「快照」里一键还原</li>" +
      "</ul>",
  },
];

let onboardStep = 0;
const ONBOARD_KEY = "eagent-onboard-version";
const ONBOARD_VERSION = "3";

function renderOnboardStep() {
  const step = ONBOARD_STEPS[onboardStep];
  $("onboardTitle").textContent = step.title;
  $("onboardBody").innerHTML = step.body;
  $("onboardIndex").textContent = onboardStep + 1 + " / " + ONBOARD_STEPS.length;
  $("onboardPrev").disabled = onboardStep === 0;
  $("onboardNext").textContent = onboardStep === ONBOARD_STEPS.length - 1 ? "开始使用" : "下一步";
  // 到最后一步就没什么可跳的了
  $("onboardSkip").classList.toggle("hidden", onboardStep === ONBOARD_STEPS.length - 1);
}

function closeOnboarding() {
  settings.guided = true;
  persistSettings();
  try { localStorage.setItem(ONBOARD_KEY, ONBOARD_VERSION); } catch (error) { /* 忽略 */ }
  $("onboarding").classList.add("hidden");
}

function maybeShowOnboarding() {
  let seen = "";
  try { seen = localStorage.getItem(ONBOARD_KEY) || ""; } catch (error) { seen = ""; }
  if (seen === ONBOARD_VERSION) return;
  onboardStep = 0;
  renderOnboardStep();
  $("onboarding").classList.remove("hidden");
}

$("onboardNext").addEventListener("click", () => {
  if (onboardStep >= ONBOARD_STEPS.length - 1) {
    closeOnboarding();
    return;
  }
  onboardStep += 1;
  renderOnboardStep();
});
$("onboardPrev").addEventListener("click", () => {
  if (onboardStep === 0) return;
  onboardStep -= 1;
  renderOnboardStep();
});
$("onboardSkip").addEventListener("click", () => {
  $("onboardNever").checked = true;  // 跳过即视为不再引导
  closeOnboarding();
});

function applyZoom() {
  const zoom = settings.zoom || 1;
  document.documentElement.style.zoom = zoom;
  if ($("zoomLabel")) $("zoomLabel").textContent = Math.round(zoom * 100) + "%";
  if (state.editor) state.editor.layout();
  if (state.diffEditor) state.diffEditor.layout();
}

function setZoom(value) {
  settings.zoom = Math.min(1.6, Math.max(0.75, Math.round(value * 100) / 100));
  persistSettings();
  applyZoom();
}

$("zoomReset").addEventListener("click", () => setZoom(1));

function updateWelcome() {
  const empty = !state.active;
  $("welcome").classList.toggle("hidden", !empty);
  document.querySelector(".editor-wrap").classList.toggle("empty", empty);
  if (!empty && state.editor) state.editor.layout();
}
