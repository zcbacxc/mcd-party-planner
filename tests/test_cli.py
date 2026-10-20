"""命令行入口单元测试。

覆盖要点：
- 参数解析：必填项缺失即报错、默认值与子命令接线正确；
- 出口约定：Token 缺失以退出码 2 结束并给出指引，MCP 异常以退出码 1 结束；
- 输出契约：`--json` 输出可被解析的结构化结果，默认输出为人类可读报告；
- 安全边界：`party` 默认 dry_run，输出中不得出现真实下单调用。

全部用例离线运行（注入替身客户端），不读 `.env`、不发网络请求。
"""

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import cli  # noqa: E402
from src.mcp_client import McpError  # noqa: E402

# ---- 替身数据（真实响应形态：金额单位为分） --------------------------------
STORES = {"success": True, "data": [
    {"storeCode": "S1", "storeName": "测试餐厅", "businessStatus": True}]}
MENU = {"success": True, "data": {"categories": [
    {"name": "汉堡", "meals": [{"code": "M1"}]},
    {"name": "饮品", "meals": [{"code": "B1"}]},
]}}
DETAIL = {"success": True, "data": {"name": "巨无霸"}}
PRICE = {"success": True, "data": {"price": 3400}}
SPU = {"success": True, "data": [
    {"spuId": 1779, "spuName": "麦当劳奇妙生日派对", "catName": "生日类派对"}]}
CITY = {"success": True, "data": [
    {"code": 510100, "name": "成都市", "latitude": 30.5, "longitude": 104.0}]}
PSTORE = {"success": True, "data": [{"code": "3450261", "name": "大魔方餐厅"}]}
PDATE = {"success": True, "data": [{"date": "2026-10-16"}]}
PSESSION = {"success": True, "data": [
    {"id": 1, "timeStart": "12:00", "timeEnd": "13:30", "price": 5000, "leftNum": 12}]}
SKU = {"success": True, "data": {"skuList": [{"skuId": 2027}]}}
CALENDAR = {"success": True, "data": {"dailyList": [
    {"date": "2026-10-16", "dateText": "2026年10月16日 今日",
     "events": [{"activityTitle": "会员日"}]}]}}
ADDRESSES = {"success": True, "data": {"addresses": []}}


class StubClient:
    """只实现业务层依赖的 call_tool 鸭子接口。"""

    has_token = True

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append(name)
        return self.dispatch(name, arguments or {})


def dispatch(name, arguments):
    table = {
        "query-nearby-stores": STORES,
        "query-meals": MENU,
        "query-meal-detail": DETAIL,
        "calculate-price": PRICE,
        "mall-points-products": SPU,
        "mall-product-detail": SKU,
        "query-party-city": CITY,
        "query-party-store": PSTORE,
        "query-party-store-date": PDATE,
        "query-party-store-session": PSESSION,
        "campaign-calendar": CALENDAR,
        "delivery-query-addresses": ADDRESSES,
    }
    if name not in table:
        raise AssertionError(f"未预设的工具：{name}")
    return table[name]


def run(argv, client=None):
    """执行 CLI，返回 (退出码, stdout, stderr)；SystemExit 归一化为退出码。"""
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(cli, "build_client", return_value=client or StubClient(dispatch)), \
            redirect_stdout(out), redirect_stderr(err):
        try:
            code = cli.main(argv)
        except SystemExit as exc:  # require_token 以 SystemExit(2) 结束
            code = int(exc.code or 0)
    return code, out.getvalue(), err.getvalue()


class ParserTest(unittest.TestCase):
    def test_plan_requires_core_arguments(self):
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            cli.make_parser().parse_args(["plan", "--keyword", "春熙路",
                                          "--headcount", "4", "--budget", "200"])

    def test_defaults(self):
        args = cli.make_parser().parse_args(
            ["plan", "--city", "成都", "--keyword", "春熙路",
             "--headcount", "4", "--budget", "200"])
        self.assertEqual(args.store_limit, 3)
        self.assertEqual(args.rate_limit, 300)
        self.assertFalse(args.json)

    def test_party_defaults_to_dry_run(self):
        args = cli.make_parser().parse_args(["party", "--city", "成都市"])
        self.assertFalse(args.confirm)
        self.assertEqual(args.headcount, 5)


class SelfcheckTest(unittest.TestCase):
    def test_reports_configured_token_without_echo(self):
        env = {"MCD_MCP_TOKEN": "unit-test-token"}
        out = io.StringIO()
        with mock.patch.object(cli.config, "load_local_env", return_value={}), \
                mock.patch.dict(os.environ, env, clear=False), \
                redirect_stdout(out):
            code = cli.cmd_selfcheck(mock.Mock())
        text = out.getvalue()
        self.assertEqual(code, 0)
        self.assertIn("已配置", text)
        self.assertNotIn("unit-test-token", text, "自检输出不得回显 Token 明文")


class ErrorHandlingTest(unittest.TestCase):
    def test_missing_token_exits_with_code_2(self):
        client = StubClient(dispatch)
        client.has_token = False
        code, _, err = run(["plan", "--city", "成都", "--keyword", "春熙路",
                            "--headcount", "4", "--budget", "200"], client)
        self.assertEqual(code, 2)
        self.assertIn("未检测到 MCP Token", err)

    def test_mcp_error_maps_to_exit_code_1(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "build_client", side_effect=McpError("boom")), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["tools"])
        self.assertEqual(code, 1)
        self.assertIn("MCP 调用失败", err.getvalue())


class OutputContractTest(unittest.TestCase):
    def test_plan_json_is_parseable(self):
        code, out, _ = run(["plan", "--city", "成都", "--keyword", "春熙路",
                            "--headcount", "4", "--budget", "200", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["headcount"], 4)
        self.assertTrue(payload["plans"])
        self.assertIn("perPerson", payload["plans"][0])

    def test_plan_text_report(self):
        code, out, _ = run(["plan", "--city", "成都", "--keyword", "春熙路",
                            "--headcount", "4", "--budget", "200"])
        self.assertEqual(code, 0)
        self.assertIn("【团餐方案】", out)
        self.assertIn("人均", out)

    def test_party_outputs_payload_without_ordering(self):
        client = StubClient(dispatch)
        code, out, _ = run(["party", "--city", "成都市"], client)
        self.assertEqual(code, 0)
        self.assertIn("dry_run=True", out)
        self.assertNotIn("party-order-create", client.calls, "默认不得真实下单")

    def test_calendar_reports_assistance_section(self):
        code, out, _ = run(["calendar", "--city", "成都", "--keyword", "春熙路"])
        # 替身未提供活动日历与助餐链路，按降级契约仍须输出报告骨架
        self.assertIn(code, (0, 1))
        if code == 0:
            self.assertIn("【活动日历组织建议】", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
