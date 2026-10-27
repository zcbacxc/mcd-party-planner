"""录制 / 回放传输层测试。

覆盖：回放键归一化、录制落盘与响应头裁剪、往返一致、同一工具多次调用按序回放、
缺调用时报错而不是编造数据、格式校验、以及与 `McpClient` 的集成（自动握手）。
"""

import json
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import replay  # noqa: E402
from src.mcp_client import McpClient, McpError  # noqa: E402


def _resp(status: int, body: str, headers=None):
    return status, body.encode("utf-8"), headers or {}


def _rpc_body(result):
    return json.dumps({"jsonrpc": "2.0", "id": 1, "result": result}, ensure_ascii=False)


class CallKeyTest(unittest.TestCase):
    def test_tools_call_includes_tool_name(self):
        key = replay.call_key({"method": "tools/call", "params": {"name": "query-meals"}})
        self.assertEqual(key, "tools/call:query-meals")

    def test_plain_method_uses_method_name(self):
        self.assertEqual(replay.call_key({"method": "initialize"}), "initialize")


class RecordingTransportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "snap.json")

    def test_records_calls_and_writes_file(self):
        def inner(endpoint, payload, headers, timeout):
            return _resp(200, _rpc_body({"ok": True}), {"Date": "now", "Mcp-Session-Id": "S1"})

        transport = replay.RecordingTransport(inner, self.path, endpoint="https://mcp.mcd.cn")
        transport("https://mcp.mcd.cn", {"method": "initialize"}, {}, 15.0)

        with open(self.path, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(data["format"], replay.REPLAY_FORMAT)
        self.assertEqual(data["callCount"], 1)
        self.assertEqual(data["calls"][0]["key"], "initialize")
        self.assertEqual(data["endpoint"], "https://mcp.mcd.cn")

    def test_filters_noisy_response_headers(self):
        def inner(endpoint, payload, headers, timeout):
            return _resp(200, "{}", {"Date": "now", "x-request-id": "abc",
                                     "Mcp-Session-Id": "S1", "Content-Type": "application/json"})

        transport = replay.RecordingTransport(inner, self.path)
        transport("e", {"method": "initialize"}, {}, 1.0)

        with open(self.path, encoding="utf-8") as handle:
            headers = json.load(handle)["calls"][0]["response"]["headers"]
        self.assertEqual(headers, {"Mcp-Session-Id": "S1", "Content-Type": "application/json"})


class ReplayTransportTest(unittest.TestCase):
    def _snapshot(self, calls):
        return {"format": replay.REPLAY_FORMAT, "recordedAt": "2026-10-27T14:00:00+08:00",
                "endpoint": "https://mcp.mcd.cn", "note": "test", "calls": calls}

    def test_roundtrip_is_lossless(self):
        body = _rpc_body({"structuredContent": {"price": 3180}})

        def inner(endpoint, payload, headers, timeout):
            return _resp(200, body, {"Mcp-Session-Id": "S1"})

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "snap.json")
            recorder = replay.RecordingTransport(inner, path)
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                       "params": {"name": "calculate-price"}}
            status_a, body_a, headers_a = recorder("e", payload, {}, 1.0)

            player = replay.ReplayTransport.from_file(path)
            status_b, body_b, headers_b = player("e", payload, {}, 1.0)

        self.assertEqual((status_a, body_a, headers_a), (status_b, body_b, headers_b))

    def test_same_tool_replays_in_recorded_order(self):
        calls = [
            {"key": "tools/call:query-meal-detail",
             "response": {"status": 200, "body": _rpc_body({"n": 1}), "headers": {}}},
            {"key": "tools/call:query-meal-detail",
             "response": {"status": 200, "body": _rpc_body({"n": 2}), "headers": {}}},
        ]
        player = replay.ReplayTransport(self._snapshot(calls))
        payload = {"method": "tools/call", "params": {"name": "query-meal-detail"}}

        first = json.loads(player("e", payload, {}, 1.0)[1])["result"]["n"]
        second = json.loads(player("e", payload, {}, 1.0)[1])["result"]["n"]

        self.assertEqual((first, second), (1, 2))
        self.assertEqual(player.remaining(), 0)

    def test_missing_call_raises_instead_of_faking_data(self):
        player = replay.ReplayTransport(self._snapshot([]))
        with self.assertRaises(McpError) as ctx:
            player("e", {"method": "tools/call", "params": {"name": "query-meals"}}, {}, 1.0)
        self.assertIn("query-meals", str(ctx.exception))

    def test_rejects_unknown_format(self):
        with self.assertRaises(ValueError):
            replay.ReplayTransport({"format": "something-else", "calls": []})

    def test_missing_file_surfaces_os_error(self):
        with self.assertRaises(OSError):
            replay.ReplayTransport.from_file("examples/not-exists.json")


class ClientWithReplayTest(unittest.TestCase):
    """回放传输需要自动握手，否则拿不到协议版本、录制里的 initialize 也会浪费。"""

    def test_client_auto_initializes_with_replay_transport(self):
        calls = [
            {"key": "initialize",
             "response": {"status": 200, "headers": {"Mcp-Session-Id": "S1"},
                          "body": _rpc_body({"protocolVersion": "2025-06-18"})}},
            {"key": "notifications/initialized",
             "response": {"status": 202, "headers": {}, "body": ""}},
            {"key": "tools/call:calculate-price",
             "response": {"status": 200, "headers": {},
                          "body": _rpc_body({"structuredContent": {"price": 3180}})}},
        ]
        player = replay.ReplayTransport(
            {"format": replay.REPLAY_FORMAT, "calls": calls})

        client = McpClient(token="", transport=player)
        result = client.call_tool("calculate-price", {"storeCode": "1"})

        self.assertEqual(result, {"price": 3180})
        self.assertEqual(client.protocol_version, "2025-06-18")
        self.assertEqual(player.remaining(), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
