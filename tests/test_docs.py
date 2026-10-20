"""文档契约测试：文档中宣称的数字必须与代码实际一致。

覆盖：
- README 声明的接入工具数 == 业务层实际调用的工具数；
- MCP_INTEGRATION 声明的服务端工具数 == `TOOL_ARG_SPEC` 登记数；
- README 声明的单测项数 == 实际用例数；
- CHANGELOG 含当前版本小节。

价值：任何一次「加测试 / 加工具 / 升版本」忘记同步文档都会立刻变红，
而不是等到发版前人工扫描（历史上已多次出现文档陈旧）。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import __version__  # noqa: E402
from src.mcp_client import TOOL_ARG_SPEC  # noqa: E402


def read_doc(name: str) -> str:
    with open(os.path.join(ROOT, name), encoding="utf-8") as handle:
        return handle.read()


def count_tests() -> int:
    """统计当前测试套件里的用例数（与 `unittest discover` 口径一致）。"""
    suite = unittest.TestLoader().discover(os.path.join(ROOT, "tests"), pattern="test_*.py")

    def walk(node) -> int:
        if isinstance(node, unittest.TestSuite):
            return sum(walk(child) for child in node)
        return 1

    return walk(suite)


def used_tool_count() -> int:
    """业务层实际调用的工具数（正则提取，避免文档与实现各写一个常量）。"""
    source = read_doc(os.path.join("src", "party_planner.py"))
    names = set(re.findall(r'call_tool\(\s*"([a-z0-9-]+)"', source))
    names |= set(re.findall(r'step\(\s*"([a-z0-9-]+)"', source))
    return len(names)


class DocContractTest(unittest.TestCase):
    def test_readme_test_count_matches_suite(self):
        declared = int(re.search(r"单元测试\s*(\d+)\s*项", read_doc("README.md")).group(1))
        self.assertEqual(declared, count_tests(),
                         "README 的单测项数已过期，请同步")

    def test_readme_tool_count_matches_usage(self):
        declared = int(re.search(r"接入\s*\*\*(\d+)\s*个 Tool\*\*", read_doc("README.md")).group(1))
        self.assertEqual(declared, used_tool_count(),
                         "README 的接入工具数已过期，请同步")

    def test_integration_tool_count_matches_spec(self):
        text = read_doc("MCP_INTEGRATION.md")
        declared = int(re.search(r"服务端当前共\s*(\d+)\s*个 Tool", text).group(1))
        self.assertEqual(declared, len(TOOL_ARG_SPEC),
                         "MCP_INTEGRATION 的服务端工具数已过期，请同步")

    def test_changelog_mentions_current_version(self):
        self.assertIn(f"## [{__version__}]", read_doc("CHANGELOG.md"),
                      "CHANGELOG 缺少当前版本小节")


if __name__ == "__main__":
    unittest.main(verbosity=2)
