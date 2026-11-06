"""Office 文档读写：docx/xlsx 标准库，pptx 依赖 PowerPoint COM。"""

from __future__ import annotations

import sys
import tempfile
import unittest
import winreg
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from support import ROOT  # noqa: E402,F401

from eagent_bridge.office import (  # noqa: E402
    read_docx,
    read_pptx,
    read_xlsx,
    write_docx,
    write_pptx,
    write_xlsx,
)
from eagent_bridge.tools import ToolError, execute  # noqa: E402


def powerpoint_available() -> bool:
    try:
        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "PowerPoint.Application"))
        return True
    except OSError:
        return False


class DocxTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.docx"
            write_docx(
                path,
                [
                    {"type": "heading", "text": "项目周报", "level": 1},
                    {"type": "paragraph", "text": "第一段\n第二行"},
                    {"type": "table", "rows": [["名称", "数量"], ["苹果", "3"]]},
                ],
            )

            text = read_docx(path)

            self.assertIn("项目周报", text)
            self.assertIn("第一段", text)
            self.assertIn("名称 | 数量", text)


class XlsxTests(unittest.TestCase):
    def test_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.xlsx"
            write_xlsx(
                path,
                [
                    {
                        "name": "数据",
                        "rows": [
                            ["名称", "数量"],
                            ["苹果", 3],
                            ["合计", "=SUM(B2:B2)"],
                        ],
                    }
                ],
            )

            sheets = read_xlsx(path)

            self.assertEqual(sheets[0]["name"], "数据")
            self.assertEqual(sheets[0]["rows"][0], ["名称", "数量"])
            self.assertEqual(sheets[0]["rows"][1], ["苹果", "3"])
            self.assertEqual(sheets[0]["rows"][2], ["合计", "=SUM(B2:B2)"])


class OfficeToolTests(unittest.TestCase):
    def test_write_requires_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            with self.assertRaises(ToolError):
                execute(
                    "office_document",
                    {
                        "format": "docx",
                        "action": "write",
                        "path": "a.docx",
                        "blocks": [{"type": "paragraph", "text": "x"}],
                    },
                    workspace,
                    approver=lambda *args: False,
                )
            self.assertFalse((workspace / "a.docx").exists())

    def test_read_does_not_require_approval(self):
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)
            execute(
                "office_document",
                {
                    "format": "docx",
                    "action": "write",
                    "path": "a.docx",
                    "blocks": [{"type": "paragraph", "text": "只读测试"}],
                },
                workspace,
                auto_allow=True,
            )

            def approver(*args):
                raise AssertionError("只读操作不应该请求确认")

            result = execute(
                "office_document",
                {"format": "docx", "action": "read", "path": "a.docx"},
                workspace,
                approver=approver,
            )
            self.assertIn("只读测试", result)

    def test_blocks_path_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ToolError):
                execute(
                    "office_document",
                    {
                        "format": "docx",
                        "action": "write",
                        "path": "../outside.docx",
                        "blocks": [],
                    },
                    Path(folder),
                    auto_allow=True,
                )


@unittest.skipUnless(powerpoint_available(), "需要安装 PowerPoint 才能测试 pptx 写入")
class PptxTests(unittest.TestCase):
    def test_write_and_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "a.pptx"
            write_pptx(
                path,
                [
                    {"title": "第一页", "bullets": ["要点一", "要点二"]},
                    {"title": "第二页", "bullets": []},
                ],
            )

            slides = read_pptx(path)

            self.assertEqual(len(slides), 2)
            self.assertIn("第一页", slides[0])
            self.assertIn("要点一", slides[0])
            self.assertIn("第二页", slides[1])


if __name__ == "__main__":
    unittest.main()
