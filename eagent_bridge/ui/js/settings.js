/* ---------- 设置窗口：记忆、白名单、技能、规则、MCP ---------- */

/* ---------- 设置窗口 ---------- */
const PANE_TITLES = { appearance: "外观", models: "模型", skills: "技能", rules: "规则", memory: "记忆", mcp: "MCP", whitelist: "白名单", about: "关于" };

function switchSettingsPane(name) {
  document.querySelectorAll(".sn-item").forEach((button) => {
    button.classList.toggle("active", button.dataset.pane === name);
  });
  document.querySelectorAll(".settings-pane").forEach((pane) => {
    pane.classList.toggle("hidden", pane.dataset.pane !== name);
  });
  $("settingsTitle").textContent = PANE_TITLES[name] || "设置";
  if (name === "rules") loadRules();
  if (name === "memory") loadMemory();
}

/* ---------- 记忆 ---------- */
async function loadMemory() {
  try {
    const data = await api("/api/memory");
    $("memoryProject").value = data.project || "";
    $("memoryGlobal").value = data.global || "";
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("saveMemory").addEventListener("click", async () => {
  try {
    await api("/api/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "project", content: $("memoryProject").value }),
    });
    await api("/api/memory", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scope: "global", content: $("memoryGlobal").value }),
    });
    logMessage("status", "记忆已保存");
  } catch (error) {
    logMessage("error", error.message);
  }
});

document.querySelectorAll(".sn-item").forEach((button) => {
  button.addEventListener("click", () => switchSettingsPane(button.dataset.pane));
});

function fillSettingsForm() {
  $("maxSteps").value = String(settings.maxSteps);
  $("autoAllow").checked = !!settings.autoAllow;
  $("planFirst").checked = !!settings.planFirst;
  $("reviewAfterRun").checked = settings.reviewAfterRun !== false;
  $("theme").value = settings.theme;
  $("zoomLabel").textContent = Math.round((settings.zoom || 1) * 100) + "%";
  renderActiveModels();
  renderFallbackOptions();
  renderWhitelist();
}

function renderFallbackOptions() {
  const select = $("fallbackModel");
  select.innerHTML = "";
  const none = document.createElement("option");
  none.value = "";
  none.textContent = "不启用";
  select.appendChild(none);
  const activeId = $("activeModelSelect").value || settings.activeModelId;
  for (const model of settings.models || []) {
    if (model.id === activeId) continue;
    const option = document.createElement("option");
    option.value = model.id;
    option.textContent = model.name + "（" + modelDescription(model) + "）";
    select.appendChild(option);
  }
  select.value = settings.fallbackModelId || "";
}

$("activeModelSelect").addEventListener("change", renderFallbackOptions);

$("openSettings").addEventListener("click", () => {
  fillSettingsForm();
  loadSkills();
  loadRules();
  loadMcp();
  switchSettingsPane("appearance");
  $("settingsModal").classList.remove("hidden");
});
$("cancelSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
$("closeSettings").addEventListener("click", () => $("settingsModal").classList.add("hidden"));
$("saveSettings").addEventListener("click", () => {
  settings.maxSteps = $("maxSteps").value;
  settings.autoAllow = $("autoAllow").checked;
  settings.planFirst = $("planFirst").checked;
  settings.reviewAfterRun = $("reviewAfterRun").checked;
  settings.theme = $("theme").value;
  settings.activeModelId = $("activeModelSelect").value || settings.activeModelId;
  settings.fallbackModelId = $("fallbackModel").value || "";
  persistSettings();
  applyTheme(settings.theme);
  updateModelChip();
  $("settingsModal").classList.add("hidden");
  logMessage("status", "设置已保存");
});

/* ---------- 白名单管理 ---------- */
function renderWhitelist() {
  const box = $("whitelistList");
  box.innerHTML = "";
  const items = settings.whitelist || [];
  if (!items.length) {
    box.innerHTML = '<div class="results-empty">还没有白名单命令</div>';
    return;
  }
  for (const value of items) {
    const row = document.createElement("div");
    row.className = "manage-item";
    const icon = document.createElement("div");
    icon.className = "manage-icon";
    icon.textContent = "令";
    const main = document.createElement("div");
    main.className = "manage-main";
    const name = document.createElement("div");
    name.className = "manage-name";
    name.textContent = value;
    const desc = document.createElement("div");
    desc.className = "manage-desc";
    desc.textContent = "以 " + value + " 开头的命令自动放行";
    main.append(name, desc);
    const actions = document.createElement("div");
    actions.className = "manage-actions";
    const edit = document.createElement("button");
    edit.className = "mini-btn";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => editWhitelistEntry(value));
    const remove = document.createElement("button");
    remove.className = "mini-btn";
    remove.textContent = "删除";
    remove.addEventListener("click", () => removeWhitelistEntry(value));
    actions.append(edit, remove);
    row.append(icon, main, actions);
    box.appendChild(row);
  }
}

async function addWhitelistEntry() {
  const value = await dialog({
    title: "添加命令白名单",
    message: "前缀匹配，例如 python、pytest、git status",
    input: true,
  });
  if (!value) return;
  if ((settings.whitelist || []).includes(value)) return;
  settings.whitelist = [...(settings.whitelist || []), value];
  persistSettings();
  renderWhitelist();
}

async function editWhitelistEntry(oldValue) {
  const value = await dialog({
    title: "编辑命令白名单",
    message: "前缀匹配",
    input: true,
    value: oldValue,
  });
  if (!value) return;
  settings.whitelist = (settings.whitelist || []).map((item) => (item === oldValue ? value : item));
  persistSettings();
  renderWhitelist();
}

function removeWhitelistEntry(value) {
  settings.whitelist = (settings.whitelist || []).filter((item) => item !== value);
  persistSettings();
  renderWhitelist();
}

$("addWhitelist").addEventListener("click", addWhitelistEntry);

/* ---------- 技能管理 ---------- */
let editingSkillId = null;

async function loadSkills() {
  const box = $("skillsList");
  box.innerHTML = '<div class="results-empty">加载中…</div>';
  try {
    const data = await api("/api/skills");
    state.skillCache = data.skills || [];
    renderSkills(state.skillCache);
  } catch (error) {
    showEmpty(box, error.message);
  }
}

function renderSkills(items) {
  const box = $("skillsList");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="results-empty">还没有技能，点右上角「创建技能」</div>';
    return;
  }
  for (const skill of items) {
    const enabled = (settings.enabledSkills || []).includes(skill.id);
    const row = document.createElement("div");
    row.className = "manage-item";
    const icon = document.createElement("div");
    icon.className = "manage-icon";
    icon.textContent = "技";
    const main = document.createElement("div");
    main.className = "manage-main";
    const name = document.createElement("div");
    name.className = "manage-name";
    name.textContent = skill.name;
    const desc = document.createElement("div");
    desc.className = "manage-desc";
    desc.textContent = skill.description || skill.id;
    main.append(name, desc);
    const actions = document.createElement("div");
    actions.className = "manage-actions";
    const toggle = document.createElement("button");
    toggle.className = "switch" + (enabled ? " on" : "");
    toggle.title = enabled ? "停用" : "启用";
    toggle.addEventListener("click", () => toggleSkill(skill.id));
    const edit = document.createElement("button");
    edit.className = "mini-btn";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openSkillEditor(skill.id));
    const remove = document.createElement("button");
    remove.className = "mini-btn";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteSkillEntry(skill));
    actions.append(toggle, edit, remove);
    row.append(icon, main, actions);
    box.appendChild(row);
  }
}

function toggleSkill(id) {
  const list = settings.enabledSkills || [];
  settings.enabledSkills = list.includes(id)
    ? list.filter((item) => item !== id)
    : [...list, id];
  persistSettings();
  renderSkills(state.skillCache || []);
}

async function openSkillEditor(id) {
  editingSkillId = id || null;
  const meta = id ? (state.skillCache || []).find((item) => item.id === id) : null;
  $("skillEditorTitle").textContent = id ? "编辑技能" : "创建技能";
  $("skillFormName").value = meta ? meta.name : "";
  $("skillFormDesc").value = meta ? meta.description || "" : "";
  $("skillFormInstructions").value = "";
  if (id) {
    try {
      const data = await api("/api/skills/" + encodeURIComponent(id));
      $("skillFormInstructions").value = data.content || "";
    } catch (error) {
      logMessage("error", error.message);
    }
  }
  $("skillEditor").classList.remove("hidden");
}

async function saveSkillForm() {
  const name = $("skillFormName").value.trim();
  const description = $("skillFormDesc").value.trim();
  const instructions = $("skillFormInstructions").value;
  if (!name || !instructions.trim()) {
    logMessage("error", "技能名称和指令不能为空");
    return;
  }
  try {
    await api("/api/skills", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: editingSkillId, name, description, instructions }),
    });
    $("skillEditor").classList.add("hidden");
    state.skillCache = null;
    await loadSkills();
    logMessage("status", "技能已保存：" + name);
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function deleteSkillEntry(skill) {
  const confirmed = await dialog({
    title: "删除技能",
    message: "确定删除「" + skill.name + "」吗？",
  });
  if (!confirmed) return;
  try {
    await api("/api/skills/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: skill.id }),
    });
    settings.enabledSkills = (settings.enabledSkills || []).filter((item) => item !== skill.id);
    persistSettings();
    state.skillCache = null;
    await loadSkills();
    logMessage("status", "已删除技能：" + skill.name);
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("createSkill").addEventListener("click", () => openSkillEditor(null));
$("skillFormCancel").addEventListener("click", () => $("skillEditor").classList.add("hidden"));
$("skillFormSave").addEventListener("click", saveSkillForm);

/* ---------- 规则管理 ---------- */
let editingRuleId = null;
let editingRuleScope = "project";

function ruleEnabled(key) {
  const list = settings.enabledRules;
  return list === null || list === undefined ? true : list.includes(key);
}

async function loadRules() {
  const box = $("rulesList");
  box.innerHTML = '<div class="results-empty">加载中…</div>';
  try {
    const data = await api("/api/rules");
    state.ruleCache = data.rules || [];
    renderRules(state.ruleCache);
  } catch (error) {
    showEmpty(box, error.message);
  }
}

function renderRules(items) {
  const box = $("rulesList");
  box.innerHTML = "";
  if (!items.length) {
    box.innerHTML = '<div class="results-empty">还没有规则，点右上角「创建规则」</div>';
    return;
  }
  for (const rule of items) {
    const enabled = ruleEnabled(rule.key);
    const row = document.createElement("div");
    row.className = "manage-item";
    const icon = document.createElement("div");
    icon.className = "manage-icon";
    icon.textContent = rule.scope === "global" ? "全" : "项";
    const main = document.createElement("div");
    main.className = "manage-main";
    const name = document.createElement("div");
    name.className = "manage-name";
    name.textContent = rule.name;
    const desc = document.createElement("div");
    desc.className = "manage-desc";
    desc.textContent = rule.description || rule.id;
    main.append(name, desc);
    const actions = document.createElement("div");
    actions.className = "manage-actions";
    const toggle = document.createElement("button");
    toggle.className = "switch" + (enabled ? " on" : "");
    toggle.title = enabled ? "停用" : "启用";
    toggle.addEventListener("click", () => toggleRule(rule.key));
    const edit = document.createElement("button");
    edit.className = "mini-btn";
    edit.textContent = "编辑";
    edit.addEventListener("click", () => openRuleEditor(rule.id, rule.scope));
    const remove = document.createElement("button");
    remove.className = "mini-btn";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteRule(rule));
    actions.append(toggle, edit, remove);
    row.append(icon, main, actions);
    box.appendChild(row);
  }
}

function allRuleKeys() {
  return (state.ruleCache || []).map((item) => item.key);
}

function toggleRule(key) {
  const current = settings.enabledRules === null || settings.enabledRules === undefined
    ? allRuleKeys()
    : [...settings.enabledRules];
  settings.enabledRules = current.includes(key)
    ? current.filter((item) => item !== key)
    : [...current, key];
  persistSettings();
  renderRules(state.ruleCache || []);
}

function openRuleEditor(id, scope) {
  editingRuleId = id || null;
  editingRuleScope = scope || "project";
  const meta = id ? (state.ruleCache || []).find((item) => item.id === id && item.scope === scope) : null;
  $("ruleEditorTitle").textContent = id ? "编辑规则" : "创建规则";
  $("ruleFormName").value = meta ? meta.name : "";
  $("ruleFormDesc").value = meta ? meta.description || "" : "";
  $("ruleFormScope").value = editingRuleScope;
  $("ruleFormScope").disabled = !!id;
  $("ruleFormContent").value = "";
  if (id) {
    api("/api/rules/" + encodeURIComponent(id) + "?scope=" + encodeURIComponent(scope))
      .then((data) => { $("ruleFormContent").value = data.content || ""; })
      .catch((error) => logMessage("error", error.message));
  }
  $("ruleEditor").classList.remove("hidden");
}

async function saveRuleForm() {
  const name = $("ruleFormName").value.trim();
  const description = $("ruleFormDesc").value.trim();
  const content = $("ruleFormContent").value;
  const scope = editingRuleId ? editingRuleScope : $("ruleFormScope").value;
  if (!name || !content.trim()) {
    logMessage("error", "规则名称和内容不能为空");
    return;
  }
  try {
    const data = await api("/api/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: editingRuleId, name, description, content, scope }),
    });
    const key = data.scope + ":" + data.id;
    if (settings.enabledRules !== null && settings.enabledRules !== undefined
        && !settings.enabledRules.includes(key)) {
      settings.enabledRules = [...settings.enabledRules, key];
      persistSettings();
    }
    $("ruleEditor").classList.add("hidden");
    state.ruleCache = null;
    await loadRules();
    logMessage("status", "规则已保存：" + name);
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function deleteRule(rule) {
  if (!(await dialog({ title: "删除规则", message: "确定删除「" + rule.name + "」？" }))) return;
  try {
    await api("/api/rules/delete", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ id: rule.id, scope: rule.scope }),
    });
    state.ruleCache = null;
    await loadRules();
    logMessage("status", "已删除规则：" + rule.name);
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("createRule").addEventListener("click", () => openRuleEditor(null));
$("ruleFormCancel").addEventListener("click", () => $("ruleEditor").classList.add("hidden"));
$("ruleFormSave").addEventListener("click", saveRuleForm);

/* ---------- MCP 管理 ---------- */
async function loadMcp() {
  const box = $("mcpList");
  box.innerHTML = '<div class="results-empty">加载中…</div>';
  try {
    const data = await api("/api/mcp");
    state.mcpServers = data.servers || [];
    renderMcpList(state.mcpServers);
  } catch (error) {
    showEmpty(box, error.message);
  }
}

function renderMcpList(servers) {
  const box = $("mcpList");
  box.innerHTML = "";
  if (!servers.length) {
    box.innerHTML = '<div class="results-empty">还没有 MCP 服务，点右上角添加</div>';
    return;
  }
  for (const server of servers) {
    const row = document.createElement("div");
    row.className = "manage-item";
    const icon = document.createElement("div");
    icon.className = "manage-icon";
    icon.textContent = server.name.slice(0, 1).toUpperCase();
    const main = document.createElement("div");
    main.className = "manage-main";
    const name = document.createElement("div");
    name.className = "manage-name";
    name.textContent = server.name;
    const desc = document.createElement("div");
    desc.className = "manage-desc";
    desc.textContent = [server.command, ...(server.args || [])].join(" ");
    main.append(name, desc);
    const actions = document.createElement("div");
    actions.className = "manage-actions";
    const toggle = document.createElement("button");
    toggle.className = "switch" + (server.enabled ? " on" : "");
    toggle.title = server.enabled ? "停用" : "启用";
    toggle.addEventListener("click", () => toggleMcp(server.name));
    const edit = document.createElement("button");
    edit.className = "mini-btn";
    edit.textContent = "编辑";
    edit.addEventListener("click", openMcpEditor);
    actions.append(toggle, edit);
    row.append(icon, main, actions);
    box.appendChild(row);
  }
}

async function toggleMcp(name) {
  const server = (state.mcpServers || []).find((item) => item.name === name);
  if (!server) return;
  try {
    await api("/api/mcp/toggle", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, enabled: !server.enabled }),
    });
    await loadMcp();
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function openMcpEditor() {
  try {
    const data = await api("/api/mcp");
    $("mcpEditorText").value = data.text;
    $("mcpEditor").classList.remove("hidden");
  } catch (error) {
    logMessage("error", error.message);
  }
}

async function saveMcpEditor() {
  try {
    const result = await api("/api/mcp", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text: $("mcpEditorText").value }),
    });
    $("mcpEditor").classList.add("hidden");
    state.mcpServers = null;
    await loadMcp();
    logMessage("status", "MCP 配置已保存，共 " + result.servers + " 个服务");
  } catch (error) {
    logMessage("error", error.message);
  }
}

$("addMcp").addEventListener("click", openMcpEditor);
$("mcpEditorCancel").addEventListener("click", () => $("mcpEditor").classList.add("hidden"));
$("mcpEditorSave").addEventListener("click", saveMcpEditor);

$("maxSteps").addEventListener("change", () => { settings.maxSteps = $("maxSteps").value; persistSettings(); });
$("autoAllow").addEventListener("change", () => { settings.autoAllow = $("autoAllow").checked; persistSettings(); });
$("planFirst").addEventListener("change", () => { settings.planFirst = $("planFirst").checked; persistSettings(); });
$("theme").addEventListener("change", () => applyTheme($("theme").value));
