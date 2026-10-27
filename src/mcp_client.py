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
import random
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import __version__

DEFAULT_ENDPOINT = "https://mcp.mcd.cn"

# 协议版本：客户端只声明自己实现的那一版，服务端返回其它版本时直接报错
# （2025-06-18 规范：客户端若不支持服务端返回的版本，SHOULD 断开）。
PROTOCOL_VERSION = "2025-06-18"
SUPPORTED_PROTOCOL_VERSIONS: Tuple[str, ...] = (PROTOCOL_VERSION,)

# tools/list 分页上限：防止服务端返回异常 cursor 时死循环。
MAX_TOOL_PAGES = 10

# 429 / 5xx 退避：基数 0.5s，指数增长并叠加 Full Jitter（AWS 推荐），
# 避免多进程 / CI 并发时所有客户端在同一时刻重试。
BACKOFF_BASE = 0.5


def _backoff_delay(attempt: int) -> float:
    """第 attempt 次重试的等待秒数：`uniform(0, base * 2^attempt)`（Full Jitter）。"""
    return random.uniform(0.0, BACKOFF_BASE * (2 ** attempt))

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


# ---------------------------------------------------------------------------
# 非幂等（写）工具：这些调用会在服务端产生副作用（下单 / 建址 / 核销）。
#
# 重试策略差异（重要）：
# - 读工具：429 与 5xx 均可安全重试（重复查询无副作用）；
# - 写工具：**只对 429 重试**（429 表示请求被限流拒绝、未被受理），
#   5xx 一律不重试——服务端可能已创建订单但响应失败，重发会产生重复订单。
# ---------------------------------------------------------------------------
WRITE_TOOLS = frozenset({
    "party-order-create",     # 主题活动下单
    "create-order",           # 到店 / 外送下单
    "mall-create-order",      # 麦麦商城下单
    "delivery-create-address",  # 新建配送地址
    "cancel-order",           # 取消订单
    "auto-bind-coupons",      # 自动绑券
    "draw-lottery",           # 抽奖（消耗机会）
})


class McpError(RuntimeError):
    """MCP 调用失败的基类。"""


class McpNetworkError(McpError):
    """网络层失败：DNS、连接重置、超时、代理故障等（请求可能未到达服务端）。"""


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
    """默认传输实现：一次 JSON-RPC over HTTP POST。

    网络层异常统一归一化为 `McpNetworkError`，避免调用方看到裸的 urllib 堆栈
    （常见场景：企业代理返回 502、DNS 失败、连接重置、请求超时）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(endpoint, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read(), dict(response.headers.items())
    except urllib.error.HTTPError as exc:  # 4xx / 5xx（HTTPError 是 URLError 的子类，必须在前）
        return exc.code, exc.read(), dict(exc.headers.items())
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise McpNetworkError(f"网络不可达：{reason}") from exc


def _http_delete(endpoint: str, headers: Dict[str, str], timeout: float) -> int:
    """尽力发送 HTTP DELETE 终止会话，返回状态码；网络异常返回 0。

    会话终止失败不应影响主流程（结果已产出），因此这里吞掉所有网络异常。
    """
    request = urllib.request.Request(endpoint, headers=headers, method="DELETE")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status
    except urllib.error.HTTPError as exc:  # 4xx / 5xx 也是「有响应」
        return exc.code
    except (urllib.error.URLError, TimeoutError, OSError):
        return 0


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
        # 握手协商出的协议版本；未握手时为 None（此时不发送协议头）
        self.protocol_version: Optional[str] = None
        # 调用统计：供 CLI `--verbose` 输出到 stderr，不进入 stdout 报告
        self.stats: Dict[str, int] = {
            "calls": 0,
            "retries": 0,
            "rateLimitWaits": 0,
            "elapsedMs": 0,
        }

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
        # 2025-06-18 规范：HTTP 传输下，握手后的所有请求都必须带上协商出的版本
        if self.protocol_version:
            headers["MCP-Protocol-Version"] = self.protocol_version
        return headers

    def _next_id(self) -> int:
        self._request_id += 1
        return self._request_id

    def _send(self, payload: Dict[str, Any],
              retry_on_server_error: bool = True) -> Dict[str, Any]:
        """发送一次 JSON-RPC 请求，处理 401/429/5xx，返回 result（或 {}）。

        `retry_on_server_error=False` 用于非幂等写操作：429 仍重试（请求被拒绝、
        未被受理），但 5xx 立即失败，避免重复下单。
        """
        last_body = ""
        for attempt in range(self.max_retries + 1):
            waited = self.limiter.acquire(self._sleep)
            if waited > 0:
                self.stats["rateLimitWaits"] += 1
            started = time.monotonic()
            status, raw, resp_headers = self._transport(
                self.endpoint, payload, self._headers(), self.timeout
            )
            self.stats["calls"] += 1
            self.stats["elapsedMs"] += int((time.monotonic() - started) * 1000)
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
                self.stats["retries"] += 1
                self._sleep(_backoff_delay(attempt))
                continue
            if status >= 500:
                if not retry_on_server_error:
                    raise McpError(
                        f"HTTP {status}：服务端错误。该操作会写入数据，未自动重试；"
                        "请求可能已被受理，请先到麦当劳官方渠道核对，再决定是否重试"
                    )
                if attempt < self.max_retries:
                    self.stats["retries"] += 1
                    self._sleep(_backoff_delay(attempt))
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
             notification: bool = False, retry_on_server_error: bool = True) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if not notification:
            payload["id"] = self._next_id()
        if params is not None:
            payload["params"] = params
        return self._send(payload, retry_on_server_error=retry_on_server_error)

    def initialize(self) -> bool:
        """执行 MCP initialize 握手、校验协议版本并发送 initialized 通知。

        规范要求客户端声明自己支持的版本，服务端若返回其它版本则客户端 SHOULD 断开；
        这里以 `McpError` 中止并给出双方版本，避免在协议语义不一致时继续调用。
        """
        result = self._rpc("initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "mcd-party-planner", "version": __version__},
        })
        server_version = result.get("protocolVersion") if isinstance(result, dict) else None
        if server_version and server_version not in SUPPORTED_PROTOCOL_VERSIONS:
            raise McpError(
                f"协议版本不兼容：服务端返回 {server_version}，"
                f"客户端支持 {', '.join(SUPPORTED_PROTOCOL_VERSIONS)}"
            )
        self.protocol_version = server_version or PROTOCOL_VERSION
        self._rpc("notifications/initialized", notification=True)
        self.initialized = True
        return bool(result)

    # -- 工具 ---------------------------------------------------------------
    def _should_auto_initialize(self) -> bool:
        """是否需要先握手。

        默认传输必握手；自定义传输（测试替身）默认不握手，避免打乱其预设的调用序列；
        录制 / 回放传输显式声明 `auto_initialize = True`，因为它们同样需要 `initialize`
        才能拿到协议版本与工具清单。
        """
        if self._transport is _http_transport:
            return True
        return bool(getattr(self._transport, "auto_initialize", False))

    def list_tools(self) -> List[Dict[str, Any]]:
        """列出全部工具，按 `nextCursor` 翻页（当前服务端一页返回 35 个，无 cursor）。"""
        if not self.initialized and self._should_auto_initialize():
            self.initialize()
        tools: List[Dict[str, Any]] = []
        cursor: Optional[str] = None
        for _ in range(MAX_TOOL_PAGES):
            params = {"cursor": cursor} if cursor else None
            result = self._rpc("tools/list", params)
            tools.extend(list(result.get("tools") or []))
            cursor = result.get("nextCursor")
            if not cursor:
                break
        return tools

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None,
                  idempotent: Optional[bool] = None) -> Any:
        """调用单个 Tool，返回已解析的结果（dict / list / str）。

        重试策略按工具是否幂等自动区分：`WRITE_TOOLS` 中的工具（下单 / 建址 /
        核销等）遇 5xx 不重试，避免重复产生副作用；显式传 `idempotent` 可覆盖。
        """
        if not self.initialized and self._should_auto_initialize():
            self.initialize()
        if idempotent is None:
            idempotent = name not in WRITE_TOOLS
        args = build_arguments(name, arguments)
        result = self._rpc("tools/call", {"name": name, "arguments": args},
                           retry_on_server_error=idempotent)
        return _parse_tool_result(name, result)

    def close(self) -> bool:
        """尽力终止会话（HTTP DELETE），返回是否收到 2xx。

        自定义 transport（测试替身）不发真实请求，只清理本地状态；
        会话终止失败不应影响主流程，因此不抛异常。
        """
        if not self._session_id:
            self.initialized = False
            return False
        headers = self._headers()
        headers.pop("Content-Type", None)  # DELETE 无 body
        status = 0
        if self._transport is _http_transport:
            status = _http_delete(self.endpoint, headers, self.timeout)
        self._session_id = None
        self.protocol_version = None
        self.initialized = False
        return 200 <= status < 300

    def probe(self) -> Dict[str, Any]:
        """抓取服务端工具定义，用于校准 TOOL_ARG_SPEC（阶段 A 的 A5）。

        除入参 schema 外，一并保留 `title` / `outputSchema` / `annotations`：
        实测服务端 35/35 个工具都带 `outputSchema`（含「单位分」等字段说明），
        丢掉它会让校准产物无法用于校验响应结构。
        """
        tools = self.list_tools()
        return {
            "endpoint": self.endpoint,
            "protocolVersion": self.protocol_version,
            "toolCount": len(tools),
            "tools": [
                {
                    "name": t.get("name"),
                    "title": t.get("title"),
                    "description": (t.get("description") or "")[:200],
                    "inputSchema": t.get("inputSchema") or {},
                    "outputSchema": t.get("outputSchema") or {},
                    "annotations": t.get("annotations") or {},
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
NAME_KEYS: Tuple[str, ...] = ("name", "storeName", "mealName", "productName", "title")


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
