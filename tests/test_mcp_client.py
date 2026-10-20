"""MCP 调用层单元测试（标准库 unittest，无需安装依赖）。

覆盖要点：
- 限流保护：节流器按分钟窗口生效，429 指数退避后重试，重试耗尽抛错；
- Token 安全：请求头携带 Bearer 但测试不落盘真实 Token。
"""

import json
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import mcp_client as mc  # noqa: E402


class FakeTransport:
    """按脚本返回响应的假传输层，记录每次请求。"""

    def __init__(self, script):
        self.script = list(script)
        self.requests = []

    def __call__(self, endpoint, payload, headers, timeout):
        self.requests.append({"payload": payload, "headers": headers})
        if not self.script:
            # 通知类消息（无 id）服务端返回 202，不占用响应脚本
            if "id" not in payload:
                return 202, b"", {}
            raise AssertionError("响应脚本已耗尽")
        status, body, resp_headers = self.script.pop(0)
        if callable(body):
            body = body(payload)
        raw = body.encode("utf-8") if isinstance(body, str) else json.dumps(body).encode("utf-8")
        return status, raw, resp_headers or {}


def rpc_result(result):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result})


class RateLimiterTest(unittest.TestCase):
    def test_blocks_after_limit_and_recovers(self):
        now = [0.0]
        slept = []
        limiter = mc.RateLimiter(2, clock=lambda: now[0])
        self.assertEqual(limiter.acquire(lambda s: slept.append(s)), 0.0)
        self.assertEqual(limiter.acquire(lambda s: slept.append(s)), 0.0)
        limiter.acquire(lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
        self.assertTrue(slept, "超出限额后应阻塞等待")
        self.assertGreaterEqual(slept[-1], 59.0)

    def test_rejects_above_official_limit(self):
        with self.assertRaises(ValueError):
            mc.RateLimiter(mc.OFFICIAL_RATE_LIMIT_PER_MINUTE + 1)


class ArgumentsTest(unittest.TestCase):
    def test_filters_unknown_keys(self):
        self.assertEqual(
            mc.build_arguments("query-meals", {"storeCode": "S001", "junk": 1}),
            {"storeCode": "S001"},
        )

    def test_drops_none_values(self):
        self.assertEqual(mc.build_arguments("campaign-calendar", {"specifiedDate": None}), {})

    def test_unknown_tool_raises(self):
        with self.assertRaises(mc.McpError):
            mc.build_arguments("not-a-tool", {})


class ParseTest(unittest.TestCase):
    def test_structured_content_wins(self):
        result = {"structuredContent": {"a": 1}, "content": [{"type": "text", "text": "{\"b\":2}"}]}
        self.assertEqual(mc._parse_tool_result("t", result), {"a": 1})

    def test_text_json_parsed(self):
        result = {"content": [{"type": "text", "text": "{\"total\": 12.5}"}]}
        self.assertEqual(mc._parse_tool_result("t", result), {"total": 12.5})

    def test_plain_text_returned(self):
        result = {"content": [{"type": "text", "text": "查询成功"}]}
        self.assertEqual(mc._parse_tool_result("t", result), "查询成功")

    def test_is_error_raises(self):
        result = {"isError": True, "content": [{"type": "text", "text": "门店不存在"}]}
        with self.assertRaises(mc.McpToolError):
            mc._parse_tool_result("query-meals", result)

    def test_sse_payload(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        self.assertEqual(mc._parse_message(body), {"jsonrpc": "2.0", "id": 1, "result": {"ok": True}})


class ClientTest(unittest.TestCase):
    def test_auth_header_and_session_id(self):
        transport = FakeTransport([
            (200, rpc_result({"tools": [{"name": "query-meals"}]}),
             {"Mcp-Session-Id": "sess-1"}),
        ])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)
        tools = client.list_tools()
        self.assertEqual(tools[0]["name"], "query-meals")
        self.assertEqual(transport.requests[0]["headers"]["Authorization"], "Bearer T")
        self.assertEqual(client._session_id, "sess-1")

    def test_429_retry_then_success(self):
        slept = []
        transport = FakeTransport([
            (429, "rate limited", {}),
            (200, rpc_result({"ok": True}), {}),
        ])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: slept.append(s))
        self.assertTrue(client.initialize())
        attempts = [r for r in transport.requests if r["payload"].get("method") == "initialize"]
        self.assertEqual(len(attempts), 2, "首次 429 后应重试一次 initialize")
        self.assertGreater(len(slept), 0)

    def test_429_exhausted_raises(self):
        transport = FakeTransport([(429, "rate limited", {}) for _ in range(10)])
        client = mc.McpClient(token="T", transport=transport, max_retries=2,
                              sleep_fn=lambda s: None)
        with self.assertRaises(mc.McpRateLimitError):
            client.initialize()
        self.assertEqual(len(transport.requests), 3)

    def test_401_raises_auth_error(self):
        transport = FakeTransport([(401, "unauthorized", {})])
        client = mc.McpClient(token="bad", transport=transport, sleep_fn=lambda s: None)
        with self.assertRaises(mc.McpAuthError):
            client.initialize()

    def test_initialize_reports_package_version(self):
        """clientInfo.version 必须取自包版本，避免与 __version__ 双份维护。"""
        transport = FakeTransport([(200, rpc_result({"ok": True}), {})])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)
        client.initialize()
        init_payload = transport.requests[0]["payload"]
        self.assertEqual(init_payload["params"]["clientInfo"]["version"], mc.__version__)

    def test_call_tool_sends_tool_call(self):
        transport = FakeTransport([
            (200, rpc_result({"structuredContent": {"total": 88.0}}), {}),
        ])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)
        out = client.call_tool("calculate-price", {"storeCode": "S1", "items": [], "junk": 1})
        self.assertEqual(out, {"total": 88.0})
        params = transport.requests[0]["payload"]["params"]
        self.assertEqual(params["name"], "calculate-price")
        self.assertEqual(params["arguments"], {"storeCode": "S1", "items": []})


class RetryPolicyTest(unittest.TestCase):
    """写操作与读操作的重试策略必须不同：写操作遇 5xx 不得重试（防重复下单）。"""

    ORDER_ARGS = {"partyType": 1, "spuId": 1779, "skuId": 2027, "storeCode": "S1"}

    @staticmethod
    def _client(transport):
        return mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)

    def test_read_tool_retries_on_5xx(self):
        transport = FakeTransport([
            (500, "boom", {}),
            (200, rpc_result({"structuredContent": {"ok": True}}), {}),
        ])
        out = self._client(transport).call_tool("query-meals", {"storeCode": "S1"})
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(transport.requests), 2, "读操作 5xx 应重试")

    def test_write_tool_does_not_retry_on_5xx(self):
        transport = FakeTransport([
            (500, "boom", {}),
            (200, rpc_result({"structuredContent": {"orderId": "1"}}), {}),
        ])
        with self.assertRaises(mc.McpError) as ctx:
            self._client(transport).call_tool("party-order-create", self.ORDER_ARGS)
        self.assertEqual(len(transport.requests), 1,
                         "写操作遇 5xx 不得重发，否则服务端可能已建单导致重复订单")
        self.assertIn("未自动重试", str(ctx.exception))

    def test_write_tool_still_retries_on_429(self):
        """429 表示请求被限流拒绝、未被受理，重试不会产生重复副作用。"""
        transport = FakeTransport([
            (429, "rate limited", {}),
            (200, rpc_result({"structuredContent": {"orderId": "1"}}), {}),
        ])
        out = self._client(transport).call_tool("party-order-create", self.ORDER_ARGS)
        self.assertEqual(out, {"orderId": "1"})
        self.assertEqual(len(transport.requests), 2)

    def test_explicit_idempotent_overrides_default(self):
        transport = FakeTransport([
            (500, "boom", {}),
            (200, rpc_result({"structuredContent": {"ok": True}}), {}),
        ])
        out = self._client(transport).call_tool("party-order-create", self.ORDER_ARGS,
                                                idempotent=True)
        self.assertEqual(out, {"ok": True})
        self.assertEqual(len(transport.requests), 2)

    def test_write_tools_cover_all_side_effect_tools(self):
        """防回归：新增有副作用的工具时必须登记到 WRITE_TOOLS。"""
        for name in ("party-order-create", "create-order", "mall-create-order",
                     "delivery-create-address", "cancel-order", "auto-bind-coupons",
                     "draw-lottery"):
            self.assertIn(name, mc.WRITE_TOOLS)
        # 读工具不应被误登记
        for name in ("query-meals", "calculate-price", "query-nearby-stores"):
            self.assertNotIn(name, mc.WRITE_TOOLS)


class NetworkErrorTest(unittest.TestCase):
    """网络层异常必须归一化为 McpError，不得把 urllib 堆栈抛给用户。"""

    def test_url_error_becomes_network_error(self):
        with mock.patch("urllib.request.urlopen",
                        side_effect=urllib.error.URLError(
                            "Tunnel connection failed: 502 Bad Gateway")):
            with self.assertRaises(mc.McpNetworkError) as ctx:
                mc._http_transport("https://mcp.mcd.cn", {"jsonrpc": "2.0"}, {}, 1.0)
        message = str(ctx.exception)
        self.assertIn("网络不可达", message)
        self.assertIn("502", message)

    def test_timeout_becomes_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
            with self.assertRaises(mc.McpNetworkError):
                mc._http_transport("https://mcp.mcd.cn", {"jsonrpc": "2.0"}, {}, 1.0)

    def test_connection_reset_becomes_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=ConnectionResetError("reset")):
            with self.assertRaises(mc.McpNetworkError):
                mc._http_transport("https://mcp.mcd.cn", {"jsonrpc": "2.0"}, {}, 1.0)

    def test_http_error_still_returns_status(self):
        """HTTPError 是 URLError 子类，不能被网络异常分支吞掉。"""
        error = urllib.error.HTTPError("https://mcp.mcd.cn", 429, "too many", {}, None)
        error.read = lambda: b"rate limited"
        with mock.patch("urllib.request.urlopen", side_effect=error):
            status, body, _ = mc._http_transport("https://mcp.mcd.cn", {"jsonrpc": "2.0"}, {}, 1.0)
        self.assertEqual(status, 429)
        self.assertEqual(body, b"rate limited")


class NormalizeTest(unittest.TestCase):
    def test_as_list_variants(self):
        self.assertEqual(mc.as_list({"stores": [{"a": 1}]}), [{"a": 1}])
        self.assertEqual(mc.as_list([{"a": 1}, "x"]), [{"a": 1}])
        self.assertEqual(mc.as_list(None), [])

    def test_to_float_currency(self):
        self.assertEqual(mc.to_float("￥12.5元"), 12.5)
        self.assertEqual(mc.to_float(8), 8.0)
        self.assertEqual(mc.to_float("无"), 0.0)

    def test_money_of_nested(self):
        self.assertEqual(mc.money_of({"data": {"payAmount": "36.00"}}), 36.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
