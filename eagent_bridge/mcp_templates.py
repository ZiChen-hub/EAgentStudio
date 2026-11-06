"""常用 MCP Server 模板：一键写入 <workspace>/.eagent/mcp.json。"""

from __future__ import annotations

import json
from pathlib import Path

TEMPLATES: list[dict] = [
    {
        "id": "windows-mcp",
        "name": "Windows 桌面操作",
        "description": "截图、点击、输入、窗口切换等桌面工具",
        "requires": "uv / uvx",
        "config": {
            "command": "uvx",
            "args": [
                "--python", "3.14", "windows-mcp", "serve", "--tools",
                "Snapshot,Screenshot,Click,Type,Shortcut,Scroll,App,WaitFor,Clipboard,DisplayInventory",
            ],
            "env": {
                "ANONYMIZED_TELEMETRY": "false",
                "WINDOWS_MCP_DISABLE_FLASH": "1",
                "WINDOWS_MCP_MERGE_WORD_NODES": "1",
            },
            "autoApprove": False,
            "safeTools": ["Snapshot", "Screenshot", "DisplayInventory", "ControlStatus", "WaitFor"],
            "enabled": True,
        },
    },
    {
        "id": "filesystem",
        "name": "文件系统",
        "description": "通过 MCP 读写指定目录，默认当前工作目录",
        "requires": "npx（Node.js）",
        "config": {
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-filesystem", "{workspace}"],
            "enabled": True,
        },
    },
    {
        "id": "git",
        "name": "Git 仓库",
        "description": "查看提交、差异、分支等 Git 操作，默认当前工作目录",
        "requires": "uv / uvx",
        "config": {
            "command": "uvx",
            "args": ["mcp-server-git", "--repository", "{workspace}"],
            "enabled": True,
        },
    },
    {
        "id": "fetch",
        "name": "网页抓取",
        "description": "抓取网页并转换成 Markdown",
        "requires": "uv / uvx",
        "config": {"command": "uvx", "args": ["mcp-server-fetch"], "enabled": True},
    },
    {
        "id": "time",
        "name": "时间与时区",
        "description": "查询当前时间、时区转换",
        "requires": "uv / uvx",
        "config": {"command": "uvx", "args": ["mcp-server-time"], "enabled": True},
    },
    {
        "id": "playwright",
        "name": "Playwright 浏览器",
        "description": "用 Playwright 做浏览器自动化",
        "requires": "npx（Node.js）",
        "config": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"], "enabled": True},
    },
]


def _config_path(workspace: Path) -> Path:
    return workspace / ".eagent" / "mcp.json"


def _read(workspace: Path) -> dict:
    path = _config_path(workspace)
    if not path.is_file():
        return {"mcpServers": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("mcpServers"), dict):
        data["mcpServers"] = {}
    return data


def _replace_workspace(value, workspace: str):
    if isinstance(value, str):
        return value.replace("{workspace}", workspace)
    if isinstance(value, list):
        return [_replace_workspace(item, workspace) for item in value]
    if isinstance(value, dict):
        return {key: _replace_workspace(item, workspace) for key, item in value.items()}
    return value


def list_templates(workspace: Path) -> list[dict]:
    installed = set(_read(workspace).get("mcpServers") or {})
    items = []
    for template in TEMPLATES:
        items.append(
            {
                "id": template["id"],
                "name": template["name"],
                "description": template["description"],
                "requires": template["requires"],
                "installed": template["id"] in installed,
            }
        )
    return items


def install(workspace: Path, template_id: str) -> dict:
    template = next((item for item in TEMPLATES if item["id"] == template_id), None)
    if template is None:
        raise ValueError(f"未知的 MCP 模板: {template_id}")
    data = _read(workspace)
    servers = data["mcpServers"]
    if template_id in servers:
        raise ValueError(f"MCP 服务已存在: {template_id}")
    servers[template_id] = _replace_workspace(
        json.loads(json.dumps(template["config"])), str(workspace.resolve())
    )
    path = _config_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"name": template_id, "servers": len(servers)}
