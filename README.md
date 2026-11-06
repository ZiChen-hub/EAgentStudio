# EAgent Studio

本地桌面编码 Agent。用 Edge + CDP 把 DeepSeek / 豆包 / 元宝网页版当成模型后端，也可以接 OpenAI 兼容接口或 Ollama；在同一个窗口里读写代码、跑命令、看 diff、回滚改动。

## 快速开始

```powershell
python -m pip install -r requirements.txt
python -m eagent_bridge.desktop
```

首次启动会自动打开 Edge：在窗口里登录网页 AI，登录态会保存在本机，之后可以直接下任务。

没有 pywebview 时，也可以只用浏览器访问：

```powershell
python -m eagent_bridge.webui
```

## 其他入口

| 命令 | 作用 |
| --- | --- |
| `python -m eagent_bridge.desktop` | 桌面窗口（推荐） |
| `python -m eagent_bridge.webui` | 浏览器版界面 |
| `python -m eagent_bridge.server` | OpenAI 兼容端点 `http://127.0.0.1:8765/v1` |
| `python -m eagent_bridge.mcp_server` | MCP stdio 服务，把网页 AI 暴露给外部宿主 |
| `python -m eagent_bridge.cli selftest` | 用一次性的无头 Edge 自检 CDP 链路 |

## 主要能力

- Agent 循环：工具调用、计划确认、逐步审批、命令流式输出、停止与任务队列
- 工具：文件读写/搜索、通配符查找、批量复制移动删除、HTTP 请求与下载、Office 文档、命令执行、测试运行
- 安全：写文件/执行命令前确认，白名单、只读工具自动放行、任务快照与一键回滚
- 代码：Monaco 编辑器、文件树、搜索、符号跳转与引用、测试运行器、Git 面板、AI 生成提交信息
- Office：读写 docx/xlsx（纯标准库，无需安装 Office）；pptx 读取用标准库，写入需要本机安装 PowerPoint
- 上下文：项目规则、长期记忆、技能、`.eagent` 会话记录、长任务自动压缩
- 模型：网页 AI / OpenAI 兼容 / Ollama，内置 DeepSeek API、OpenRouter、Gemini、Groq、本地 Ollama 预设
- 桌面：计算机模式与混合模式（截图、鼠标键盘、窗口、MCP 桌面工具）

## 目录结构

```
eagent_bridge/
  runtime.py       Agent 循环、工具调度、上下文压缩、备用模型切换
  tools.py         文件/搜索/命令等内置工具与路径校验
  backends.py      网页 AI / OpenAI 兼容 / Ollama 后端
  adapters.py      各网页 AI 站点的选择器与收发消息
  session.py       浏览器启动、标签页复用、Cookie 持久化
  webui.py         本地 HTTP 服务、SSE、所有前端接口
  ui/index.html    页面结构
  ui/styles.css    界面样式
  ui/js/*.js       按功能拆分的脚本（核心、编辑器、Agent、终端、面板等）
  snapshots.py     任务快照与回滚
  index.py         代码索引、符号与引用检索
  mcp_client.py    MCP 客户端
  storage.py       会话/截图等运行数据清理
```

## 安全说明

- 服务只监听 `127.0.0.1`，并且除 `/api/health` 外都要求启动时生成的 token
- 请求 Host 必须是回环地址，避免 DNS rebinding
- 所有文件路径限制在工作目录内，快照回滚也会校验路径
- 请求体上限 20MB
- 运行数据（会话、快照、截图、日志）统一放在工作目录的 `.eagent/`，已加入 `.gitignore`
- `/v1` 端点默认关闭跨域访问；设置 `EAGENT_BRIDGE_API_KEY` 后才启用 CORS

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EAGENT_BRIDGE_WEBUI_PORT` | `8766` | 界面端口 |
| `EAGENT_BRIDGE_WORKSPACE` | 当前目录 | 默认工作目录 |
| `EAGENT_BRIDGE_PORT` | `9222` | Edge 调试端口 |
| `EAGENT_BRIDGE_PROFILE` | `%LOCALAPPDATA%\EAgentBridge\edge-profile` | 浏览器配置目录 |
| `EAGENT_BRIDGE_OPENAI_PORT` | `8765` | OpenAI 兼容端点端口 |
| `EAGENT_BRIDGE_V1_SITE` | `deepseek` | `/v1` 代理使用的网页 AI 站点 |
| `EAGENT_BRIDGE_API_KEY` | 空 | 设置后 `/v1` 要求 Bearer 校验 |

## 测试

```powershell
python -m unittest discover -s tests
```

装了 pytest 也可以直接 `python -m pytest`。测试覆盖鉴权、Host 校验、路径越界、请求体上限、SSE 续传、终端执行、符号检索、运行时降级/压缩、工具审批与快照回滚，不会启动真实浏览器或调用模型。

推送代码后，GitHub Actions 会在 windows-latest 上执行同样的编译检查、单元测试和前端脚本语法检查（见 `.github/workflows/checks.yml`）。

## 界面自动化检查

```powershell
python tests/ui_check.py
```

脚本会新建临时工作目录、启动本地服务，用无头 Edge 打开真实界面，执行 50 项界面检查（布局尺寸、按钮动作、弹窗、主题、缩放、终端、引用、快捷键、引导、异常捕获等）和 12 项服务端副作用检查。报告、截图和请求日志写到 `.eagent/ui-test/<时间>/`。

常用参数：

- `--keep`：保留临时工作目录和浏览器配置，便于排查
- `--timeout 180`：界面检查最长执行秒数
- `--report 目录`：自定义报告输出位置

