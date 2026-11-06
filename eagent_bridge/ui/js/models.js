/* ---------- 模型配置与选择菜单 ---------- */

/* ---------- 模型 ---------- */
function activeModel() {
  const list = settings.models || [];
  return list.find((model) => model.id === settings.activeModelId) || list[0] || BUILTIN_MODELS[0];
}

function modelConfigOf(model) {
  if (model.type === "web") {
    return { provider: "webai", site: model.site || "deepseek" };
  }
  if (model.type === "ollama") {
    return {
      provider: "ollama",
      baseUrl: model.baseUrl || "http://127.0.0.1:11434/v1",
      model: model.modelName || "llama3",
    };
  }
  return {
    provider: "openai",
    baseUrl: model.baseUrl || "",
    apiKey: model.apiKey || "",
    model: model.modelName || "",
  };
}

function currentModelConfig() {
  return modelConfigOf(activeModel());
}

function fallbackModelConfig() {
  const id = settings.fallbackModelId;
  if (!id) return null;
  const model = (settings.models || []).find((item) => item.id === id);
  return model ? modelConfigOf(model) : null;
}

function updateModelChip() {
  $("modelChipLabel").textContent = activeModel().name || "模型";
}

function updateModeChip() {
  const labels = { code: "编程模式", computer: "电脑模式", hybrid: "混合模式" };
  $("modeChipLabel").textContent = labels[settings.agentMode] || "编程模式";
}

function modelDescription(model) {
  if (model.type === "web") return "网页 AI · " + (model.site || "deepseek");
  if (model.type === "ollama") return "Ollama · " + (model.modelName || "llama3");
  return "OpenAI 兼容 · " + (model.modelName || "未命名模型");
}

function renderModelList() {
  const box = $("modelList");
  box.innerHTML = "";
  for (const model of settings.models) {
    const row = document.createElement("div");
    row.className = "manage-item";
    const icon = document.createElement("div");
    icon.className = "manage-icon";
    icon.textContent = (model.name || "M").slice(0, 1).toUpperCase();
    const main = document.createElement("div");
    main.className = "manage-main";
    const name = document.createElement("div");
    name.className = "manage-name";
    name.textContent = model.name + (model.id === settings.activeModelId ? "  ✓" : "");
    const desc = document.createElement("div");
    desc.className = "manage-desc";
    desc.textContent = modelDescription(model);
    main.append(name, desc);
    const actions = document.createElement("div");
    actions.className = "manage-actions";
    const edit = document.createElement("button");
    edit.className = "mini-btn";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openModelEditor(model.id));
    const remove = document.createElement("button");
    remove.className = "mini-btn";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteModel(model.id));
    actions.append(edit, remove);
    row.append(icon, main, actions);
    box.appendChild(row);
  }
}

function renderActiveModels() {
  const select = $("activeModelSelect");
  select.innerHTML = "";
  for (const model of settings.models) {
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.name;
    select.appendChild(option);
  }
  select.value = activeModel().id;
  renderModelList();
}

let editingModelId = null;

const MODEL_PRESETS = {
  deepseek: {
    name: "DeepSeek API", type: "openai",
    baseUrl: "https://api.deepseek.com/v1", modelName: "deepseek-chat",
  },
  openrouter: {
    name: "OpenRouter", type: "openai",
    baseUrl: "https://openrouter.ai/api/v1", modelName: "deepseek/deepseek-chat",
  },
  gemini: {
    name: "Gemini", type: "openai",
    baseUrl: "https://generativelanguage.googleapis.com/v1beta/openai/",
    modelName: "gemini-2.0-flash",
  },
  groq: {
    name: "Groq", type: "openai",
    baseUrl: "https://api.groq.com/openai/v1", modelName: "llama-3.3-70b-versatile",
  },
  ollama: {
    name: "本地 Ollama", type: "ollama",
    baseUrl: "http://127.0.0.1:11434/v1", modelName: "llama3",
  },
};

function applyModelPreset() {
  const preset = MODEL_PRESETS[$("modelPreset").value];
  if (!preset) return;
  $("modelFormName").value = preset.name;
  $("modelFormType").value = preset.type;
  $("modelFormBase").value = preset.baseUrl;
  $("modelFormModel").value = preset.modelName;
  updateModelFormVisibility();
}

function updateModelFormVisibility() {
  const type = $("modelFormType").value;
  document.querySelectorAll("#modelEditor .row[data-type]").forEach((row) => {
    row.classList.toggle("hidden", row.dataset.type !== (type === "web" ? "web" : "api"));
  });
  if (type === "ollama") {
    if (!$("modelFormBase").value) $("modelFormBase").value = "http://127.0.0.1:11434/v1";
    if (!$("modelFormModel").value) $("modelFormModel").value = "llama3";
  }
}

function openModelEditor(id) {
  editingModelId = id || null;
  const model = id ? settings.models.find((item) => item.id === id) : null;
  $("modelEditorTitle").textContent = model ? "编辑模型" : "添加模型";
  $("modelPreset").value = "";
  $("modelFormName").value = model ? model.name : "";
  $("modelFormType").value = model ? model.type : "web";
  $("modelFormSite").value = model && model.site ? model.site : "deepseek";
  $("modelFormBase").value = model ? model.baseUrl || "" : "";
  $("modelFormKey").value = model ? model.apiKey || "" : "";
  $("modelFormModel").value = model ? model.modelName || "" : "";
  updateModelFormVisibility();
  $("modelEditor").classList.remove("hidden");
}

function saveModelForm() {
  const name = $("modelFormName").value.trim();
  if (!name) { logMessage("error", "模型名称不能为空"); return; }
  const type = $("modelFormType").value;
  const payload = { id: editingModelId || "custom-" + Date.now(), name, type };
  if (type === "web") {
    payload.site = $("modelFormSite").value;
  } else {
    payload.baseUrl = $("modelFormBase").value.trim();
    payload.apiKey = $("modelFormKey").value.trim();
    payload.modelName = $("modelFormModel").value.trim();
  }
  if (editingModelId) {
    settings.models = settings.models.map((item) => (item.id === editingModelId ? { ...item, ...payload } : item));
  } else {
    settings.models.push(payload);
    settings.activeModelId = payload.id;
  }
  persistSettings();
  renderActiveModels();
  updateModelChip();
  $("modelEditor").classList.add("hidden");
}

function deleteModel(id) {
  if (settings.models.length <= 1) { logMessage("error", "至少保留一个模型"); return; }
  settings.models = settings.models.filter((item) => item.id !== id);
  if (settings.activeModelId === id) settings.activeModelId = settings.models[0].id;
  persistSettings();
  renderActiveModels();
  updateModelChip();
}

$("addModel").addEventListener("click", () => openModelEditor(null));
$("modelPreset").addEventListener("change", applyModelPreset);
$("checkModel").addEventListener("click", async () => {
  const button = $("checkModel");
  if (button.disabled) return;
  const label = button.textContent;
  button.disabled = true;
  button.textContent = "检测中…";
  try {
    const data = await api("/api/model/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: currentModelConfig() }),
    });
    dialog({ title: "模型检测", message: data.message || (data.ok ? "可用" : "不可用") });
  } catch (error) {
    logMessage("error", "检测失败：" + error.message);
  } finally {
    button.disabled = false;
    button.textContent = label;
  }
});
$("modelFormType").addEventListener("change", updateModelFormVisibility);
$("modelFormCancel").addEventListener("click", () => $("modelEditor").classList.add("hidden"));
$("modelFormSave").addEventListener("click", saveModelForm);

/* ---------- 模型选择菜单 ---------- */
function renderModelMenu() {
  const menu = $("modelMenu");
  menu.innerHTML = "";
  for (const model of settings.models) {
    const item = document.createElement("div");
    item.className = "mm-item" + (model.id === settings.activeModelId ? " active" : "");
    const name = document.createElement("span");
    name.textContent = model.name;
    const tag = document.createElement("span");
    tag.className = "tag";
    tag.textContent = model.type === "web" ? "网页" : model.type === "ollama" ? "Ollama" : "API";
    item.append(name, tag);
    item.addEventListener("click", () => {
      settings.activeModelId = model.id;
      persistSettings();
      updateModelChip();
      menu.classList.add("hidden");
    });
    menu.appendChild(item);
  }
}

$("modelChip").addEventListener("click", (event) => {
  event.stopPropagation();
  renderModelMenu();
  $("modelMenu").classList.toggle("hidden");
});
$("modelMenu").addEventListener("click", (event) => event.stopPropagation());
document.addEventListener("click", () => $("modelMenu").classList.add("hidden"));

$("modeChip").addEventListener("click", () => {
  if ($("sendStop").classList.contains("running")) {
    logMessage("error", "任务运行中，暂时不能切换模式");
    return;
  }
  const next = { code: "computer", computer: "hybrid", hybrid: "code" };
  settings.agentMode = next[settings.agentMode] || "code";
  persistSettings();
  updateModeChip();
  const messages = {
    code: "已切换到编程模式：只加载文件与命令工具",
    computer: "已切换到电脑模式：加载桌面工具与 Windows-MCP",
    hybrid: "已切换到混合模式：文件、命令、桌面工具和 Windows-MCP 全部可用",
  };
  logMessage("status", messages[settings.agentMode]);
});
