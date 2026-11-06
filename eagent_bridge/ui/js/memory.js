/* ---------- 记忆建议与图片输入 ---------- */

/* ---------- 记忆建议 ---------- */
async function suggestMemory(runId) {
  if (!runId) return;
  try {
    const data = await api("/api/memory/suggest", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ run: runId, model: currentModelConfig() }),
    });
    if (data.items && data.items.length) renderMemoryCard(data.items);
  } catch (error) {
    /* 提炼失败不打扰用户 */
  }
}

function renderMemoryCard(items) {
  const card = document.createElement("div");
  card.className = "msg memory";
  const title = document.createElement("div");
  title.className = "memory-title";
  title.textContent = "这次任务可以记进长期记忆：";
  const list = document.createElement("ul");
  list.className = "memory-list";
  for (const item of items) {
    const li = document.createElement("li");
    li.textContent = item;
    list.appendChild(li);
  }
  const row = document.createElement("div");
  row.className = "memory-actions";
  const save = document.createElement("button");
  save.className = "primary";
  save.textContent = "记住";
  const skip = document.createElement("button");
  skip.textContent = "忽略";
  save.addEventListener("click", async () => {
    save.disabled = true;
    try {
      await api("/api/memory/append", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ items, scope: "project" }),
      });
      card.remove();
      setStatus("已记住 " + items.length + " 条记忆", "idle");
    } catch (error) {
      save.disabled = false;
      logMessage("error", "保存记忆失败：" + error.message);
    }
  });
  skip.addEventListener("click", () => card.remove());
  row.append(save, skip);
  card.append(title, list, row);
  messages.appendChild(card);
  messages.scrollTop = messages.scrollHeight;
}

async function sendTask() {
  const task = $("task").value.trim();
  if (!task) return;
  setSending(true);
  $("task").value = "";
  logMessage("user", task);
  setStatus("运行中", "running");

  const references = {
    skills: [...state.refs.skills],
    files: [...state.refs.files],
    mcp: [...state.refs.mcp],
    symbols: [...state.refs.symbols],
    images: [...state.pendingImages],
  };
  // 手输的 &符号 也当成引用
  const typedSymbols = task.match(/(?:^|[^\w&])&([A-Za-z_$][\w$.]*)/g) || [];
  for (const raw of typedSymbols) {
    const name = raw.replace(/^[^\w&]?&/, "");
    if (name && !references.symbols.includes(name)) references.symbols.push(name);
  }

  try {
    const data = await api("/api/agent/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        task,
        max_steps: Number($("maxSteps").value),
        auto_allow: $("autoAllow").checked,
        plan_first: !!settings.planFirst,
        whitelist: settings.whitelist,
        model: currentModelConfig(),
        fallback: fallbackModelConfig(),
        skills: settings.enabledSkills,
        rules: settings.enabledRules,
        references,
        mode: settings.agentMode,
      }),
    });
    if (data.queued) {
      logMessage("assistant", "已加入队列（第 " + data.position + " 位）：" + task);
      queueWatch(300);
    } else {
      connectEvents(data.run);
    }
    clearReferences();
  } catch (error) {
    logMessage("error", error.message);
    finishRun("启动失败", "error");
  }
}

/* ---------- 图片 ---------- */
function renderImageChips() {
  const box = $("imageChips");
  box.innerHTML = "";
  box.classList.toggle("hidden", !state.pendingImages.length);
  state.pendingImages.forEach((path, index) => {
    const chip = document.createElement("div");
    chip.className = "image-chip";
    const img = document.createElement("img");
    img.src = "/api/image?path=" + encodeURIComponent(path) + "&token=" + encodeURIComponent(apiToken());
    img.alt = path;
    img.title = path;
    const close = document.createElement("button");
    close.textContent = "×";
    close.title = "移除";
    close.addEventListener("click", () => {
      state.pendingImages.splice(index, 1);
      renderImageChips();
    });
    chip.append(img, close);
    box.appendChild(chip);
  });
}

function addImagePath(path) {
  if (!path) return;
  if (!state.pendingImages.includes(path)) state.pendingImages.push(path);
  renderImageChips();
}

async function uploadImageFile(file) {
  if (!file || !file.type || !file.type.startsWith("image/")) return;
  const dataUrl = await new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
  const base64 = dataUrl.split(",", 2)[1] || "";
  if (!base64) return;
  try {
    const data = await api("/api/upload", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: file.name || "image.png", data: base64 }),
    });
    addImagePath(data.path);
  } catch (error) {
    logMessage("error", "上传图片失败：" + error.message);
  }
}

$("attachImage").addEventListener("click", () => $("imageFile").click());
$("imageFile").addEventListener("change", async (event) => {
  const files = [...(event.target.files || [])];
  event.target.value = "";
  for (const file of files) await uploadImageFile(file);
});
$("shotImage").addEventListener("click", async (event) => {
  const button = event.currentTarget;
  if (button.disabled) return;
  button.disabled = true;
  button.textContent = "截屏中…";
  try {
    const data = await api("/api/screenshot", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    addImagePath(data.path);
  } catch (error) {
    logMessage("error", "截屏失败：" + error.message);
  } finally {
    button.disabled = false;
    button.textContent = "截屏";
  }
});
$("task").addEventListener("paste", (event) => {
  const files = [...((event.clipboardData && event.clipboardData.files) || [])];
  const images = files.filter((file) => file.type && file.type.startsWith("image/"));
  if (!images.length) return;
  event.preventDefault();
  images.forEach((file) => uploadImageFile(file));
});
