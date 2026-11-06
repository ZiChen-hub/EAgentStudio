/* ---------- 聊天引用：/ 技能 · @ 文件 · // MCP · & 符号 ---------- */

/* ---------- 聊天引用：/ 技能 · @ 文件 · // MCP · & 符号 ---------- */
function renderRefChips() {
  const box = $("refChips");
  box.innerHTML = "";
  const labels = { skills: "技能", files: "文件", mcp: "MCP", symbols: "符号" };
  for (const kind of ["skills", "files", "mcp", "symbols"]) {
    for (const value of state.refs[kind]) {
      const chip = document.createElement("span");
      chip.className = "ref-chip";
      const shown = kind === "symbols" ? (value.split("|")[2] || value) : value;
      chip.textContent = labels[kind] + " · " + shown;
      const close = document.createElement("button");
      close.textContent = "×";
      close.addEventListener("click", () => {
        state.refs[kind] = state.refs[kind].filter((item) => item !== value);
        renderRefChips();
      });
      chip.appendChild(close);
      box.appendChild(chip);
    }
  }
}

function clearReferences() {
  state.refs = { skills: [], files: [], mcp: [], symbols: [] };
  state.pendingImages = [];
  renderRefChips();
  renderImageChips();
}

async function ensureRefData(kind, query) {
  if (kind === "skills" && !state.skillCache) {
    state.skillCache = (await api("/api/skills")).skills || [];
  }
  if (kind === "files" && !allFiles) {
    allFiles = (await api("/api/files")).files || [];
  }
  if (kind === "mcp" && !state.mcpServers) {
    state.mcpServers = (await api("/api/mcp")).servers || [];
  }
  if (kind === "symbols") {
    const data = await api("/api/symbols?q=" + encodeURIComponent(query || "") + "&limit=40");
    state.symbolCache = data.symbols || [];
  }
}

async function showRefPopup(kind, query, start) {
  state.refKind = kind;
  state.refStart = start;
  state.refIndex = 0;
  state.refSeq += 1;
  const seq = state.refSeq;
  try {
    await ensureRefData(kind, query);
  } catch (error) {
    return;
  }
  if (state.refKind !== kind || state.refSeq !== seq) return;

  const needle = query.toLowerCase();
  const items = [];
  if (kind === "skills") {
    for (const skill of state.skillCache || []) {
      if (needle && !skill.name.toLowerCase().includes(needle) && !skill.id.toLowerCase().includes(needle)) continue;
      items.push({ value: skill.id, label: skill.name, hint: "技能", token: "/" + skill.id });
    }
  } else if (kind === "files") {
    for (const file of allFiles || []) {
      if (needle && !file.toLowerCase().includes(needle)) continue;
      items.push({ value: file, label: file, hint: "文件", token: "@" + file });
    }
  } else if (kind === "symbols") {
    for (const sym of state.symbolCache || []) {
      const short = sym.path.split("/").pop();
      items.push({
        value: sym.path + "|" + sym.line + "|" + sym.name,
        label: sym.name,
        hint: sym.kind + " · " + short + ":" + sym.line,
        token: "&" + sym.name,
      });
    }
  } else {
    for (const server of state.mcpServers || []) {
      if (needle && !server.name.toLowerCase().includes(needle)) continue;
      items.push({ value: server.name, label: server.name, hint: "MCP", token: "//" + server.name });
    }
  }
  state.refItems = items.slice(0, 40);
  renderRefPopup();
}

function renderRefPopup() {
  const box = $("refPopup");
  box.innerHTML = "";
  if (!state.refItems.length) {
    box.innerHTML = '<div class="rp-empty">没有匹配项</div>';
    box.classList.remove("hidden");
    return;
  }
  state.refItems.forEach((item, index) => {
    const row = document.createElement("div");
    row.className = "rp-item" + (index === state.refIndex ? " active" : "");
    const label = document.createElement("span");
    label.textContent = item.label;
    const hint = document.createElement("span");
    hint.className = "rp-kind";
    hint.textContent = item.hint;
    row.append(label, hint);
    row.addEventListener("click", () => selectRef(item));
    box.appendChild(row);
  });
  box.classList.remove("hidden");
}

function selectRef(item) {
  const task = $("task");
  const caret = task.selectionStart;
  const before = task.value.slice(0, caret);
  const after = task.value.slice(caret);
  const head = before.slice(0, state.refStart);
  task.value = head + item.token + " " + after;
  if (!state.refs[state.refKind].includes(item.value)) {
    state.refs[state.refKind].push(item.value);
  }
  renderRefChips();
  closeRefPopup();
  task.focus();
  const position = (head + item.token + " ").length;
  task.setSelectionRange(position, position);
}

function closeRefPopup() {
  $("refPopup").classList.add("hidden");
  state.refKind = null;
  state.refItems = [];
  state.refIndex = 0;
}

function updateRefPopup() {
  const task = $("task");
  const before = task.value.slice(0, task.selectionStart);
  const mcpMatch = before.match(/\/\/([^\s]*)$/);
  if (mcpMatch) { showRefPopup("mcp", mcpMatch[1], before.lastIndexOf("//")); return; }
  const skillMatch = before.match(/(?:^|\s)\/([^\s/]*)$/);
  if (skillMatch) { showRefPopup("skills", skillMatch[1], before.lastIndexOf("/")); return; }
  const fileMatch = before.match(/@([^\s]*)$/);
  if (fileMatch) { showRefPopup("files", fileMatch[1], before.lastIndexOf("@")); return; }
  const symbolMatch = before.match(/(?:^|[^\w&])&([^\s&]*)$/);
  if (symbolMatch) { showRefPopup("symbols", symbolMatch[1], before.lastIndexOf("&")); return; }
  closeRefPopup();
}

$("task").addEventListener("input", updateRefPopup);
$("task").addEventListener("keydown", (event) => {
  if ($("refPopup").classList.contains("hidden")) return;
  if (event.key === "ArrowDown") {
    event.preventDefault();
    state.refIndex = Math.min(state.refIndex + 1, state.refItems.length - 1);
    renderRefPopup();
  } else if (event.key === "ArrowUp") {
    event.preventDefault();
    state.refIndex = Math.max(state.refIndex - 1, 0);
    renderRefPopup();
  } else if (event.key === "Enter" || event.key === "Tab") {
    const item = state.refItems[state.refIndex];
    if (item) { event.preventDefault(); selectRef(item); }
  } else if (event.key === "Escape") {
    closeRefPopup();
  }
});

$("maxSteps").addEventListener("change", () => { settings.maxSteps = $("maxSteps").value; persistSettings(); });
$("autoAllow").addEventListener("change", () => { settings.autoAllow = $("autoAllow").checked; persistSettings(); });
$("planFirst").addEventListener("change", () => { settings.planFirst = $("planFirst").checked; persistSettings(); });
$("theme").addEventListener("change", () => applyTheme($("theme").value));
