/* ---------- Git 面板 ---------- */

/* ---------- Git ---------- */
const GIT_MARKS = {
  untracked: "U", added: "A", deleted: "D", renamed: "R", modified: "M",
};

async function loadGit() {
  const box = $("gitChanges");
  box.innerHTML = '<div class="results-empty">读取中…</div>';
  try {
    const data = await api("/api/git");
    state.git = data;
    renderGit(data);
  } catch (error) {
    showEmpty(box, error.message);
  }
}

function renderGit(data) {
  const box = $("gitChanges");
  const info = $("gitInfo");
  box.innerHTML = "";
  $("gitInit").classList.toggle("hidden", !!data.repo);

  if (!data.repo) {
    info.innerHTML = '<span>当前目录还不是 Git 仓库</span>';
    box.innerHTML = '<div class="results-empty">点上方「初始化仓库」开始</div>';
    return;
  }

  info.innerHTML = "";
  const branch = document.createElement("span");
  branch.className = "branch";
  branch.textContent = data.branch || "（无分支）";
  const count = document.createElement("span");
  count.textContent = data.changes.length ? data.changes.length + " 处变更" : "工作区干净";
  info.append(branch, count);

  if (!data.changes.length) {
    box.innerHTML = '<div class="results-empty">没有待提交的改动</div>';
    return;
  }

  for (const change of data.changes) {
    const row = document.createElement("div");
    row.className = "git-row" + (change.staged ? " staged" : "");
    const code = document.createElement("span");
    code.className = "git-code";
    code.textContent = GIT_MARKS[change.kind] || "·";
    const path = document.createElement("span");
    path.className = "git-path";
    path.textContent = change.path;
    path.title = change.path;
    const act = document.createElement("button");
    act.className = "mini-btn git-act";
    act.textContent = change.staged ? "取消" : "暂存";
    act.addEventListener("click", (event) => {
      event.stopPropagation();
      toggleStage(change);
    });
    row.append(code, path, act);
    row.addEventListener("click", () => showGitDiff(change));
    box.appendChild(row);
  }
}

async function showGitDiff(change) {
  if (state.pending) {
    logMessage("status", "先处理当前待确认的改动，再看 Git 对比");
    return;
  }
  try {
    const data = await api(
      "/api/git/diff?path=" + encodeURIComponent(change.path) + "&staged=" + (change.staged ? "1" : "0")
    );
    showDiff({ path: data.path, old: data.old || "", new: data.new || "" }, true);
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function toggleStage(change) {
  const path = change.staged ? "/api/git/unstage" : "/api/git/stage";
  try {
    await api(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: [change.path] }),
    });
    await loadGit();
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("gitRefresh").addEventListener("click", loadGit);
$("gitStageAll").addEventListener("click", async () => {
  const changes = (state.git && state.git.changes) || [];
  const paths = changes.filter((item) => !item.staged).map((item) => item.path);
  if (!paths.length) return;
  try {
    await api("/api/git/stage", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths }),
    });
    await loadGit();
  } catch (error) {
    logMessage("error", error.message);
  }
});
$("gitInit").addEventListener("click", async () => {
  try {
    await api("/api/git/init", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    await loadGit();
    logMessage("status", "已初始化 Git 仓库");
  } catch (error) {
    logMessage("error", error.message);
  }
});
$("gitMessageBtn").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "生成中…";
  try {
    const data = await api("/api/git/message", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: currentModelConfig() }),
    });
    $("gitMessage").value = data.message || "";
  } catch (error) {
    logMessage("error", "生成提交信息失败：" + error.message);
  } finally {
    button.disabled = false;
    button.textContent = "AI 写";
  }
});
$("gitCommitBtn").addEventListener("click", async () => {
  const message = $("gitMessage").value.trim();
  if (!message) {
    logMessage("error", "先填提交信息");
    return;
  }
  try {
    const data = await api("/api/git/commit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    $("gitMessage").value = "";
    await loadGit();
    logMessage("status", "提交完成：" + (data.output || "").split("\n")[0]);
  } catch (error) {
    logMessage("error", error.message);
  }
});
