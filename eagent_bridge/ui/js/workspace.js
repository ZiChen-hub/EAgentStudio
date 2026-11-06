/* ---------- 工作目录切换、启动与全局按钮 ---------- */

/* ---------- 初始化 / 切换工作目录 ---------- */
async function switchWorkspace() {
  if ($("sendStop").classList.contains("running")) {
    logMessage("error", "任务运行中，不能切换工作目录，请先停止任务");
    return;
  }
  let target = "";
  const hasPicker = !!(window.pywebview && window.pywebview.api && window.pywebview.api.choose_folder);
  if (hasPicker) {
    try {
      target = await window.pywebview.api.choose_folder();
    } catch (error) {
      target = "";
    }
  } else {
    target = await dialog({
      title: "切换工作目录",
      message: "输入绝对路径",
      input: true,
      value: $("workspace").value,
    });
  }
  if (!target) return;

  try {
    const data = await api("/api/workspace", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: target }),
    });
    $("workspace").value = data.path;
    settings.workspace = data.path;
    persistSettings();
    $("termOut").textContent = "";
    updateTermPrompt(data.path);
    clearReferences();
    state.skillCache = null;
    state.ruleCache = null;
    state.mcpServers = null;
    allFiles = null;
    for (const path of [...state.models.keys()]) closeTab(path);
    await refreshTree();
    persistChatStore(true);
    switchChatWorkspace(data.path);
    logMessage("status", "已切换到工作目录：" + data.path);
  } catch (error) {
    logMessage("error", error.message);
  }
}

function boot() {
  if (!window.monaco) {
    const note = document.createElement("div");
    note.className = "loader";
    note.textContent = "Monaco 加载失败，请检查网络后刷新";
    $("editor").appendChild(note);
    return;
  }
  createEditor();
  applySettingsToUI();
  updateWelcome();
  maybeShowOnboarding();
  (async () => {
    try {
      let data = await api("/api/workspace");
      if (settings.workspace && settings.workspace !== data.path) {
        try {
          data = await api("/api/workspace", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ path: settings.workspace }),
          });
        } catch (error) {
          logMessage("error", "上次的工作目录不可用：" + error.message);
        }
      }
      $("workspace").value = data.path;
      settings.workspace = data.path;
      persistSettings();
      updateTermPrompt(data.path);
      await refreshTree();
      switchChatWorkspace(data.path);
      setStatus("就绪");
      logMessage("status", "工作目录已加载：" + data.path + "　描述任务即可开始");
      // 刷新页面后如果任务还在跑，直接接上事件流和队列卡片
      checkQueue(true);
    } catch (error) {
      logMessage("error", error.message);
    }
  })();
}

$("sendStop").addEventListener("click", () => {
  if ($("sendStop").classList.contains("running")) stopTask();
  else sendTask();
});

async function stopTask() {
  try {
    await api("/api/agent/stop", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
  } catch (error) {
    logMessage("error", "停止失败：" + error.message);
    return;
  }
  finishRun("已停止");
  queueWatch(600);
}

$("task").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
    event.preventDefault();
    sendTask();
  }
});
$("accept").addEventListener("click", () => respondApproval(true));
$("reject").addEventListener("click", () => respondApproval(false));
$("closeDiff").addEventListener("click", hideDiff);
$("workspace").addEventListener("click", switchWorkspace);
$("newSession").addEventListener("click", async () => {
  if ($("sendStop").classList.contains("running")) {
    logMessage("error", "请先停止当前任务");
    return;
  }
  try {
    await api("/api/agent/new_session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: currentModelConfig() }),
    });
    startNewChat();
    $("task").value = "";
    clearReferences();
    logMessage("status", "已创建新会话");
  } catch (error) {
    logMessage("error", error.message);
  }
});

$("chatHistoryBtn").addEventListener("click", (event) => {
  event.stopPropagation();
  const pop = $("chatHistoryPop");
  if (pop.classList.contains("hidden")) {
    renderChatHistory();
    pop.classList.remove("hidden");
  } else {
    pop.classList.add("hidden");
  }
});
$("chatHistoryPop").addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", () => $("chatHistoryPop").classList.add("hidden"));
window.addEventListener("beforeunload", () => persistChatStore(true));

window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (window.monaco) monaco.editor.setTheme(editorTheme());
});
