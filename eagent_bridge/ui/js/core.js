/* ---------- 核心：状态、设置、会话记录、请求与日志 ---------- */

const state = {
  editor: null, diffEditor: null,
  models: new Map(), tabs: new Map(), active: null,
  runId: null, pending: null, source: null, lastEventId: 0,
  diffModels: [],
  refs: { skills: [], files: [], mcp: [], symbols: [] },
  refItems: [], refIndex: 0, refKind: null, refStart: 0, refSeq: 0,
  skillCache: null, mcpServers: null, ruleCache: null, symbolCache: null, git: null,
  commandBlock: null,
  queueCard: null, queueTimer: null,
  pendingImages: [],
  termCwd: "",
};

const $ = (id) => document.getElementById(id);
const messages = $("messages");

const STORE_KEY = "eagent-studio-settings";
const BUILTIN_MODELS = [
  { id: "web-deepseek", name: "DeepSeek 网页版", type: "web", site: "deepseek" },
  { id: "web-doubao", name: "豆包网页版", type: "web", site: "doubao" },
  { id: "web-yuanbao", name: "元宝网页版", type: "web", site: "yuanbao" },
];
const DEFAULT_SETTINGS = {
  maxSteps: "12",
  autoAllow: false,
  planFirst: false,
  theme: "auto",
  whitelist: ["python", "pytest", "git status", "git diff", "git log", "npm test", "npm run", "pip list", "dir", "type"],
  workspace: "",
  models: BUILTIN_MODELS.slice(),
  activeModelId: "web-deepseek",
  fallbackModelId: "",
  reviewAfterRun: true,
  agentMode: "code",
  enabledSkills: [],
  enabledRules: null,
  panels: { sidebar: false, chat: false, terminal: false },
  layout: { sidebar: 250, chat: 372, terminal: 210 },
  guided: false,
  zoom: 1,
};

function loadSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem(STORE_KEY) || "{}");
    const merged = { ...DEFAULT_SETTINGS, ...stored };
    if (!Array.isArray(merged.models) || !merged.models.length) {
      merged.models = BUILTIN_MODELS.slice();
    }
    merged.panels = { ...DEFAULT_SETTINGS.panels, ...(stored.panels || {}) };
    merged.layout = { ...DEFAULT_SETTINGS.layout, ...(stored.layout || {}) };
    return merged;
  } catch (error) {
    return { ...DEFAULT_SETTINGS, panels: { ...DEFAULT_SETTINGS.panels }, layout: { ...DEFAULT_SETTINGS.layout } };
  }
}

function persistSettings() {
  try { localStorage.setItem(STORE_KEY, JSON.stringify(settings)); } catch (error) { /* 忽略 */ }
}

let settings = loadSettings();

/* ---------- 聊天会话（按工作目录隔离）---------- */
const CHATS_KEY = "eagent-studio-chats";
const CHAT_STORE_LIMIT = 3_500_000;
let chatSaveTimer = null;
let replayingChat = false;

function loadChatStore() {
  try {
    return JSON.parse(localStorage.getItem(CHATS_KEY) || "{}") || {};
  } catch (error) {
    return {};
  }
}

let chatStore = loadChatStore();

function persistChatStore(immediate = false) {
  if (immediate) {
    if (chatSaveTimer) {
      clearTimeout(chatSaveTimer);
      chatSaveTimer = null;
    }
    writeChatStore();
    return;
  }
  if (chatSaveTimer) clearTimeout(chatSaveTimer);
  chatSaveTimer = setTimeout(() => {
    chatSaveTimer = null;
    writeChatStore();
  }, 400);
}

function writeChatStore() {
  let payload;
  try {
    payload = JSON.stringify(chatStore);
  } catch (error) {
    return;
  }
  if (payload.length > CHAT_STORE_LIMIT) {
    pruneChatStore();
    try { payload = JSON.stringify(chatStore); } catch (error) { return; }
  }
  try {
    localStorage.setItem(CHATS_KEY, payload);
  } catch (error) {
    pruneChatStore();
    try {
      localStorage.setItem(CHATS_KEY, JSON.stringify(chatStore));
    } catch (ignore) {
      console.warn("聊天记录保存失败", ignore);
    }
  }
}

function pruneChatStore() {
  const entries = [];
  for (const book of Object.values(chatStore)) {
    for (const chat of book.items || []) entries.push({ book, chat });
  }
  entries.sort((a, b) => (a.chat.updatedAt || 0) - (b.chat.updatedAt || 0));
  for (const entry of entries) {
    let size = 0;
    try { size = JSON.stringify(chatStore).length; } catch (error) { break; }
    if (size <= CHAT_STORE_LIMIT * 0.75) break;
    if (entry.chat.id === entry.book.activeId) continue;
    entry.book.items = entry.book.items.filter((item) => item.id !== entry.chat.id);
  }
}

function chatBook(workspace, create = true) {
  if (!workspace) return null;
  if (!chatStore[workspace] && create) {
    chatStore[workspace] = { activeId: "", items: [] };
  }
  return chatStore[workspace] || null;
}

function currentChat() {
  const book = chatBook(settings.workspace);
  if (!book) return null;
  return book.items.find((chat) => chat.id === book.activeId) || null;
}

function createChat() {
  const book = chatBook(settings.workspace, true);
  if (!book) return null;
  const chat = { id: "chat-" + Date.now(), createdAt: Date.now(), updatedAt: Date.now(), messages: [] };
  book.items.unshift(chat);
  book.activeId = chat.id;
  if (book.items.length > 20) {
    book.items = book.items.slice(0, 20);
  }
  persistChatStore();
  return chat;
}

function recordChatMessage(kind, text) {
  if (replayingChat) return;
  const chat = currentChat();
  if (!chat) return;
  chat.messages.push({ kind, text: String(text || "").slice(0, 1200) });
  if (chat.messages.length > 200) chat.messages.splice(0, chat.messages.length - 200);
  chat.updatedAt = Date.now();
  persistChatStore();
}

function chatTitle(chat) {
  const first = (chat.messages || []).find((message) => message.kind === "user");
  const text = first ? first.text.trim() : "";
  if (!text) return "新会话";
  return text.length > 15 ? text.slice(0, 15) + "…" : text;
}

function renderChat(chat) {
  messages.innerHTML = "";
  if (!chat) return;
  replayingChat = true;
  try {
    for (const message of chat.messages) logMessage(message.kind, message.text);
  } finally {
    replayingChat = false;
  }
}

function ensureChatForWorkspace(workspace) {
  const book = chatBook(workspace, true);
  if (!book) return null;
  if (!book.items.some((chat) => chat.id === book.activeId)) {
    if (book.items.length) {
      book.activeId = book.items[0].id;
      persistChatStore();
    } else {
      createChat();
    }
  }
  return currentChat();
}

function switchChatWorkspace(workspace) {
  const chat = ensureChatForWorkspace(workspace);
  renderChat(chat);
}

function startNewChat() {
  createChat();
  renderChat(currentChat());
}

function renderChatHistory() {
  const pop = $("chatHistoryPop");
  const book = chatBook(settings.workspace, false);
  pop.innerHTML = "";
  if (!book || !book.items.length) {
    pop.innerHTML = '<div class="ch-empty">还没有聊天记录</div>';
    return;
  }
  const sorted = [...book.items].sort((a, b) => (b.updatedAt || 0) - (a.updatedAt || 0));
  for (const chat of sorted) {
    const row = document.createElement("div");
    row.className = "ch-item" + (chat.id === book.activeId ? " active" : "");
    row.textContent = chatTitle(chat);
    row.title = chatTitle(chat);
    row.addEventListener("click", () => {
      book.activeId = chat.id;
      persistChatStore();
      renderChat(chat);
      $("chatHistoryPop").classList.add("hidden");
    });
    pop.appendChild(row);
  }
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
  } catch (error) {
    const area = document.createElement("textarea");
    area.value = text;
    area.style.position = "fixed";
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
}

function showEmpty(box, text, className = "results-empty") {
  box.innerHTML = "";
  const empty = document.createElement("div");
  empty.className = className;
  empty.textContent = text;
  box.appendChild(empty);
}

function logMessage(kind, text) {
  if (kind === "tool") return;
  const content = String(text == null ? "" : text);
  const div = document.createElement("div");
  div.className = "msg " + kind;
  if (kind === "image") {
    const image = document.createElement("img");
    image.className = "msg-image";
    image.loading = "lazy";
    image.src = "/api/image?path=" + encodeURIComponent(content) + "&token=" + encodeURIComponent(apiToken());
    div.appendChild(image);
  } else {
    div.appendChild(document.createTextNode(content));
    if (kind !== "status") {
      const copy = document.createElement("button");
      copy.type = "button";
      copy.className = "msg-copy";
      copy.textContent = "复制";
      copy.title = "复制内容";
      copy.addEventListener("click", (event) => {
        event.stopPropagation();
        copyText(content);
        copy.textContent = "已复制";
        setTimeout(() => { copy.textContent = "复制"; }, 1200);
      });
      div.appendChild(copy);
    }
  }
  messages.appendChild(div);
  while (messages.children.length > 500) {
    messages.removeChild(messages.firstChild);
  }
  messages.scrollTop = messages.scrollHeight;
  // 状态提示只在本次界面里显示，不写进历史，避免刷新后满屏都是操作回执
  if (kind !== "status") recordChatMessage(kind, content);
}

function languageOf(path) {
  const ext = (path.split(".").pop() || "").toLowerCase();
  return ({ py: "python", js: "javascript", ts: "typescript", json: "json", html: "html",
    css: "css", md: "markdown", java: "java", c: "c", cpp: "cpp", go: "go", rs: "rust",
    sh: "shell", ps1: "powershell", yml: "yaml", yaml: "yaml", txt: "plaintext" })[ext] || "plaintext";
}

function apiToken() {
  const meta = document.querySelector('meta[name="eagent-token"]');
  return meta ? meta.content : "";
}

async function api(path, options) {
  const init = options || {};
  const headers = { ...(init.headers || {}), "X-EAgent-Token": apiToken() };
  const response = await fetch(path, { ...init, headers });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(data.error || response.statusText);
    error.status = response.status;
    throw error;
  }
  return data;
}
