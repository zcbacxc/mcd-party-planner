"""配置读取层测试：`load_local_env` 的返回值语义与环境变量覆盖规则。

重点覆盖 G13 修复后的契约：**只返回键名集合，绝不返回键值**，
避免调用方把 Token 明文带进日志、报告或异常信息。
"""

import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import config  # noqa: E402


class LoadLocalEnvTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {}, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_env(self, content: str) -> str:
        path = os.path.join(self.tmp.name, ".env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        return self.tmp.name

    def test_returns_key_names_only(self):
        base = self.write_env("MCD_MCP_TOKEN=secret-token\nMCD_MCP_ENDPOINT=https://mcp.mcd.cn\n")

        loaded = config.load_local_env(base)

        self.assertIsInstance(loaded, set, "应返回键名集合，而非键值字典")
        self.assertEqual(loaded, {"MCD_MCP_TOKEN", "MCD_MCP_ENDPOINT"})
        # 关键断言：返回值里不允许出现 Token 明文
        self.assertNotIn("secret-token", repr(loaded))

    def test_writes_values_into_environ(self):
        base = self.write_env("MCD_MCP_TOKEN='quoted-token'\n")

        config.load_local_env(base)

        self.assertEqual(os.environ["MCD_MCP_TOKEN"], "quoted-token", "应剥离引号后写入环境变量")

    def test_keeps_existing_env_by_default(self):
        os.environ["MCD_MCP_TOKEN"] = "from-shell"
        base = self.write_env("MCD_MCP_TOKEN=from-file\n")

        loaded = config.load_local_env(base)

        self.assertEqual(os.environ["MCD_MCP_TOKEN"], "from-shell", "默认不覆盖已有环境变量")
        self.assertIn("MCD_MCP_TOKEN", loaded, "键仍应出现在已加载集合中")

    def test_override_true_replaces_existing(self):
        os.environ["MCD_MCP_TOKEN"] = "from-shell"
        base = self.write_env("MCD_MCP_TOKEN=from-file\n")

        config.load_local_env(base, override=True)

        self.assertEqual(os.environ["MCD_MCP_TOKEN"], "from-file")

    def test_missing_file_returns_empty_set(self):
        loaded = config.load_local_env(self.tmp.name)

        self.assertEqual(loaded, set())

    def test_skips_comments_blank_and_invalid_lines(self):
        base = self.write_env("# 注释\n\nNOT_A_PAIR\nMCD_MCP_TOKEN=t\n")

        loaded = config.load_local_env(base)

        self.assertEqual(loaded, {"MCD_MCP_TOKEN"})


if __name__ == "__main__":
    unittest.main(verbosity=2)
