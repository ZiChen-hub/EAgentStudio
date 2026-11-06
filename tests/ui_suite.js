/* EAgent Studio 页面内 UI 测试套件。
 *
 * 由 tests/ui_check.py 注入执行，执行完把结果写到 window.__uiResults。
 * 只使用页面里已有的全局函数，不依赖测试框架。
 */
(async () => {
  const results = [];
  const runtimeErrors = [];
  const consoleErrors = [];

  const $ = (id) => document.getElementById(id);
  const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const assert = (condition, message) => {
    if (!condition) throw new Error(message || "断言失败");
  };
  const isVisible = (element) => {
    if (!element || element.classList.contains("hidden")) return false;
    const rect = element.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const waitFor = async (probe, label, timeout = 8000) => {
    const started = Date.now();
    let lastError = null;
    while (Date.now() - started < timeout) {
      try {
        const value = probe();
        if (value) return value;
      } catch (error) {
        lastError = error;
      }
      await wait(60);
    }
    throw new Error("等待超时：" + label + (lastError ? "（" + lastError.message + "）" : ""));
  };
  const fire = (element, type) => element.dispatchEvent(new Event(type, { bubbles: true }));
  const setValue = (element, value) => {
    element.value = value;
    if (element.setSelectionRange) element.setSelectionRange(value.length, value.length);
    fire(element, "input");
    fire(element, "change");
  };
  const press = (element, key, extra = {}) =>
    element.dispatchEvent(
      new KeyboardEvent("keydown", { key, bubbles: true, cancelable: true, ...extra })
    );
  const click = (element, label) => {
    assert(element, (label || "元素") + " 不存在");
    element.click();
  };
  const closeAll = async () => {
    for (const id of [
      "settingsModal", "modelEditor", "skillEditor", "ruleEditor", "mcpEditor",
      "quickOpen", "ctxMenu", "modelMenu", "onboarding", "chatHistoryPop",
    ]) {
      const element = $(id);
      if (element) element.classList.add("hidden");
    }
    if ($("refPopup")) $("refPopup").classList.add("hidden");
    if (!$("dialog").classList.contains("hidden")) closeDialog(null);
    await wait(40);
  };
  const check = async (name, fn) => {
    const started = performance.now();
    try {
      const detail = await fn();
      results.push({
        name,
        ok: true,
        detail: detail == null ? "" : String(detail).slice(0, 500),
        ms: Math.round(performance.now() - started),
      });
    } catch (error) {
      results.push({
        name,
        ok: false,
        detail: String((error && error.message) || error).slice(0, 500),
        ms: Math.round(performance.now() - started),
      });
    }
  };

  window.addEventListener("error", (event) => {
    runtimeErrors.push(String(event.message || event.error));
  });
  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    runtimeErrors.push(
      "unhandledrejection: " + (reason && reason.stack ? reason.stack : String(reason))
    );
  });
  const originalConsoleError = console.error;
  console.error = (...args) => {
    consoleErrors.push(args.map((item) => String(item)).join(" "));
    originalConsoleError.apply(console, args);
  };

  try {
    // 首次进入会被引导层挡住，先关掉，后面单独测引导流程
    if (!$("onboarding").classList.contains("hidden")) {
      localStorage.setItem("eagent-onboard-version", "3");
      $("onboarding").classList.add("hidden");
    }
  } catch (error) {
    /* 忽略 localStorage 异常 */
  }

  try {
    await check("启动完成（Monaco / 工作目录 / 文件树）", async () => {
      assert(window.monaco, "Monaco 未加载");
      assert($("workspace").value, "工作目录为空");
      await waitFor(() => document.querySelectorAll("#tree .node").length > 0, "文件树渲染");
      return $("workspace").value;
    });

    await check("DOM 没有重复 id", () => {
      const ids = [...document.querySelectorAll("[id]")].map((element) => element.id);
      const duplicates = ids.filter((id, index) => ids.indexOf(id) !== index);
      assert(duplicates.length === 0, "重复 id：" + [...new Set(duplicates)].join(", "));
      return ids.length + " 个 id";
    });

    await check("脚本引用的元素都存在", async () => {
      const files = [
        "core", "editor", "agent", "memory", "terminal", "workspace", "ui",
        "git", "panels", "models", "settings", "layout", "refs", "shortcuts",
      ];
      const missing = [];
      for (const name of files) {
        const response = await fetch("/ui/js/" + name + ".js", { cache: "no-store" });
        const text = await response.text();
        for (const match of text.matchAll(/\$\("([^"]+)"\)/g)) {
          if (!document.getElementById(match[1]) && !missing.includes(match[1])) {
            missing.push(match[1]);
          }
        }
      }
      assert(missing.length === 0, "缺失元素：" + missing.join(", "));
      return files.length + " 个脚本";
    });

    await check("主要区域可见", () => {
      const selectors = [
        ".topbar", ".layout", ".sidebar", "#editor", ".chat", ".terminal",
        "#messages", "#task", "#termBody", "#termInput",
      ];
      for (const selector of selectors) {
        assert(isVisible(document.querySelector(selector)), selector + " 不可见");
      }
      return selectors.length + " 个区域";
    });

    await check("顶栏与三栏尺寸合理", () => {
      const rect = (selector) => document.querySelector(selector).getBoundingClientRect();
      const topbar = rect(".topbar");
      const sidebar = rect(".sidebar");
      const chat = rect(".chat");
      const terminal = rect(".terminal");
      assert(topbar.height >= 36 && topbar.height <= 64, "顶栏高度异常：" + topbar.height);
      assert(sidebar.width >= 180 && sidebar.width <= 460, "侧栏宽度异常：" + sidebar.width);
      assert(chat.width >= 280 && chat.width <= 560, "聊天宽度异常：" + chat.width);
      assert(terminal.height >= 100, "终端高度异常：" + terminal.height);
      return `顶栏 ${topbar.height}px / 侧栏 ${sidebar.width}px / 聊天 ${chat.width}px / 终端 ${terminal.height}px`;
    });

    await check("没有横向溢出", () => {
      const overflow = document.documentElement.scrollWidth - window.innerWidth;
      assert(overflow <= 1, "横向溢出 " + overflow + "px");
      return "overflow=" + overflow;
    });

    await check("终端前缀只读且格式正确", () => {
      const prompt = $("termPrompt");
      assert(prompt, "终端前缀不存在");
      assert(prompt.textContent.startsWith("PS "), "前缀格式异常：" + prompt.textContent);
      assert(prompt.tagName !== "INPUT", "前缀不应该是输入框");
      const input = $("termInput");
      assert(input && input.tagName === "INPUT", "终端输入框不存在");
      assert(!input.readOnly && !input.disabled, "终端输入框不可编辑");
      return prompt.textContent;
    });

    await check("编辑器已挂载 Monaco", () => {
      assert(isVisible($("editor")), "编辑器区域不可见");
      assert(document.querySelector("#editor .monaco-editor"), "Monaco 没有挂载");
    });

    await check("面板折叠：侧栏", () => {
      const layout = document.querySelector(".layout");
      const sidebar = document.querySelector(".sidebar");
      click($("handleSidebar"));
      assert(layout.classList.contains("sidebar-closed"), "侧栏没有折叠");
      assert(sidebar.classList.contains("collapsed"), "侧栏没有 collapsed 状态");
      click($("handleSidebar"));
      assert(!layout.classList.contains("sidebar-closed"), "侧栏没有恢复");
    });

    await check("面板折叠：聊天", () => {
      const layout = document.querySelector(".layout");
      click($("handleChat"));
      assert(layout.classList.contains("chat-closed"), "聊天面板没有折叠");
      click($("handleChat"));
      assert(!layout.classList.contains("chat-closed"), "聊天面板没有恢复");
    });

    await check("面板折叠：终端", () => {
      const layout = document.querySelector(".layout");
      click($("handleTerminal"));
      assert(layout.classList.contains("terminal-closed"), "终端没有折叠");
      click($("handleTerminal"));
      assert(!layout.classList.contains("terminal-closed"), "终端没有恢复");
    });

    await check("设置弹窗反复打开关闭", async () => {
      for (let index = 0; index < 3; index += 1) {
        click($("openSettings"));
        assert(isVisible($("settingsModal")), "设置没有打开");
        click($("closeSettings"));
        assert(!isVisible($("settingsModal")), "设置没有关闭");
      }
      return "3 次";
    });

    await check("设置分页逐个切换", () => {
      click($("openSettings"));
      const items = [...document.querySelectorAll(".sn-item")];
      assert(items.length >= 7, "设置分页过少：" + items.length);
      for (const item of items) {
        item.click();
        const pane = document.querySelector('.settings-pane[data-pane="' + item.dataset.pane + '"]');
        assert(pane, "分页不存在：" + item.dataset.pane);
        assert(!pane.classList.contains("hidden"), "分页没有显示：" + item.dataset.pane);
        const visiblePanes = [...document.querySelectorAll(".settings-pane")].filter(
          (element) => !element.classList.contains("hidden")
        );
        assert(visiblePanes.length === 1, "同时显示了多个分页");
      }
      click($("closeSettings"));
      return items.length + " 个分页";
    });

    await check("主题切换（浅色 / 深色 / 自动）", () => {
      click($("openSettings"));
      setValue($("theme"), "light");
      assert(document.documentElement.dataset.theme === "light", "浅色主题未生效");
      setValue($("theme"), "dark");
      assert(document.documentElement.dataset.theme === "dark", "深色主题未生效");
      setValue($("theme"), "auto");
      assert(!document.documentElement.dataset.theme, "自动主题没有清除属性");
      click($("closeSettings"));
    });

    await check("界面缩放与重置", () => {
      setZoom(1.2);
      assert(Math.abs((settings.zoom || 1) - 1.2) < 0.01, "缩放值没有生效");
      assert(document.documentElement.style.zoom === "1.2", "页面 zoom 没有生效");
      click($("openSettings"));
      click($("zoomReset"));
      assert(document.documentElement.style.zoom === "1", "缩放没有恢复");
      click($("closeSettings"));
    });

    await check("设置保存并持久化", async () => {
      click($("openSettings"));
      setValue($("maxSteps"), "20");
      click($("saveSettings"));
      await wait(120);
      const saved = JSON.parse(localStorage.getItem("eagent-studio-settings") || "{}");
      assert(saved.maxSteps === "20", "设置没有持久化：" + saved.maxSteps);
      click($("openSettings"));
      assert($("maxSteps").value === "20", "重新打开后设置没有恢复");
      click($("closeSettings"));
      return "maxSteps=20";
    });

    await check("模型编辑器可以打开并取消", () => {
      click($("openSettings"));
      document.querySelector('.sn-item[data-pane="models"]').click();
      click($("addModel"));
      assert(isVisible($("modelEditor")), "模型编辑器没有打开");
      click($("modelFormCancel"));
      assert(!isVisible($("modelEditor")), "模型编辑器没有关闭");
      click($("closeSettings"));
    });

    await check("模型预设可以填充表单", () => {
      click($("openSettings"));
      document.querySelector('.sn-item[data-pane="models"]').click();
      click($("addModel"));
      setValue($("modelPreset"), "deepseek");
      assert($("modelFormBase").value.includes("api.deepseek.com"), "Base URL 未填充");
      assert($("modelFormModel").value === "deepseek-chat", "模型名未填充");
      click($("modelFormCancel"));
      click($("closeSettings"));
    });

    await check("模型菜单可以打开并关闭", async () => {
      click($("modelChip"));
      assert(isVisible($("modelMenu")), "模型菜单没有打开");
      $("statusText").click();
      await wait(80);
      assert(!isVisible($("modelMenu")), "模型菜单没有关闭");
    });

    await check("技能 / 规则 / MCP / 白名单编辑器", async () => {
      click($("openSettings"));
      document.querySelector('.sn-item[data-pane="skills"]').click();
      click($("createSkill"));
      assert(isVisible($("skillEditor")), "技能编辑器没有打开");
      click($("skillFormCancel"));
      assert(!isVisible($("skillEditor")), "技能编辑器没有关闭");

      document.querySelector('.sn-item[data-pane="rules"]').click();
      click($("createRule"));
      assert(isVisible($("ruleEditor")), "规则编辑器没有打开");
      click($("ruleFormCancel"));
      assert(!isVisible($("ruleEditor")), "规则编辑器没有关闭");

      document.querySelector('.sn-item[data-pane="mcp"]').click();
      click($("addMcp"));
      await waitFor(() => isVisible($("mcpEditor")), "MCP 编辑器没有打开");
      click($("mcpEditorCancel"));
      assert(!isVisible($("mcpEditor")), "MCP 编辑器没有关闭");

      document.querySelector('.sn-item[data-pane="whitelist"]').click();
      click($("addWhitelist"));
      assert(isVisible($("dialog")), "白名单弹窗没有打开");
      click($("dialogCancel"));
      await wait(100);
      assert(!isVisible($("dialog")), "白名单弹窗没有关闭");
      click($("cancelSettings"));
      assert(!isVisible($("settingsModal")), "取消按钮没有关闭设置");
    });

    await check("记忆保存按钮", async () => {
      click($("openSettings"));
      document.querySelector('.sn-item[data-pane="memory"]').click();
      await waitFor(() => $("memoryProject").value.includes("项目使用 Python"), "记忆没有加载");
      setValue($("memoryProject"), $("memoryProject").value + "\n- UI 测试写入\n");
      click($("saveMemory"));
      await wait(300);
      const response = await fetch(
        "/api/memory?token=" + encodeURIComponent(apiToken()),
        { cache: "no-store" }
      );
      const data = await response.json();
      assert(data.project.includes("UI 测试写入"), "记忆没有保存");
      click($("closeSettings"));
    });

    await check("模式按钮循环切换", () => {
      const before = settings.agentMode;
      click($("modeChip"));
      assert(settings.agentMode !== before, "模式没有切换");
      click($("modeChip"));
      click($("modeChip"));
      assert(settings.agentMode === before, "模式没有循环回原值：" + settings.agentMode);
      return before;
    });

    await check("侧栏分页逐个切换", async () => {
      const tabs = ["files", "search", "git", "snapshots", "tests", "history"];
      for (const tab of tabs) {
        const button = document.querySelector('#sideTabs button[data-tab="' + tab + '"]');
        assert(button, "侧栏分页按钮缺失：" + tab);
        button.click();
        const pane = $("pane-" + tab);
        assert(pane && !pane.classList.contains("hidden"), "分页没有显示：" + tab);
        assert(pane.getBoundingClientRect().height > 0, "分页高度为 0：" + tab);
      }
      return tabs.length + " 个分页";
    });

    await check("搜索有结果并能打开文件", async () => {
      document.querySelector('#sideTabs button[data-tab="search"]').click();
      await wait(120);
      setValue($("searchInput"), "hello");
      const rows = await waitFor(
        () => {
          const list = document.querySelectorAll("#searchResults .result");
          return list.length ? list : null;
        },
        "搜索结果"
      );
      rows[0].click();
      await waitFor(() => state.active && state.active.endsWith("sample.py"), "打开搜索结果");
      return state.active;
    });

    await check("清空搜索无残留结果", async () => {
      setValue($("searchInput"), "");
      await wait(700);
      const leftover = document.querySelectorAll("#searchResults .result").length;
      assert(leftover === 0, "清空后仍有 " + leftover + " 条结果");
    });

    await check("Ctrl+P 快速打开", async () => {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "p", ctrlKey: true, bubbles: true }));
      assert(isVisible($("quickOpen")), "快速打开没有显示");
      setValue($("quickInput"), "sample");
      const items = await waitFor(
        () => {
          const list = document.querySelectorAll("#quickList .palette-item");
          return list.length ? list : null;
        },
        "快速打开结果"
      );
      items[0].click();
      await waitFor(() => state.active === "sample.py", "快速打开没有打开文件");
      assert(!isVisible($("quickOpen")), "快速打开没有关闭");
    });

    await check("文件树刷新并打开文件", async () => {
      document.querySelector('#sideTabs button[data-tab="files"]').click();
      click($("refreshTree"));
      await waitFor(() => document.querySelector('#tree .node[data-path="sample.py"]'), "文件树刷新");
      document.querySelector('#tree .node[data-path="sample.py"]').click();
      await waitFor(() => state.active === "sample.py", "文件树打开文件");
      assert($("tabs").textContent.includes("sample.py"), "标签页没有显示文件名");
    });

    await check("编辑器修改并保存", async () => {
      document.querySelector('#tree .node[data-path="edit-me.txt"]').click();
      await waitFor(() => state.active === "edit-me.txt", "打开 edit-me.txt");
      state.editor.setValue("edited by ui suite\n");
      await saveActive();
      const response = await fetch(
        "/api/file?path=edit-me.txt&token=" + encodeURIComponent(apiToken()),
        { cache: "no-store" }
      );
      const data = await response.json();
      assert(data.content === "edited by ui suite\n", "保存内容不一致");
      return "edit-me.txt";
    });

    await check("新建文件对话框", async () => {
      click($("newFile"));
      assert(isVisible($("dialog")), "新建文件弹窗没有出现");
      assert(!$("dialogInput").classList.contains("hidden"), "新建文件输入框没有显示");
      $("dialogInput").value = "ui-created.txt";
      click($("dialogOk"));
      await waitFor(
        () => document.querySelector('#tree .node[data-path="ui-created.txt"]'),
        "新建文件没有出现在树中"
      );
    });

    await check("右键菜单出现并关闭", async () => {
      const node = document.querySelector('#tree .node[data-path="sample.py"]');
      node.dispatchEvent(
        new MouseEvent("contextmenu", { bubbles: true, cancelable: true, clientX: 120, clientY: 140 })
      );
      assert(isVisible($("ctxMenu")), "右键菜单没有显示");
      assert($("ctxMenu").textContent.includes("重命名"), "右键菜单内容异常");
      $("statusText").click();
      await wait(80);
      assert(!isVisible($("ctxMenu")), "右键菜单没有关闭");
    });

    await check("Git 面板初始化仓库", async () => {
      document.querySelector('#sideTabs button[data-tab="git"]').click();
      await waitFor(
        () => !$("gitChanges").textContent.includes("读取中"),
        "Git 面板加载"
      );
      assert(isVisible($("gitInit")), "初始化仓库按钮没有显示");
      click($("gitInit"));
      await waitFor(() => !isVisible($("gitInit")), "Git 初始化没有完成");
      click($("gitRefresh"));
      await waitFor(
        () => !$("gitInfo").textContent.includes("还不是 Git 仓库"),
        "Git 状态没有刷新"
      );
    });

    await check("测试命令自动探测", async () => {
      document.querySelector('#sideTabs button[data-tab="tests"]').click();
      await waitFor(() => $("testCommand").value.trim(), "测试命令没有探测到");
      return $("testCommand").value;
    });

    await check("运行一条快速测试", async () => {
      setValue($("testCommand"), "python -c \"print('ui-ok')\"");
      click($("testRun"));
      await waitFor(
        () => /通过|失败/.test($("testStatus").textContent),
        "测试没有完成",
        20000
      );
      assert($("testStatus").textContent.includes("通过"), "快速测试没有通过：" + $("testStatus").textContent);
      assert($("testOutput").textContent.includes("ui-ok"), "测试输出缺失");
      return $("testStatus").textContent;
    });

    await check("快照列表与回滚确认框", async () => {
      document.querySelector('#sideTabs button[data-tab="snapshots"]').click();
      await waitFor(() => document.querySelector("#snapshotsList .result"), "快照列表没有加载");
      const button = [...document.querySelectorAll("#snapshotsList button")].find((item) =>
        item.textContent.includes("回滚")
      );
      assert(button, "回滚按钮缺失");
      button.click();
      assert(isVisible($("dialog")), "回滚确认框没有出现");
      click($("dialogCancel"));
      await wait(100);
      assert(!isVisible($("dialog")), "回滚确认框没有关闭");
    });

    await check("历史会话列表并加载", async () => {
      document.querySelector('#sideTabs button[data-tab="history"]').click();
      await waitFor(() => document.querySelector("#historyList .result"), "历史列表没有加载");
      document.querySelector("#historyList .result").click();
      await waitFor(() => $("messages").textContent.includes("历史任务"), "历史会话没有加载");
      assert($("messages").textContent.includes("历史回复"), "历史事件没有渲染");
    });

    await check("终端执行命令", async () => {
      if (document.querySelector(".layout").classList.contains("terminal-closed")) {
        click($("handleTerminal"));
      }
      setValue($("termInput"), "Write-Output ui-test-ok");
      press($("termInput"), "Enter");
      await waitFor(
        () => !termBusy && /(^|\n)ui-test-ok(\r?\n|$)/.test($("termOut").textContent),
        "终端命令没有执行",
        20000
      );
    });

    await check("终端复制按钮", async () => {
      click($("termCopy"));
      await waitFor(() => $("termCopy").textContent === "已复制", "复制按钮没有反馈", 3000);
    });

    await check("终端 cd 并恢复目录", async () => {
      const original = $("workspace").value;
      const oldPrompt = $("termPrompt").textContent;
      setValue($("termInput"), "cd ..");
      press($("termInput"), "Enter");
      try {
        await waitFor(() => $("termPrompt").textContent !== oldPrompt, "cd 后前缀没有变化", 15000);
      } catch (error) {
        throw new Error(
          error.message +
            "（busy=" + termBusy + ", input=" + JSON.stringify($("termInput").value) +
            ", out=" + JSON.stringify($("termOut").textContent.slice(-120)) + "）"
        );
      }
      assert($("termPrompt").textContent.startsWith("PS "), "cd 后前缀格式异常");
      setValue($("termInput"), 'cd "' + original + '"');
      press($("termInput"), "Enter");
      await waitFor(
        () => $("termPrompt").textContent === "PS " + original + ">",
        "cd 没有恢复工作目录",
        15000
      );
    });

    await check("终端清屏", () => {
      click($("termClear"));
      assert($("termOut").textContent === "", "终端没有清空");
    });

    await check("引用弹层：/ 技能", async () => {
      setValue($("task"), "/");
      await waitFor(
        () => isVisible($("refPopup")) && $("refPopup").textContent.includes("演示技能"),
        "技能引用没有出现"
      );
      press($("task"), "Escape");
      assert(!isVisible($("refPopup")), "技能引用没有关闭");
      setValue($("task"), "");
    });

    await check("引用弹层：@ 文件", async () => {
      setValue($("task"), "@");
      await waitFor(
        () => isVisible($("refPopup")) && $("refPopup").textContent.includes("sample.py"),
        "文件引用没有出现"
      );
      press($("task"), "Escape");
      setValue($("task"), "");
    });

    await check("引用弹层：& 符号", async () => {
      setValue($("task"), "&");
      await waitFor(
        () => isVisible($("refPopup")) && $("refPopup").textContent.includes("hello"),
        "符号引用没有出现"
      );
      press($("task"), "Escape");
      setValue($("task"), "");
      clearReferences();
    });

    await check("聊天记录弹层", async () => {
      click($("chatHistoryBtn"));
      assert(isVisible($("chatHistoryPop")), "聊天记录弹层没有打开");
      $("statusText").click();
      await wait(100);
      assert(!isVisible($("chatHistoryPop")), "聊天记录弹层没有关闭");
    });

    await check("空任务点击发送无副作用", async () => {
      setValue($("task"), "");
      const before = $("messages").children.length;
      click($("sendStop"));
      await wait(150);
      assert(!$("sendStop").classList.contains("running"), "空任务不应进入运行状态");
      assert($("messages").children.length === before, "空任务不应产生消息");
    });

    await check("弹窗并发：后一个会取消前一个", async () => {
      const first = dialog({ title: "弹窗 A" });
      const second = dialog({ title: "弹窗 B" });
      const firstValue = await first;
      assert(firstValue === null, "旧弹窗没有按取消结束");
      assert(isVisible($("dialog")), "新弹窗没有显示");
      click($("dialogCancel"));
      const secondValue = await second;
      assert(secondValue === null, "新弹窗取消结果异常");
    });

    await check("只读 diff 预览与关闭", () => {
      showDiff({ path: "sample.py", old: "a\n", new: "b\n" }, true);
      assert(isVisible($("diff")), "diff 区域没有显示");
      assert(!isVisible($("accept")) && isVisible($("closeDiff")), "只读 diff 按钮状态异常");
      click($("closeDiff"));
      assert(!isVisible($("diff")) && isVisible($("editor")), "diff 关闭后编辑器没有恢复");
    });

    await check("切换工作目录弹窗可以取消", async () => {
      click($("workspace"));
      await waitFor(
        () => isVisible($("dialog")) && !$("dialogInput").classList.contains("hidden"),
        "切换目录弹窗没有出现"
      );
      click($("dialogCancel"));
      await wait(80);
      assert(!isVisible($("dialog")), "切换目录弹窗没有关闭");
    });

    await check("引导流程（上一步 / 4 步 / 跳过）", async () => {
      localStorage.removeItem("eagent-onboard-version");
      maybeShowOnboarding();
      assert(isVisible($("onboarding")), "引导没有显示");
      assert($("onboardIndex").textContent.includes("/ 4"), "引导步数异常：" + $("onboardIndex").textContent);
      click($("onboardNext"));
      assert($("onboardIndex").textContent.startsWith("2 / 4"), "没有进入第二步");
      click($("onboardPrev"));
      assert($("onboardIndex").textContent.startsWith("1 / 4"), "上一步没有生效");
      for (let index = 0; index < 3; index += 1) click($("onboardNext"));
      assert($("onboardNext").textContent === "开始使用", "最后一步按钮文案异常");
      assert($("onboardSkip").classList.contains("hidden"), "最后一步不应显示跳过");
      click($("onboardNext"));
      assert(!isVisible($("onboarding")), "引导没有关闭");
      assert(localStorage.getItem("eagent-onboard-version") === "3", "引导版本没有写入");

      localStorage.removeItem("eagent-onboard-version");
      maybeShowOnboarding();
      click($("onboardNext"));
      click($("onboardSkip"));
      assert(!isVisible($("onboarding")), "跳过没有关闭引导");
      assert($("onboardNever").checked, "跳过后没有勾选不再引导");
      assert(localStorage.getItem("eagent-onboard-version") === "3", "跳过没有写入版本");
    });

    await check("Esc 关闭设置弹窗", () => {
      click($("openSettings"));
      assert(isVisible($("settingsModal")), "设置没有打开");
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));
      assert(!isVisible($("settingsModal")), "Esc 没有关闭设置");
    });

    await check("页面没有未捕获异常", async () => {
      await wait(200);
      assert(runtimeErrors.length === 0, runtimeErrors.join(" | "));
      return consoleErrors.length ? "console.error " + consoleErrors.length + " 条（见报告）" : "无";
    });
  } catch (error) {
    results.push({
      name: "套件执行中断",
      ok: false,
      detail: String((error && error.stack) || error).slice(0, 800),
      ms: 0,
    });
  } finally {
    try {
      await closeAll();
    } catch (error) {
      /* 收尾失败不影响结果 */
    }
    window.__uiConsoleErrors = consoleErrors;
    window.__uiResults = results;
    window.__uiDone = true;
  }
})();
