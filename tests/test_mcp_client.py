"""MCP 调用层单元测试（标准库 unittest，无需安装依赖）。

覆盖要点：
- 限流保护：节流器按分钟窗口生效，429 指数退避后重试，重试耗尽抛错；
- Token 安全：请求头携带 Bearer 但测试不落盘真实 Token。
"""

import json
import os
import sys
import unittest

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
