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


class ProtocolVersionTest(unittest.TestCase):
    """协议版本协商：声明版本、校验服务端返回值、握手后携带协议头。"""

    @staticmethod
    def _client(transport):
        return mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)

    def test_initialize_stores_negotiated_version(self):
        transport = FakeTransport([
            (200, rpc_result({"protocolVersion": mc.PROTOCOL_VERSION, "capabilities": {}}), {}),
        ])
        client = self._client(transport)
        self.assertTrue(client.initialize())
        self.assertEqual(client.protocol_version, mc.PROTOCOL_VERSION)
        self.assertEqual(transport.requests[0]["payload"]["params"]["protocolVersion"],
                         mc.PROTOCOL_VERSION)

    def test_initialize_rejects_unsupported_version(self):
        """服务端返回客户端不支持的版本时中止，避免按错误语义继续调用。"""
        transport = FakeTransport([(200, rpc_result({"protocolVersion": "2024-11-05"}), {})])
        client = self._client(transport)
        with self.assertRaises(mc.McpError) as ctx:
            client.initialize()
        self.assertIn("协议版本不兼容", str(ctx.exception))
        self.assertIn("2024-11-05", str(ctx.exception))
        self.assertFalse(client.initialized)

    def test_protocol_header_sent_after_handshake(self):
        transport = FakeTransport([
            (200, rpc_result({"protocolVersion": mc.PROTOCOL_VERSION}), {}),
            (202, "", {}),                                   # notifications/initialized
            (200, rpc_result({"structuredContent": {"ok": True}}), {}),
        ])
        client = self._client(transport)
        client.initialize()
        client.call_tool("query-meals", {"storeCode": "S1"})
        self.assertNotIn("MCP-Protocol-Version", transport.requests[0]["headers"])
        self.assertEqual(transport.requests[-1]["headers"]["MCP-Protocol-Version"],
                         mc.PROTOCOL_VERSION)

    def test_no_protocol_header_before_handshake(self):
        client = self._client(FakeTransport([]))
        self.assertNotIn("MCP-Protocol-Version", client._headers())


class ToolsPaginationTest(unittest.TestCase):
    """tools/list 分页：跟随 nextCursor，且不超过页数上限。"""

    @staticmethod
    def _client(transport):
        return mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)

    def test_follows_next_cursor(self):
        transport = FakeTransport([
            (200, rpc_result({"tools": [{"name": "a"}], "nextCursor": "c1"}), {}),
            (200, rpc_result({"tools": [{"name": "b"}]}), {}),
        ])
        tools = self._client(transport).list_tools()
        self.assertEqual([t["name"] for t in tools], ["a", "b"])
        self.assertEqual(transport.requests[1]["payload"]["params"], {"cursor": "c1"})

    def test_stops_at_page_limit(self):
        """服务端若持续返回 cursor，客户端不得无限翻页。"""
        script = [(200, rpc_result({"tools": [{"name": f"t{i}"}], "nextCursor": "again"}), {})
                  for i in range(mc.MAX_TOOL_PAGES + 3)]
        transport = FakeTransport(script)
        tools = self._client(transport).list_tools()
        self.assertEqual(len(tools), mc.MAX_TOOL_PAGES)
        self.assertEqual(len(transport.requests), mc.MAX_TOOL_PAGES)


class ProbeMetadataTest(unittest.TestCase):
    """probe 必须保留 outputSchema / annotations / title，否则校准产物不完整。"""

    def test_probe_keeps_full_tool_metadata(self):
        tool = {"name": "t", "title": "T", "description": "d" * 300,
                "inputSchema": {"type": "object"},
                "outputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True}}
        transport = FakeTransport([(200, rpc_result({"tools": [tool]}), {})])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)
        snapshot = client.probe()
        item = snapshot["tools"][0]
        self.assertEqual(item["outputSchema"], {"type": "object", "properties": {}})
        self.assertEqual(item["annotations"], {"readOnlyHint": True})
        self.assertEqual(item["title"], "T")
        self.assertEqual(len(item["description"]), 200, "描述仍截断到 200 字符")
        self.assertEqual(snapshot["toolCount"], 1)


class CloseTest(unittest.TestCase):
    """会话终止：默认传输发 DELETE，自定义传输只清理本地状态。"""

    def test_close_sends_delete_with_session_header(self):
        client = mc.McpClient(token="T")
        client._session_id = "sess-1"
        client.protocol_version = mc.PROTOCOL_VERSION
        with mock.patch.object(mc, "_http_delete", return_value=204) as delete:
            self.assertTrue(client.close())
        headers = delete.call_args[0][1]
        self.assertEqual(headers["Mcp-Session-Id"], "sess-1")
        self.assertNotIn("Content-Type", headers, "DELETE 不应带 JSON Content-Type")
        self.assertIsNone(client._session_id)
        self.assertFalse(client.initialized)

    def test_close_without_session_is_noop(self):
        client = mc.McpClient(token="T")
        with mock.patch.object(mc, "_http_delete") as delete:
            self.assertFalse(client.close())
            delete.assert_not_called()

    def test_close_ignores_transport_failure(self):
        client = mc.McpClient(token="T")
        client._session_id = "sess-1"
        with mock.patch.object(mc, "_http_delete", return_value=0):
            self.assertFalse(client.close())
        self.assertIsNone(client._session_id)


class StatsTest(unittest.TestCase):
    """调用统计：次数 / 重试 / 限流等待，供 --verbose 使用。"""

    def test_counts_calls_and_retries(self):
        transport = FakeTransport([
            (429, "rate limited", {}),
            (200, rpc_result({"tools": []}), {}),
        ])
        client = mc.McpClient(token="T", transport=transport, sleep_fn=lambda s: None)
        client.list_tools()
        self.assertEqual(client.stats["calls"], 2)
        self.assertEqual(client.stats["retries"], 1)
        self.assertGreaterEqual(client.stats["elapsedMs"], 0)

    def test_counts_rate_limit_waits(self):
        now = [0.0]
        slept = []

        def sleep_fn(seconds):
            slept.append(seconds)
            now[0] += seconds

        transport = FakeTransport([(200, rpc_result({"tools": []}), {}),
                                   (200, rpc_result({"tools": []}), {})])
        client = mc.McpClient(token="T", transport=transport, rate_limit_per_minute=1,
                              sleep_fn=sleep_fn, clock=lambda: now[0])
        client.list_tools()   # 第 1 次不等待
        client.list_tools()   # 第 2 次触发滑动窗口等待
        self.assertEqual(client.stats["rateLimitWaits"], 1)
        self.assertTrue(slept)


class BackoffJitterTest(unittest.TestCase):
    """退避必须带抖动，且等待时长不超过指数上界。"""

    def test_sleep_within_jitter_bounds(self):
        slept = []
        transport = FakeTransport([(429, "rate limited", {}) for _ in range(3)]
                                  + [(200, rpc_result({"tools": []}), {})])
        client = mc.McpClient(token="T", transport=transport, max_retries=3,
                              sleep_fn=slept.append)
        client.list_tools()
        self.assertEqual(len(slept), 3)
        for attempt, seconds in enumerate(slept):
            self.assertGreaterEqual(seconds, 0.0)
            self.assertLessEqual(seconds, mc.BACKOFF_BASE * (2 ** attempt))

    def test_backoff_uses_random_uniform(self):
        with mock.patch.object(mc.random, "uniform", return_value=0.0) as uniform:
            transport = FakeTransport([(429, "x", {}),
                                       (200, rpc_result({"tools": []}), {})])
            client = mc.McpClient(token="T", transport=transport,
                                  sleep_fn=lambda s: None)
            client.list_tools()
        uniform.assert_called()
        self.assertEqual(uniform.call_args[0][0], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
