/* ---------- 终端 ---------- */

/* ---------- 终端（行输入，整行交给 PowerShell 执行） ---------- */
const termInput = $("termInput");
let termHistory = [];
let termHistoryIndex = 0;
let termBusy = false;

const TERM_MAX_CHARS = 200000;

function termAppend(text) {
  if (!text) return;
  const body = $("termBody");
  const out = $("termOut");
  const stick = body ? body.scrollHeight - body.scrollTop - body.clientHeight < 40 : true;
  let next = out.textContent + text;
  if (next.length > TERM_MAX_CHARS) next = next.slice(next.length - TERM_MAX_CHARS);
  out.textContent = next;
  if (stick && body) body.scrollTop = body.scrollHeight;
}

function updateTermPrompt(cwd) {
  const path = cwd || $("workspace").value || "";
  $("termPrompt").textContent = "PS " + path + ">";
}

async function runTermCommand() {
  const raw = termInput.value;
  const command = raw.trim();
  if (!command || termBusy) return;
  termInput.value = "";
  termHistory.push(command);
  termHistoryIndex = termHistory.length;
  termAppend($("termPrompt").textContent + " " + command + "\n");

  termBusy = true;
  try {
    const data = await api("/api/terminal/exec", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ command }),
    });
    if (data.output) termAppend(data.output.replace(/\r\n/g, "\n"));
    if (data.cwd) updateTermPrompt(data.cwd);
  } catch (error) {
    termAppend("[错误] " + error.message + "\n");
  } finally {
    termBusy = false;
    termInput.focus({ preventScroll: true });
  }
}

function clearTerm() {
  $("termOut").textContent = "";
  termInput.focus({ preventScroll: true });
}

termInput.addEventListener("keydown", (event) => {
  if (event.isComposing || event.key === "Process") return;
  if (event.key === "Enter") {
    event.preventDefault();
    runTermCommand();
    return;
  }
  if (event.key === "ArrowUp") {
    if (!termHistory.length) return;
    event.preventDefault();
    termHistoryIndex = Math.max(0, termHistoryIndex - 1);
    termInput.value = termHistory[termHistoryIndex] || "";
    return;
  }
  if (event.key === "ArrowDown") {
    if (!termHistory.length) return;
    event.preventDefault();
    termHistoryIndex = Math.min(termHistory.length, termHistoryIndex + 1);
    termInput.value = termHistory[termHistoryIndex] || "";
    return;
  }
  if (event.ctrlKey && event.key.toLowerCase() === "l") {
    event.preventDefault();
    clearTerm();
    return;
  }
  if (event.ctrlKey && event.key.toLowerCase() === "c") {
    const selection = window.getSelection();
    if (selection && selection.toString().trim()) return;  // 有选中内容就走浏览器复制
    if (termInput.value) {
      event.preventDefault();
      termInput.value = "";
    }
  }
});

document.querySelector(".terminal").addEventListener("mouseup", (event) => {
  if (event.target.closest("button")) return;
  const selection = window.getSelection();
  if (selection && selection.toString().trim()) return;  // 正在选文本，保留选择
  termInput.focus({ preventScroll: true });
});

$("termClear").addEventListener("click", (event) => {
  event.stopPropagation();
  clearTerm();
});

$("termCopy").addEventListener("click", async (event) => {
  event.stopPropagation();
  // 必须在 await 之前取 currentTarget：异步回来后它会被浏览器置空
  const button = event.currentTarget;
  const text = $("termOut").textContent || "";
  if (!text.trim()) return;
  await copyText(text);
  button.textContent = "已复制";
  setTimeout(() => { button.textContent = "复制"; }, 1200);
});
