/* ---------- 文件树与 Monaco 编辑器 ---------- */

/* ---------- 文件树 ---------- */
function makeNode(entry, level) {
  const row = document.createElement("div");
  row.className = "node";
  row.style.paddingLeft = (10 + level * 14) + "px";
  row.dataset.path = entry.path;
  row.dataset.type = entry.type;
  const caret = document.createElement("span");
  caret.className = "caret";
  caret.textContent = entry.type === "dir" ? "▸" : "";
  const name = document.createElement("span");
  name.className = "name";
  name.textContent = entry.name;
  row.append(caret, name);

  if (entry.type === "dir") {
    const children = document.createElement("div");
    children.className = "children";
    row.addEventListener("click", async (event) => {
      event.stopPropagation();
      const open = children.classList.toggle("open");
      caret.textContent = open ? "▾" : "▸";
      if (open && !children.dataset.loaded) {
        await loadChildren(entry.path, children, level + 1);
        children.dataset.loaded = "1";
      }
    });
    row.dataset.children = "1";
    row.after(children);
    return { row, children };
  }
  row.addEventListener("click", () => openFile(entry.path));
  return { row, children: null };
}

async function loadChildren(path, container, level) {
  container.innerHTML = "";
  try {
    const data = await api("/api/tree?path=" + encodeURIComponent(path));
    if (!data.entries.length) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "（空）";
      empty.style.paddingLeft = (10 + level * 14) + "px";
      container.appendChild(empty);
      return;
    }
    for (const entry of data.entries) {
      const node = makeNode(entry, level);
      container.appendChild(node.row);
      if (node.children) container.appendChild(node.children);
    }
  } catch (error) {
    logMessage("error", "读取目录失败：" + error.message);
  }
}

async function refreshTree() {
  const tree = $("tree");
  tree.innerHTML = "";
  try {
    const data = await api("/api/tree");
    for (const entry of data.entries) {
      const node = makeNode(entry, 0);
      tree.appendChild(node.row);
      if (node.children) tree.appendChild(node.children);
    }
    if (!data.entries.length) tree.innerHTML = '<div class="empty">工作目录为空</div>';
  } catch (error) {
    showEmpty(tree, error.message, "empty");
  }
}

/* ---------- 编辑器 ---------- */
function effectiveDark() {
  const attr = document.documentElement.dataset.theme;
  if (attr === "dark") return true;
  if (attr === "light") return false;
  return window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function editorTheme() {
  return effectiveDark() ? "vs-dark" : "vs";
}

function applyTheme(mode) {
  if (mode === "auto") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.dataset.theme = mode;
  settings.theme = mode;
  persistSettings();
  if (window.monaco) monaco.editor.setTheme(editorTheme());
}

function createEditor() {
  if (!window.monaco) return;
  state.editor = monaco.editor.create($("editor"), {
    value: "",
    language: "plaintext",
    theme: editorTheme(),
    automaticLayout: true,
    fontSize: 13,
    minimap: { enabled: false },
    scrollBeyondLastLine: false,
    tabSize: 4,
  });
  state.editor.addCommand(monaco.KeyMod.CtrlCmd | monaco.KeyCode.KeyS, saveActive);
  state.editor.addCommand(monaco.KeyCode.F12, gotoDefinition);
  state.editor.addCommand(monaco.KeyMod.Shift | monaco.KeyCode.F12, findReferences);
}

/* ---------- 代码导航 ---------- */
function currentWord() {
  if (!state.editor) return "";
  const position = state.editor.getPosition();
  const word = state.editor.getModel().getWordAtPosition(position);
  return word ? word.word : "";
}

async function gotoDefinition() {
  const word = currentWord();
  if (!word) return;
  const data = await api(
    "/api/definition?symbol=" + encodeURIComponent(word) + "&path=" + encodeURIComponent(state.active || "")
  );
  const items = data.definitions || [];
  if (!items.length) {
    setStatus("没找到 " + word + " 的定义", "idle");
    return;
  }
  const target = items[0];
  await openFile(target.path, target.line);
  setStatus("已跳转到 " + target.path + ":" + target.line, "idle");
}

async function findReferences() {
  const word = currentWord();
  if (!word) return;
  const data = await api("/api/references?symbol=" + encodeURIComponent(word));
  const items = data.references || [];
  $("searchInput").value = word;
  switchSideTab("search");
  renderSearchResults(items);
  setStatus(items.length ? "找到 " + items.length + " 处引用" : "没有引用", "idle");
}

function renderTabs() {
  const tabs = $("tabs");
  tabs.innerHTML = "";
  for (const [path, item] of state.tabs) {
    const tab = document.createElement("div");
    tab.className = "tab" + (path === state.active ? " active" : "") + (item.dirty ? " dirty" : "");
    const dot = document.createElement("span");
    dot.className = "dot";
    const name = document.createElement("span");
    name.className = "name";
    name.textContent = path.split("/").pop();
    name.title = path;
    const close = document.createElement("span");
    close.className = "close";
    close.textContent = "×";
    close.addEventListener("click", (event) => { event.stopPropagation(); closeTab(path); });
    tab.append(dot, name, close);
    tab.addEventListener("click", () => setActive(path));
    tabs.appendChild(tab);
  }
}

async function openFile(path, line) {
  if (!state.editor) return;
  if (!state.models.has(path)) {
    try {
      const data = await api("/api/file?path=" + encodeURIComponent(path));
      const model = monaco.editor.createModel(data.content, languageOf(path), monaco.Uri.parse("inmemory:///" + path));
      model.onDidChangeContent(() => {
        const item = state.tabs.get(path);
        if (item) { item.dirty = true; renderTabs(); }
      });
      state.models.set(path, model);
      state.tabs.set(path, { dirty: false });
    } catch (error) {
      logMessage("error", "打开文件失败：" + error.message);
      return;
    }
  }
  setActive(path);
  if (line && state.editor) {
    state.editor.revealLineInCenter(line);
    state.editor.setPosition({ lineNumber: line, column: 1 });
    state.editor.focus();
  }
}

function setActive(path) {
  state.active = path;
  state.editor.setModel(state.models.get(path));
  renderTabs();
  markTree(path);
  updateWelcome();
}

function markTree(path) {
  document.querySelectorAll(".node").forEach((node) => {
    node.classList.toggle("active", node.dataset.path === path);
  });
}

function closeTab(path) {
  const model = state.models.get(path);
  if (model) model.dispose();
  state.models.delete(path);
  state.tabs.delete(path);
  if (state.active === path) {
    const next = [...state.tabs.keys()].pop() || null;
    state.active = next;
    state.editor.setModel(next ? state.models.get(next) : null);
  }
  renderTabs();
  updateWelcome();
}

async function saveActive() {
  if (!state.active) return;
  const model = state.models.get(state.active);
  try {
    await api("/api/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.active, content: model.getValue() }),
    });
    const item = state.tabs.get(state.active);
    if (item) { item.dirty = false; renderTabs(); }
    logMessage("status", "已保存 " + state.active);
  } catch (error) {
    logMessage("error", "保存失败：" + error.message);
  }
}

async function reloadIfOpen(path) {
  if (!state.models.has(path)) return;
  const tab = state.tabs.get(path);
  if (tab && tab.dirty) {
    logMessage("status", path + " 有未保存修改，已跳过自动刷新");
    return;
  }
  try {
    const data = await api("/api/file?path=" + encodeURIComponent(path));
    const model = state.models.get(path);
    if (model) {
      model.setValue(data.content);
      const item = state.tabs.get(path);
      if (item) { item.dirty = false; renderTabs(); }
    }
  } catch (error) { /* 文件可能被删除 */ }
}
