"""录制 / 回放传输层：让没有 Token 的人也能复现一次真实运行。

设计要点：

- 与 `_http_transport` **完全同签名**（`endpoint, payload, headers, timeout` →
  `status, body, headers`），因此可直接注入 `McpClient(transport=...)`，调用层零改动；
- 录制按 `method`（`tools/call` 追加工具名）归档，同一工具被多次调用时**按录制顺序依次回放**；
- 回放**不校验入参**：同一份快照可以配不同人数 / 预算反复查看报告形态，
  但缺少对应调用时立即报错，绝不静默编造数据；
- 录制文件是可读 JSON（响应体按 UTF-8 文本存储），便于人工核对与 diff。
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .mcp_client import McpError

# 文件格式标识：将来结构变更时靠它给出明确错误，而不是解析到一半才炸
REPLAY_FORMAT = "mcd-party-planner/replay@1"

Transport = Callable[[str, Dict[str, Any], Dict[str, str], float],
                     Tuple[int, bytes, Dict[str, str]]]

# 只保留回放真正用得到的响应头，其余（Date / x-request-id / WAF 追踪号等）不入快照：
# 既让文件更小更易 diff，也避免把服务端的请求追踪标识固化进仓库。
KEPT_RESPONSE_HEADERS = ("mcp-session-id", "content-type")


def _filter_headers(headers: Dict[str, str]) -> Dict[str, str]:
    return {key: value for key, value in (headers or {}).items()
            if key.lower() in KEPT_RESPONSE_HEADERS}


def call_key(payload: Dict[str, Any]) -> str:
    """把一次 JSON-RPC 请求归一化为回放键：`tools/call` 带上工具名，其余按方法名。"""
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    name = params.get("name") if isinstance(params, dict) else None
    return f"{method}:{name}" if name else method


class RecordingTransport:
    """包装真实传输，把每次请求 / 响应写入快照文件。

    `auto_initialize = True` 让 `McpClient` 在自定义传输下仍会先握手
    （否则录制里会缺少 `initialize`，回放时协议版本为空）。
    """

    auto_initialize = True

    def __init__(self, inner: Transport, path: str,
                 endpoint: str = "", note: str = "录制快照，非实时数据") -> None:
        self._inner = inner
        self.path = path
        self.endpoint = endpoint
        self.note = note
        self.calls: List[Dict[str, Any]] = []
        self._recorded_at = _dt.datetime.now().astimezone().isoformat(timespec="seconds")

    def __call__(self, endpoint: str, payload: Dict[str, Any], headers: Dict[str, str],
                 timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
        status, raw, resp_headers = self._inner(endpoint, payload, headers, timeout)
        self.calls.append({
            "key": call_key(payload),
            "request": payload,
            "response": {
                "status": status,
                "headers": _filter_headers(resp_headers),
                "body": raw.decode("utf-8", errors="replace") if raw else "",
            },
        })
        self.save()
        return status, raw, resp_headers

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        payload = {
            "format": REPLAY_FORMAT,
            "recordedAt": self._recorded_at,
            "endpoint": self.endpoint,
            "note": self.note,
            "callCount": len(self.calls),
            "calls": self.calls,
        }
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(tmp, self.path)


class ReplayTransport:
    """从快照文件读取响应，替代真实网络传输。"""

    auto_initialize = True

    def __init__(self, data: Dict[str, Any], path: str = "<memory>") -> None:
        fmt = data.get("format")
        if fmt != REPLAY_FORMAT:
            raise ValueError(
                f"回放文件格式不支持：{fmt!r}（期望 {REPLAY_FORMAT}）：{path}"
            )
        self.path = path
        self.endpoint = data.get("endpoint") or ""
        self.recorded_at = data.get("recordedAt") or ""
        self.note = data.get("note") or ""
        self._queues: Dict[str, List[Dict[str, Any]]] = {}
        for call in data.get("calls") or []:
            self._queues.setdefault(str(call.get("key") or ""), []).append(call)
        self.consumed = 0

    @classmethod
    def from_file(cls, path: str) -> "ReplayTransport":
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return cls(data, path=path)

    def __call__(self, endpoint: str, payload: Dict[str, Any], headers: Dict[str, str],
                 timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
        key = call_key(payload)
        queue = self._queues.get(key) or []
        if not queue:
            raise McpError(
                f"回放数据缺少 {key} 的响应（{self.path}）。"
                "该快照可能录制于旧版本，请重新录制或换一份快照"
            )
        call = queue.pop(0)
        self.consumed += 1
        response = call.get("response") or {}
        body = str(response.get("body") or "")
        return (int(response.get("status") or 200),
                body.encode("utf-8"),
                dict(response.get("headers") or {}))

    def remaining(self) -> int:
        """尚未被消费的录制条数，供自检输出。"""
        return sum(len(queue) for queue in self._queues.values())
