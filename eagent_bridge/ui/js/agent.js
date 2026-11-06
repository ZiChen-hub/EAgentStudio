/* ---------- diff 确认、Agent 事件流、任务队列与命令输出 ---------- */

/* ---------- diff 确认 ---------- */
function showDiff(preview, readonly) {
  if (!state.diffEditor) {
    state.diffEditor = monaco.editor.createDiffEditor($("diff"), {
      theme: editorTheme(),
      automaticLayout: true,
      readOnly: true,
      renderSideBySide: true,
      fontSize: 13,
      minimap: { enabled: false },
    });
  }
  for (const model of state.diffModels) model.dispose();
  state.diffModels = [];

  const language = languageOf(preview.path);
  const original = monaco.editor.createModel(preview.old || "", language, monaco.Uri.parse("diff://orig/" + Date.now()));
  const modified = monaco.editor.createModel(preview.new || "", language, monaco.Uri.parse("diff://mod/" + Date.now()));
  state.diffModels = [original, modified];
  state.diffEditor.setModel({ original, modified });
  $("diff").style.display = "block";
  $("editor").style.display = "none";
  $("approvePath").textContent = preview.path;
  $("accept").classList.toggle("hidden", !!readonly);
  $("reject").classList.toggle("hidden", !!readonly);
  $("closeDiff").classList.toggle("hidden", !readonly);
  $("approveBar").classList.add("show");
  state.pending = readonly ? null : preview;
}

function hideDiff() {
  $("diff").style.display = "none";
  $("editor").style.display = "block";
  $("approveBar").classList.remove("show");
  $("accept").classList.remove("hidden");
  $("reject").classList.remove("hidden");
  $("closeDiff").classList.add("hidden");
  state.pending = null;
}

async function respondApproval(allow) {
  if (!state.pending) return;
  const { id, path } = state.pending;
  try {
    await api("/api/approval", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: state.runId, id, allow }),
    });
    hideDiff();
    if (allow && path) reloadIfOpen(path);
  } catch (error) {
    logMessage("error", "确认失败：" + error.message);
  }
}

/* ---------- Agent ---------- */
function connectEvents(runId, since = 0) {
  if (state.source) state.source.close();
  // 同一个任务重连时接着上次的事件序号，避免把历史消息再刷一遍
  const resumed = runId === state.runId;
  state.runId = runId;
  state.lastEventId = resumed ? Math.max(state.lastEventId || 0, since) : since;
  state.commandBlock = null;
  const source = new EventSource(
    "/api/agent/events?run=" + runId +
    "&since=" + (state.lastEventId || 0) +
    "&token=" + encodeURIComponent(apiToken())
  );
  state.source = source;
  let sawFinish = false;
  source.onmessage = (event) => {
    if (event.lastEventId) state.lastEventId = Number(event.lastEventId) || state.lastEventId;
    let data;
    try {
      data = JSON.parse(event.data);
    } catch (error) {
      return;
    }
    if (data.type === "run_started") {
      setStatus("运行中", "running");
      return;
    }
    if (data.type === "agent") {
      if (data.kind === "finish") sawFinish = true;
      if (data.kind === "status") {
        // 进度只显示在顶栏，不写进聊天记录
        setStatus(data.text, "running");
        return;
      }
      if (data.kind === "command_start") { startCommandBlock(data.text); return; }
      if (data.kind === "command_output") { appendCommandLine(data.text); return; }
      if (data.kind === "command_end") { endCommandBlock(data.text); return; }
      logMessage(data.kind, data.text);
      return;
    }
    if (data.type === "approval") {
      const preview = data.preview || {};
      if (preview.kind === "write") {
        showDiff({ ...preview, id: data.id });
      } else if (preview.kind === "plan") {
        logMessage("plan", preview.plan || "");
        state.pending = { id: data.id, path: null };
        $("diff").style.display = "none";
        $("editor").style.display = "block";
        $("approvePath").textContent = "执行这份计划";
        $("accept").classList.remove("hidden");
        $("reject").classList.remove("hidden");
        $("closeDiff").classList.add("hidden");
        $("approveBar").classList.add("show");
      } else {
        state.pending = { id: data.id, path: null };
        const detail = preview.kind === "command"
          ? "命令：" + preview.command + "\n目录：" + preview.cwd
          : (preview.detail || data.tool);
        respondApproval(window.confirm("Agent 请求执行：" + data.tool + "\n\n" + detail));
      }
      return;
    }
    if (data.type === "approval_result") {
      logMessage("status", data.allow ? "已接受操作" : "已拒绝操作");
      return;
    }
    if (data.type === "run_done") {
      if (!sawFinish) logMessage("finish", data.summary || "任务结束");
      const finishedRun = state.runId;
      finishRun("任务结束", "idle");
      setTimeout(() => afterRun(finishedRun), 800);
      queueWatch(600);
      return;
    }
    if (data.type === "run_error") {
      logMessage("error", data.message);
      finishRun("运行失败", "error");
      queueWatch(600);
      return;
    }
  };
  source.onerror = () => {
    // 关掉 EventSource 的自动重连，改由队列接口决定是续传还是收尾
    source.close();
    if (state.source !== source) return;
    state.source = null;
    checkQueue().then(() => {
      if (state.source) return;
      finishRun("连接中断", "error");
      queueWatch(600);
    });
  };
}

function setSending(running) {
  const button = $("sendStop");
  button.classList.toggle("running", running);
  button.title = running ? "停止" : "发送";
}

function finishRun(status, kind = "idle") {
  if (state.commandBlock) endCommandBlock("");
  setSending(false);
  $("task").placeholder = status + "，可继续下达任务";
  setStatus(status, kind);
  if (state.source) { state.source.close(); state.source = null; }
}

/* ---------- 任务队列 ---------- */
function queueWatch(delay) {
  if (state.queueTimer) clearTimeout(state.queueTimer);
  state.queueTimer = setTimeout(async () => {
    state.queueTimer = null;
    await checkQueue();
  }, delay || 800);
}

async function checkQueue() {
  let data;
  try {
    data = await api("/api/agent/queue", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
  } catch (error) {
    return;
  }
  const items = data.items || [];
  renderQueueCard(items);
  if (!state.source && data.active) {
    const sameRun = data.active.id === state.runId;
    if (!sameRun) logMessage("user", data.active.task || "（排队任务）");
    connectEvents(data.active.id, sameRun ? state.lastEventId : 0);
    setSending(true);
    setStatus("运行中", "running");
  }
  if (items.length) queueWatch(1500);
}

function renderQueueCard(items) {
  if (!items.length) {
    if (state.queueCard) {
      state.queueCard.remove();
      state.queueCard = null;
    }
    return;
  }
  if (!state.queueCard) {
    const card = document.createElement("div");
    card.className = "msg queue";
    messages.appendChild(card);
    state.queueCard = card;
  }
  const card = state.queueCard;
  card.innerHTML = "";
  const title = document.createElement("div");
  title.className = "queue-title";
  title.textContent = "排队中（" + items.length + "）";
  card.appendChild(title);
  for (const item of items) {
    const row = document.createElement("div");
    row.className = "queue-row";
    const text = document.createElement("span");
    text.className = "queue-task";
    text.textContent = item.task;
    text.title = item.task;
    const remove = document.createElement("button");
    remove.className = "mini-btn";
    remove.textContent = "取消";
    remove.addEventListener("click", async () => {
      remove.disabled = true;
      try {
        await api("/api/agent/queue/remove", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ id: item.id }),
        });
      } catch (error) {
        logMessage("error", error.message);
      }
      await checkQueue();
    });
    row.append(text, remove);
    card.appendChild(row);
  }
}

/* ---------- 命令输出 ---------- */
function startCommandBlock(command) {
  const box = document.createElement("div");
  box.className = "msg command";
  const head = document.createElement("div");
  head.className = "command-head";
  head.textContent = "$ " + command;
  const pre = document.createElement("pre");
  pre.className = "command-out";
  const foot = document.createElement("div");
  foot.className = "command-foot";
  foot.textContent = "运行中…";
  box.append(head, pre, foot);
  messages.appendChild(box);
  while (messages.children.length > 500) {
    messages.removeChild(messages.firstChild);
  }
  messages.scrollTop = messages.scrollHeight;
  state.commandBlock = { box, pre, foot, lines: 0 };
}

function appendCommandLine(text) {
  if (!state.commandBlock) startCommandBlock("（命令输出）");
  const block = state.commandBlock;
  if (block.lines < 600) {
    block.pre.textContent += (block.lines ? "\n" : "") + text;
    block.lines += 1;
  } else if (block.lines === 600) {
    block.pre.textContent += "\n…（输出过多，界面不再追加）";
    block.lines += 1;
  }
  const nearBottom = messages.scrollHeight - messages.scrollTop - messages.clientHeight < 80;
  if (nearBottom) messages.scrollTop = messages.scrollHeight;
}

function endCommandBlock(info) {
  const block = state.commandBlock;
  if (!block) return;
  const text = info || "命令结束";
  block.foot.textContent = text;
  block.foot.classList.toggle("ok", /exit_code=0/.test(text));
  block.foot.classList.toggle("bad", !!info && !/exit_code=0/.test(text));
  state.commandBlock = null;
}

/* ---------- 任务结束后的两个角色：审查 + 记忆 ---------- */
async function afterRun(runId) {
  if (!runId || runId !== state.runId || state.source) return;
  if (settings.reviewAfterRun !== false) await reviewRun(runId);
  await suggestMemory(runId);
}

async function reviewRun(runId) {
  try {
    const data = await api("/api/review", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: runId, model: currentModelConfig() }),
    });
    if (data.items && data.items.length) renderReviewCard(data.items);
  } catch (error) {
    /* 自审失败不打扰用户 */
  }
}

function renderReviewCard(items) {
  const card = document.createElement("div");
  card.className = "msg review";
  const title = document.createElement("div");
  title.className = "review-title";
  title.textContent = "自审发现 " + items.length + " 处可能的问题：";
  const list = document.createElement("ul");
  list.className = "memory-list";
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  const row = document.createElement("div");
  row.className = "memory-actions";
  const fix = document.createElement("button");
  fix.className = "primary";
  fix.textContent = "让 Agent 修复";
  const skip = document.createElement("button");
  skip.textContent = "忽略";
  fix.addEventListener("click", () => {
    card.remove();
    $("task").value = "请修复以下自审发现的问题：\n" + items.map((item) => "- " + item).join("\n");
    sendTask();
  });
  skip.addEventListener("click", () => card.remove());
  row.append(fix, skip);
  card.append(title, list, row);
  messages.appendChild(card);
  messages.scrollTop = messages.scrollHeight;
}
