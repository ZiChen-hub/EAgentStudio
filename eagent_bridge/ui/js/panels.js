/* ---------- 搜索、历史、测试、快照、快速打开与文件操作 ---------- */

/* ---------- 搜索 ---------- */
let searchTimer = null;
$("searchInput").addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(runSearch, 350);
});

async function runSearch() {
  const query = $("searchInput").value.trim();
  const box = $("searchResults");
  if (!query) { box.innerHTML = ""; return; }
  box.innerHTML = '<div class="results-empty">搜索中…</div>';
  try {
    const data = await api("/api/search?q=" + encodeURIComponent(query));
    renderSearchResults(data.results || []);
  } catch (error) {
    showEmpty(box, error.message);
  }
}

function renderSearchResults(results) {
  const box = $("searchResults");
  box.innerHTML = "";
  if (!results.length) {
    box.innerHTML = '<div class="results-empty">没有匹配结果</div>';
    return;
  }
  for (const item of results) {
    const row = document.createElement("div");
    row.className = "result";
    const path = document.createElement("div");
    path.className = "r-path";
    path.textContent = item.path + ":" + item.line;
    const text = document.createElement("div");
    text.className = "r-text";
    text.textContent = item.text;
    row.append(path, text);
    row.addEventListener("click", () => openFile(item.path, item.line));
    box.appendChild(row);
  }
}

/* ---------- 会话历史 ---------- */
function formatTime(seconds) {
  return new Date((seconds || 0) * 1000).toLocaleString();
}

async function loadHistory() {
  const box = $("historyList");
  box.innerHTML = '<div class="results-empty">加载中…</div>';
  try {
    const data = await api("/api/sessions");
    box.innerHTML = "";
    if (!data.sessions.length) {
      box.innerHTML = '<div class="results-empty">还没有历史会话</div>';
      return;
    }
    for (const item of data.sessions) {
      const row = document.createElement("div");
      row.className = "result";
      const task = document.createElement("div");
      task.className = "r-task";
      task.textContent = item.task || "（无任务描述）";
      const meta = document.createElement("div");
      meta.className = "r-meta";
      meta.textContent = formatTime(item.created) + (item.error ? " · 失败" : " · 完成");
      row.append(task, meta);
      row.addEventListener("click", () => loadSession(item.id));
      box.appendChild(row);
    }
  } catch (error) {
    showEmpty(box, error.message);
  }
}

async function loadSession(id) {
  try {
    const data = await api("/api/sessions/" + encodeURIComponent(id));
    messages.innerHTML = "";
    logMessage("user", data.task || "");
    for (const event of data.events || []) {
      if (event.type === "agent") logMessage(event.kind, event.text || "");
      else if (event.type === "approval") {
        const preview = event.preview || {};
        logMessage("tool", "[" + event.tool + "] " + (preview.path || preview.command || ""));
      } else if (event.type === "run_error") logMessage("error", event.message || "");
    }
    logMessage("finish", data.summary || data.error || "会话记录");
    $("task").value = data.task || "";
    switchSideTab("files");
    setStatus("已载入历史会话");
  } catch (error) {
    logMessage("error", error.message);
  }
}

/* ---------- 测试 ---------- */
async function loadTestCommand() {
  if ($("testCommand").value.trim()) return;
  try {
    const data = await api("/api/tests/detect");
    $("testCommand").value = data.command || "";
  } catch (error) {
    /* 探测失败就留空，用户自己填 */
  }
}

async function runTests() {
  const button = $("testRun");
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "运行中…";
  $("testStatus").className = "test-status";
  $("testStatus").textContent = "正在运行…";
  $("testOutput").textContent = "";
  try {
    const data = await api("/api/tests/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command: $("testCommand").value.trim() }),
    });
    renderTestResult(data);
  } catch (error) {
    $("testStatus").className = "test-status bad";
    $("testStatus").textContent = "运行失败：" + error.message;
  } finally {
    button.disabled = false;
    button.textContent = "运行";
  }
}

function renderTestResult(data) {
  const box = $("testFailures");
  box.innerHTML = "";
  const ok = data.exit_code === 0;
  const parts = [ok ? "通过" : "失败"];
  if (data.passed) parts.push("通过 " + data.passed);
  if (data.failed) parts.push("失败 " + data.failed);
  parts.push("退出码 " + data.exit_code);
  parts.push(data.duration + "s");
  $("testStatus").className = "test-status " + (ok ? "ok" : "bad");
  $("testStatus").textContent = parts.join(" · ");
  $("testOutput").textContent = data.output || "";

  const failures = data.failures || [];
  if (!failures.length) {
    if (!ok) box.innerHTML = '<div class="results-empty">没有解析到失败位置，请看下方原始输出</div>';
    return;
  }
  for (const item of failures) {
    const row = document.createElement("div");
    row.className = "result";
    const name = document.createElement("div");
    name.className = "r-task";
    name.textContent = item.test || item.file;
    const path = document.createElement("div");
    path.className = "r-path";
    path.textContent = item.file + ":" + item.line;
    row.append(name, path);
    if (item.message) row.title = item.message;
    row.addEventListener("click", () => openFile(item.file, item.line));
    box.appendChild(row);
  }
}

$("testRun").addEventListener("click", runTests);

/* ---------- 快照 ---------- */
async function loadSnapshots() {
  const box = $("snapshotsList");
  box.innerHTML = '<div class="results-empty">加载中…</div>';
  try {
    const data = await api("/api/snapshots");
    box.innerHTML = "";
    if (!data.snapshots.length) {
      box.innerHTML = '<div class="results-empty">还没有快照。Agent 改过文件后会自动记录。</div>';
      return;
    }
    for (const item of data.snapshots) {
      const row = document.createElement("div");
      row.className = "result snap";
      const main = document.createElement("div");
      main.className = "snap-main";
      const task = document.createElement("div");
      task.className = "r-task";
      task.textContent = item.task || "（无任务描述）";
      const meta = document.createElement("div");
      meta.className = "r-meta";
      meta.textContent =
        formatTime(item.created) +
        " · " + item.count + " 个文件" +
        (item.restored ? " · 已回滚" : "");
      main.append(task, meta);
      const act = document.createElement("button");
      act.className = "mini-btn";
      act.textContent = "回滚";
      act.addEventListener("click", (event) => {
        event.stopPropagation();
        restoreSnapshot(item);
      });
      row.append(main, act);
      row.title = (item.files || []).join("\n");
      box.appendChild(row);
    }
  } catch (error) {
    showEmpty(box, error.message);
  }
}

async function restoreSnapshot(item) {
  const list = (item.files || []).slice(0, 8).join("\n");
  const more = item.count > 8 ? "\n… 共 " + item.count + " 个文件" : "";
  if (!(await dialog({
    title: "回滚这次改动",
    message: "将把这些文件恢复到任务开始前：\n" + list + more + "\n\n当前内容会被覆盖。",
  }))) return;
  try {
    const data = await api("/api/snapshots/restore", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: item.id }),
    });
    logMessage("status", "已回滚 " + data.restored.length + " 个文件，删除 " + data.removed.length + " 个新建文件");
    await loadSnapshots();
    await refreshTree();
    for (const path of data.restored) reloadIfOpen(path);
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("snapshotRefresh").addEventListener("click", loadSnapshots);

/* ---------- 快速打开 ---------- */
$("quickInput").addEventListener("input", renderQuick);
$("quickInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter") {
    const first = $("quickList").querySelector(".palette-item");
    if (first) first.click();
  }
});

async function openQuick() {
  $("quickOpen").classList.remove("hidden");
  $("quickInput").value = "";
  renderQuick();
  $("quickInput").focus();
  if (!allFiles) {
    try {
      allFiles = (await api("/api/files")).files;
    } catch (error) {
      logMessage("error", error.message);
    }
    renderQuick();
  }
}

function renderQuick() {
  const list = $("quickList");
  if (!allFiles) {
    list.innerHTML = '<div class="palette-empty">加载中…</div>';
    return;
  }
  const query = $("quickInput").value.trim().toLowerCase();
  const items = (query ? allFiles.filter((file) => file.toLowerCase().includes(query)) : allFiles).slice(0, 60);
  list.innerHTML = "";
  if (!items.length) {
    list.innerHTML = '<div class="palette-empty">没有匹配文件</div>';
    return;
  }
  for (const file of items) {
    const row = document.createElement("div");
    row.className = "palette-item";
    row.textContent = file;
    row.addEventListener("click", () => {
      $("quickOpen").classList.add("hidden");
      openFile(file);
    });
    list.appendChild(row);
  }
}

/* ---------- 文件操作 ---------- */
async function createFileAt(base) {
  const path = await dialog({
    title: "新建文件",
    message: "输入相对工作目录的路径",
    input: true,
    value: base ? base + "/" : "",
  });
  if (!path) return;
  try {
    await api("/api/fs/file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, content: "" }),
    });
    allFiles = null;
    await refreshTree();
    await openFile(path);
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function createFolderAt(base) {
  const path = await dialog({
    title: "新建文件夹",
    message: "输入相对工作目录的路径",
    input: true,
    value: base ? base + "/" : "",
  });
  if (!path) return;
  try {
    await api("/api/fs/mkdir", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    allFiles = null;
    await refreshTree();
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function renameEntry(path) {
  const target = await dialog({ title: "重命名", input: true, value: path });
  if (!target || target === path) return;
  try {
    const wasOpen = state.models.has(path);
    await api("/api/fs/rename", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path, to: target }),
    });
    if (wasOpen) closeTab(path);
    allFiles = null;
    await refreshTree();
    if (wasOpen) await openFile(target);
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function deleteEntry(path) {
  const confirmed = await dialog({
    title: "删除",
    message: "确定删除 " + path + " 吗？此操作不可撤销。",
  });
  if (!confirmed) return;
  try {
    await api("/api/fs/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path }),
    });
    if (state.models.has(path)) closeTab(path);
    allFiles = null;
    await refreshTree();
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("newFile").addEventListener("click", () => createFileAt(""));
$("newFolder").addEventListener("click", () => createFolderAt(""));
$("refreshTree").addEventListener("click", () => { allFiles = null; refreshTree(); });

document.addEventListener("contextmenu", (event) => {
  const node = event.target.closest(".node");
  if (!node) return;
  event.preventDefault();
  ctxTarget = node;
  const menu = $("ctxMenu");
  menu.classList.remove("hidden");
  menu.style.left = Math.min(event.clientX, window.innerWidth - 160) + "px";
  menu.style.top = Math.min(event.clientY, window.innerHeight - 120) + "px";
});

$("ctxMenu").addEventListener("click", async (event) => {
  const button = event.target.closest("button");
  if (!button || !ctxTarget) return;
  const path = ctxTarget.dataset.path;
  const isDir = ctxTarget.dataset.type === "dir";
  $("ctxMenu").classList.add("hidden");
  if (button.dataset.act === "rename") await renameEntry(path);
  else if (button.dataset.act === "delete") await deleteEntry(path);
  else if (button.dataset.act === "newfile") {
    const base = isDir ? path : path.includes("/") ? path.slice(0, path.lastIndexOf("/")) : "";
    await createFileAt(base);
  }
});
document.addEventListener("click", () => $("ctxMenu").classList.add("hidden"));
