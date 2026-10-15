"""麦当劳 MCP 调用层。

设计要点：
- 经 streamablehttp + Bearer Token 访问 `https://mcp.mcd.cn`（官方唯一接入方式）；
- 官方硬限 600 次/分钟，客户端内置节流器默认 300 次/分钟（留 50% 余量）
  + 429 指数退避；
- Token 只从环境变量或本地 `.env` 读取，绝不写入仓库、绝不打印。

本模块仅依赖标准库，便于零安装运行。
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"

# 官方硬限：每个 Token 每分钟最多 600 次请求（超出返回 429）。
OFFICIAL_RATE_LIMIT_PER_MINUTE = 600
# 我方默认：取官方上限的 50%，预留突发余量。
DEFAULT_RATE_LIMIT_PER_MINUTE = 300

DEFAULT_TIMEOUT = 15.0
DEFAULT_MAX_RETRIES = 3

# ---------------------------------------------------------------------------
# 工具入参规范（单一校准点）
#
# 已按 `python -m src.cli probe` 抓取的**真实 inputSchema** 校准（服务端共 35 个工具）。
# 注意：README 描述与真实 schema 存在差异，例如主题活动相关工具实际名为
# `query-party-store-date` / `query-party-store-session`（非 query-partystore-*），
# `query-nearby-stores` 无 address 参数而用 beType + searchType + city/keyword。
# ---------------------------------------------------------------------------
TOOL_ARG_SPEC: Dict[str, Tuple[str, ...]] = {
    # 门店与菜单
    "query-nearby-stores": ("beType", "searchType", "city", "keyword"),
    "delivery-query-stores": ("addressId", "beType"),
    "query-meals": ("storeCode", "orderType", "beType", "beCode", "reservationDate"),
    "query-meal-detail": ("storeCode", "orderType", "beType", "beCode", "code", "reservationDate"),
    "calculate-price": ("storeCode", "orderType", "beType", "beCode", "items",
                        "gmServiceCode", "needTableware", "reservationDate", "withOrder"),
    "query-meal-assistance": ("storeCode", "orderType", "beType", "beCode", "reservationDate"),
    "query-store-coupons": ("storeCode", "orderType", "beType", "beCode", "reservationDate"),
    "query-promotions": ("storeCode", "orderType", "beType", "beCode", "reservationDate"),
    # 活动日历
    "campaign-calendar": ("specifiedDate",),
    # 主题活动（派对 / 品鉴会）
    "mall-points-products": ("catRuleIds",),
    "mall-product-detail": ("spuId",),
    "query-party-city": ("spuId",),
    "query-party-store": ("code", "latitude", "longitude", "spuId"),
    "query-party-store-date": ("spuId", "storeCode"),
    "query-party-store-session": ("spuId", "storeCode", "dateStr"),
    "party-order-create": ("partyType", "spuId", "skuId", "storeCode", "code", "dateStr",
                           "id", "leftNum", "timeStart", "timeEnd", "count", "partyTimeInfo"),
    # 其他（登记备用）
    "now-time-info": (),
    "list-nutrition-foods": (),
    "available-coupons": (),
    "auto-bind-coupons": (),
    "query-my-coupons": ("page", "pageSize"),
    "query-my-account": (),
    "query-order": ("orderId",),
    "order-list": (),
    "cancel-order": ("orderId", "cancelReasonCode"),
    "delivery-query-addresses": (),
    "delivery-create-address": ("address", "addressDetail", "city", "contactName", "gender", "phone"),
    "mall-order-list": ("lastId", "size"),
    "mall-order-detail": ("orderId",),
    "mall-create-order": ("addressId", "count", "skuId", "spuCategory"),
    "query-lottery-info": (),
    "draw-lottery": (),
    "query-my-prizes": ("pageNum", "pageSize"),
    "query-survey-coupon": ("orderId",),
    "create-order": ("storeCode", "orderType", "beType", "beCode", "items", "addressId",
                     "takeWayCode", "needTableware", "remark", "reservationDate",
                     "gmServiceCode", "withOrder"),
}


class McpError(RuntimeError):
    """MCP 调用失败的基类。"""


class McpAuthError(McpError):
    """401：Token 无效、过期或未配置。"""


class McpRateLimitError(McpError):
    """429：重试后仍触发限流。"""


class McpToolError(McpError):
    """工具返回 isError=True。"""


class RateLimiter:
    """滑动窗口节流器：限制每分钟内发出的请求次数。"""

    def __init__(self, max_calls_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if max_calls_per_minute <= 0:
            raise ValueError("max_calls_per_minute 必须为正整数")
        if max_calls_per_minute > OFFICIAL_RATE_LIMIT_PER_MINUTE:
            raise ValueError(
                f"不允许超过官方上限 {OFFICIAL_RATE_LIMIT_PER_MINUTE} 次/分钟"
            )
        self.max_calls = max_calls_per_minute
        self._clock = clock
        self._timestamps: "deque[float]" = deque()

    def acquire(self, sleep_fn: Callable[[float], None] = time.sleep) -> float:
        """阻塞至可发送请求，返回实际等待秒数。"""
        waited = 0.0
        while True:
            now = self._clock()
            while self._timestamps and now - self._timestamps[0] >= 60.0:
                self._timestamps.popleft()
            if len(self._timestamps) < self.max_calls:
                self._timestamps.append(now)
                return waited
            sleep_for = 60.0 - (now - self._timestamps[0]) + 0.001
            sleep_fn(sleep_for)
            waited += sleep_for


def build_arguments(tool: str, values: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """按 TOOL_ARG_SPEC 构造工具入参，丢弃未声明的键（避免向服务端发送无效字段）。"""
    if tool not in TOOL_ARG_SPEC:
        raise McpError(f"未登记的工具名：{tool}")
    spec = TOOL_ARG_SPEC[tool]
    values = values or {}
    return {key: values[key] for key in spec if key in values and values[key] is not None}


def _http_transport(endpoint: str, payload: Dict[str, Any], headers: Dict[str, str],
                    timeout: float) -> Tuple[int, bytes, Dict[str, str]]:
    """默认传输实现：一次 JSON-RPC over HTTP POST。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:  # 4xx / 5xx
        return exc.code, exc.read(), dict(exc.headers.items())


class McpClient:
    """麦当劳 MCP 客户端（Streamable HTTP + Bearer Token）。"""

    def __init__(self,
                 token: Optional[str] = None,
                 endpoint: Optional[str] = None,
                 rate_limit_per_minute: int = DEFAULT_RATE_LIMIT_PER_MINUTE,
                 timeout: float = DEFAULT_TIMEOUT,
                 max_retries: int = DEFAULT_MAX_RETRIES,
                 transport: Optional[Callable[..., Tuple[int, bytes, Dict[str, str]]]] = None,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.endpoint = endpoint or os.environ.get("MCD_MCP_ENDPOINT", DEFAULT_ENDPOINT)
        self._token = token if token is not None else os.environ.get("MCD_MCP_TOKEN", "")
        self.timeout = timeout
        self.max_retries = max_retries
        self.limiter = RateLimiter(rate_limit_per_minute, clock=clock)
        self._sleep = sleep_fn
        self._transport = transport or _http_transport
        self._session_id: Optional[str] = None
        self._request_id = 0
        self.initialized = False

    # -- 基础 ---------------------------------------------------------------
    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        """发送一次 JSON-RPC 请求，处理 401/429，返回 result（或 {}）。"""
        last_body = ""
        for attempt in range(self.max_retries + 1):
            self.limiter.acquire(self._sleep)
            status, raw, resp_headers = self._transport(
                self.endpoint, payload, self._headers(), self.timeout
            )
            session_id = resp_headers.get("Mcp-Session-Id") or resp_headers.get("mcp-session-id")
            if session_id:
                self._session_id = session_id
            body = raw.decode("utf-8", errors="replace") if raw else ""
            last_body = body

            if status == 401:
                raise McpAuthError("401：MCP Token 无效、已过期或未配置")
            if status == 429:
                if attempt >= self.max_retries:
                    raise McpRateLimitError("429：重试后仍触发限流（600 次/分钟）")
                self._sleep(0.5 * (2 ** attempt))
                continue
            if status >= 500 and attempt < self.max_retries:
                self._sleep(0.5 * (2 ** attempt))
                continue
            if status >= 400:
                raise McpError(f"HTTP {status}：{body[:300]}")

            message = _parse_message(body)
            if message is None:
                return {}
            if "error" in message:
                raise McpError(f"MCP 错误：{message['error']}")
            if "result" in message:
                return message["result"] or {}
            return {}
        raise McpError(f"请求失败：{last_body[:300]}")

    def _rpc(self, method: str, params: Optional[Dict[str, Any]] = None,
             notification: bool = False) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params
        return self._send(payload)

    def initialize(self) -> bool:
        """执行 MCP initialize 握手并发送 initialized 通知。"""
        result = self._rpc("initialize", {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "mcd-party-planner", "version": "1.0.0"},
        })
        self._rpc("notifications/initialized", notification=True)
        self.initialized = True
        return bool(result)

    # -- 工具 ---------------------------------------------------------------
    def list_tools(self) -> List[Dict[str, Any]]:
        result = self._rpc("tools/list")
        return list(result.get("tools") or [])

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Any:
        """调用单个 Tool，返回已解析的结果（dict / list / str）。"""
        if not self.initialized and self._transport is _http_transport:
            self.initialize()
        args = build_arguments(name, arguments)
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        return _parse_tool_result(name, result)

    def probe(self) -> Dict[str, Any]:
        """抓取服务端工具定义，用于校准 TOOL_ARG_SPEC（阶段 A 的 A5）。"""
        tools = self.list_tools()
        return {
            "endpoint": self.endpoint,
            "toolCount": len(tools),
            "tools": [
                {
                    "name": t.get("name"),
                    "description": (t.get("description") or "")[:200],
                    "inputSchema": t.get("inputSchema") or {},
                }
                for t in tools
            ],
        }


def _parse_message(body: str) -> Optional[Dict[str, Any]]:
    """解析响应体：兼容普通 JSON 与 SSE（data: ...）两种形态。"""
    body = body.strip()
    if not body:
        return None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def _parse_tool_result(name: str, result: Dict[str, Any]) -> Any:
    """解析 tools/call 结果：优先 structuredContent，其次 content[].text。"""
    if result.get("isError"):
        texts = [c.get("text", "") for c in result.get("content") or [] if isinstance(c, dict)]
        raise McpToolError(f"工具 {name} 返回错误：{' '.join(texts)[:300]}")

    structured = result.get("structuredContent")
    if structured not in (None, {}):
        return structured

    chunks: List[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text" and item.get("text"):
            chunks.append(item["text"])
    if not chunks:
        return result
    text = "\n".join(chunks).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


# -- 归一化辅助（供业务层复用，集中处理响应字段差异） -------------------------
LIST_KEYS: Tuple[str, ...] = ("items", "list", "data", "stores", "storeList",
                              "meals", "mealList", "records", "result", "content")
CODE_KEYS: Tuple[str, ...] = ("storeCode", "storeId", "code", "storeNo", "storeCodeNo")
MEAL_CODE_KEYS: Tuple[str, ...] = ("mealCode", "productCode", "code", "itemCode", "skuCode", "goodsCode")
NAME_KEYS: Tuple[str, ...] = ("name", "storeName", "mealName", "productName", "title")
CATEGORY_KEYS: Tuple[str, ...] = ("categoryName", "category", "categoryCode", "type", "tag", "tags")


def as_list(payload: Any) -> List[Dict[str, Any]]:
    """把形态不确定的响应归一化为 dict 列表。"""
    if payload is None:
        return []
    if isinstance(payload, dict):
        for key in LIST_KEYS:
            value = payload.get(key)
            if isinstance(value, list):
                return [v for v in value if isinstance(v, dict)]
        for value in payload.values():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return [v for v in value if isinstance(v, dict)]
        return [payload]
    if isinstance(payload, list):
        return [v for v in payload if isinstance(v, dict)]
    return []


def first_value(item: Dict[str, Any], keys: Sequence[str], default: Any = None) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", []):
            return value
    return default


def to_float(value: Any, default: float = 0.0) -> float:
    """金额字段归一化：兼容 12.5 / "12.5" / "￥12.5" / "12.5元"。"""
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = "".join(ch for ch in value if (ch.isdigit() or ch in ".-"))
        try:
            return float(cleaned)
        except ValueError:
            return default
    return default


def money_of(item: Dict[str, Any], keys: Sequence[str] = ("total", "totalAmount", "payAmount",
                                                           "amount", "price", "payPrice",
                                                           "actualAmount"),
             scale: float = 1.0) -> float:
    """从价格结果中提取应付金额。

    服务端金额单位为「分」（如 calculate-price 返回 data.price=3180 表示 ¥31.80），
    调用方传 scale=0.01 换算为元。
    """
    if isinstance(item, dict):
        for key in keys:
            if key in item:
                value = to_float(item[key], default=-1.0)
                if value >= 0:
                    return value * scale
        for key in ("data", "result", "priceInfo", "order"):
            nested = item.get(key)
            if isinstance(nested, dict):
                value = money_of(nested, keys, scale)
                if value > 0:
                    return value
    return 0.0


def iter_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item
