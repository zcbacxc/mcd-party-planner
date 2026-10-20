"""业务逻辑层：聚会 / 团餐组织助手（已按真实 MCP schema 校准）。

三个核心场景：

1. 团餐方案生成 —— `query-nearby-stores` → `query-meals` → `query-meal-detail`
   → `calculate-price`，按「人数 / 预算」生成 ≥2 套含总价对比的方案；
2. 主题活动预约 —— `mall-points-products` → `query-party-city` → `query-party-store`
   → `query-party-store-date` → `query-party-store-session` → `party-order-create`
   （**默认 dry_run，不真实下单**）；
3. 活动日历建议 —— `campaign-calendar` × `query-nearby-stores` 交叉输出组织建议，
   并尝试 `delivery-query-addresses` → `delivery-query-stores` → `query-meal-assistance`
   查询企业团餐助餐服务（任一步失败即降级标注）。

真实服务端约定（由 `python -m src.cli probe` 实测确认）：
- 门店查询无 address 参数，用 beType + searchType + city/keyword；
- 菜单只返回 `code` 与分类名，**餐品名称需 `query-meal-detail` 补齐**；
- 金额单位为**分**，换算成元需除以 100；
- 主题活动链路需要 spuId（来自 `mall-points-products`）贯穿始终。

数据约定：用户输入仅运行时内存使用，不落盘；餐品/门店数据一律实时取自 MCP。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

from .mcp_client import (
    McpToolError,
    as_list,
    first_value,
    money_of,
)

# ---- 业务枚举（真实 schema 取值） -------------------------------------------
BE_TYPE_DINE_IN = 1      # 到店取餐
BE_TYPE_GROUP_MEAL = 6   # 企业团餐
ORDER_TYPE_DINE_IN = 1   # 到店（含到店自取）
ORDER_TYPE_DELIVERY = 2  # 外送（含麦乐送 + 企业团餐）
SEARCH_TYPE_BY_LOCATION = 2

CENTS_TO_YUAN = 0.01     # 服务端金额单位为「分」

# 品类角色 → 分类名关键字（真实分类名形如「鸡肉汉堡\n/卷」「小食甜品\n/其他」）
# 一个分类可同时命中多个角色（例如「小食甜品/其他」既作小食也作甜品）。
CATEGORY_ROLE_KEYWORDS: Dict[str, Tuple[str, ...]] = {
    "主食": ("汉堡", "堡", "卷", "单人餐", "大堡口福", "牛鱼肉堡", "套餐"),
    "小食": ("小食", "炸鸡", "蘸酱", "鸡", "薯"),
    "甜品": ("甜品", "冰淇淋", "圣代", "派", "蛋糕"),
    "饮料": ("饮品", "咖啡", "麦咖啡"),
}

# 三套组合：单人份构成（顺序即组合层级）
COMBO_SPECS: Dict[str, Tuple[str, ...]] = {
    "经济型": ("主食", "饮料"),
    "均衡型": ("主食", "小食", "饮料"),
    "丰盛型": ("主食", "小食", "饮料", "甜品"),
}

NAME_KEYS = ("name", "storeName", "shortName", "productName", "spuName", "title")


# ---- 通用辅助 ---------------------------------------------------------------
def _data_of(payload: Any) -> Any:
    """取统一响应体的 data 段。"""
    if isinstance(payload, dict):
        return payload.get("data", payload)
    return payload


def _is_ok(payload: Any) -> bool:
    return not (isinstance(payload, dict) and payload.get("success") is False)


def store_code_of(store: Dict[str, Any]) -> str:
    return str(first_value(store, ("storeCode", "code"), "") or "")


def name_of(item: Dict[str, Any], default: str = "未命名") -> str:
    return str(first_value(item, NAME_KEYS, default) or default)


def _open_stores(payload: Any, limit: int) -> List[Dict[str, Any]]:
    """优先取营业中门店（非营业门店拉菜单会报「门店可能已关闭或不在营业时间」）。"""
    rows = as_list(_data_of(payload))
    opened = [r for r in rows if r.get("businessStatus") in (True, 1)]
    chosen = opened if opened else rows
    return chosen[: max(1, limit)]


def _roles_of_category(category_name: str) -> List[str]:
    return [role for role, keywords in CATEGORY_ROLE_KEYWORDS.items()
            if any(k in category_name for k in keywords)]


def _role_to_codes(categories: List[Dict[str, Any]]) -> Dict[str, List[Tuple[str, str]]]:
    """角色 → [(餐品 code, 所属分类名)]。"""
    mapping: Dict[str, List[Tuple[str, str]]] = {role: [] for role in CATEGORY_ROLE_KEYWORDS}
    for category in categories:
        cat_name = str(category.get("name") or "")
        roles = _roles_of_category(cat_name)
        if not roles:
            continue
        for meal in as_list(category.get("meals")):
            code = str(first_value(meal, ("code", "productCode"), "") or "")
            if not code:
                continue
            for role in roles:
                mapping[role].append((code, cat_name))
    return mapping


def _pick_codes(mapping: Dict[str, List[Tuple[str, str]]], roles: Sequence[str]
                ) -> Tuple[List[Tuple[str, str, str]], List[str]]:
    """按角色挑选餐品，避免同一分类内重复选用。返回 [(code, role, 分类名)] 与缺失角色。"""
    used: set = set()
    picked: List[Tuple[str, str, str]] = []
    missing: List[str] = []
    for role in roles:
        chosen = None
        for code, cat_name in mapping.get(role, []):
            if (code, cat_name) not in used:
                chosen = (code, role, cat_name)
                break
        if chosen:
            used.add((chosen[0], chosen[2]))
            picked.append(chosen)
        else:
            missing.append(role)
    return picked, missing


class _NameCache:
    """餐品名称缓存：菜单只返回 code，名称需逐个查详情，缓存避免重复调用。"""

    def __init__(self, client: Any, store_code: str) -> None:
        self.client = client
        self.store_code = store_code
        self._cache: Dict[str, str] = {}

    def get(self, code: str) -> str:
        if code in self._cache:
            return self._cache[code]
        try:
            detail = self.client.call_tool("query-meal-detail", {
                "storeCode": self.store_code, "orderType": ORDER_TYPE_DINE_IN,
                "beType": BE_TYPE_DINE_IN, "code": code,
            })
        except McpToolError:
            self._cache[code] = code
            return code
        data = _data_of(detail)
        label = name_of(data if isinstance(data, dict) else {}, default=code)
        self._cache[code] = label
        return label


# ---------------------------------------------------------------------------
# 场景 1：团餐方案生成
# ---------------------------------------------------------------------------
def plan_group_meal(client: Any, city: str, keyword: str, headcount: int, budget: float,
                    store_limit: int = 3,
                    combos: Sequence[str] = ("经济型", "均衡型", "丰盛型")) -> Dict[str, Any]:
    """按「城市 + 关键词 + 人数 + 预算」生成 ≥2 套可比对团餐方案（真实计价）。"""
    if headcount <= 0:
        raise ValueError("headcount 必须为正整数")
    if budget < 0:
        raise ValueError("budget 不能为负")

    warnings: List[str] = []
    stores = _open_stores(client.call_tool("query-nearby-stores", {
        "beType": BE_TYPE_DINE_IN, "searchType": SEARCH_TYPE_BY_LOCATION,
        "city": city, "keyword": keyword,
    }), store_limit)
    if not stores:
        return {"city": city, "keyword": keyword, "headcount": headcount, "budget": budget,
                "storesScanned": 0, "plans": [], "recommended": None,
                "affordableCount": 0, "warnings": ["未查询到附近门店"]}

    plans: List[Dict[str, Any]] = []
    for store in stores:
        store_code = store_code_of(store)
        if not store_code:
            continue
        if store.get("businessStatus") not in (True, 1):
            warnings.append(f"门店 {name_of(store)} 当前非营业时间，计价结果可能不可用")

        menu = client.call_tool("query-meals", {
            "storeCode": store_code, "orderType": ORDER_TYPE_DINE_IN, "beType": BE_TYPE_DINE_IN,
        })
        if not _is_ok(menu):
            warnings.append(f"门店 {name_of(store)} 菜单查询失败：{menu.get('message')}")
            continue
        categories = as_list((_data_of(menu) or {}).get("categories"))
        if not categories:
            warnings.append(f"门店 {name_of(store)} 未返回可售餐品")
            continue

        mapping = _role_to_codes(categories)
        cache = _NameCache(client, store_code)

        for combo in combos:
            picked, missing = _pick_codes(mapping, COMBO_SPECS.get(combo, ("主食",)))
            if not picked:
                warnings.append(f"门店 {name_of(store)} 缺少组合「{combo}」所需品类")
                continue
            if missing:
                warnings.append(
                    f"门店 {name_of(store)} 的组合「{combo}」降级：缺少 {'、'.join(missing)}"
                )
            price = client.call_tool("calculate-price", {
                "storeCode": store_code, "orderType": ORDER_TYPE_DINE_IN,
                "beType": BE_TYPE_DINE_IN,
                "items": [{"productCode": code, "quantity": headcount}
                          for code, _role, _cat in picked],
            })
            if not _is_ok(price):
                warnings.append(
                    f"门店 {name_of(store)} / {combo} 计价失败：{price.get('message')}")
                continue

            total = money_of(_data_of(price), scale=CENTS_TO_YUAN)
            plans.append({
                "store": name_of(store),
                "storeCode": store_code,
                "combo": combo,
                "total": round(total, 2),
                "perPerson": round(total / headcount, 2) if headcount else 0.0,
                "withinBudget": total <= budget if budget else True,
                "items": [{"name": cache.get(code), "category": role, "quantity": headcount}
                          for code, role, _cat in picked],
            })

    plans.sort(key=lambda p: (not p["withinBudget"], p["perPerson"]))
    affordable = [p for p in plans if p["withinBudget"]]
    return {
        "city": city, "keyword": keyword, "headcount": headcount, "budget": budget,
        "storesScanned": len(stores), "plans": plans,
        "recommended": affordable[0] if affordable else (plans[0] if plans else None),
        "affordableCount": len(affordable), "warnings": warnings,
    }


def render_group_meal_report(result: Dict[str, Any]) -> str:
    lines = [
        "【团餐方案】",
        f"城市：{result['city']}　关键词：{result['keyword']}　"
        f"人数：{result['headcount']}　预算：¥{result['budget']}",
        f"已扫描门店：{result['storesScanned']}　可选方案：{len(result['plans'])}　"
        f"预算内方案：{result['affordableCount']}",
        "",
    ]
    for index, plan in enumerate(result["plans"], 1):
        lines.append(
            f"{index}. [{plan['combo']}] {plan['store']}　合计 ¥{plan['total']}　"
            f"人均 ¥{plan['perPerson']}　{'预算内' if plan['withinBudget'] else '超预算'}"
        )
        lines.append("   构成：" + "、".join(
            f"{i['name']}×{i['quantity']}" for i in plan["items"]))
    if result["recommended"]:
        rec = result["recommended"]
        lines += ["", f"推荐：[{rec['combo']}] {rec['store']}，"
                      f"合计 ¥{rec['total']}，人均 ¥{rec['perPerson']}"]
    for warning in result["warnings"]:
        lines.append(f"！提示：{warning}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 场景 2：主题活动（派对 / 品鉴会）预约
# ---------------------------------------------------------------------------
def discover_party_spu(client: Any, keyword: str = "派对") -> Optional[Dict[str, Any]]:
    """从麦麦商城商品中定位派对类 spu（主题活动链路的入口参数 spuId）。"""
    payload = client.call_tool("mall-points-products", {})
    for row in as_list(_data_of(payload)):
        text = f"{row.get('spuName', '')}{row.get('catName', '')}"
        if keyword in text:
            return {"spuId": row.get("spuId"), "spuName": row.get("spuName"),
                    "catName": row.get("catName")}
    return None


def _sku_id_of(client: Any, spu_id: Any) -> Optional[Any]:
    try:
        detail = client.call_tool("mall-product-detail", {"spuId": spu_id})
    except McpToolError:
        return None
    data = _data_of(detail) or {}
    for sku in as_list(data.get("skuList")):
        return sku.get("skuId")
    return None


def _select_session(step: Any, spu_id: Any, stores: Sequence[Dict[str, Any]],
                    preferred_date: Optional[str], count: int,
                    max_stores: int = 3, max_dates: int = 3
                    ) -> Optional[Tuple[Dict[str, Any], str, str, Dict[str, Any]]]:
    """在「门店 × 日期 × 场次」中挑选可容纳人数的最近场次。

    真实服务端常见「首店当日无场次」或「首场余位不足」，逐级探测可显著降低
    整条链路无输出的概率。返回 (门店, 门店编码, 日期, 场次)；全部不可用时返回 None。
    """
    fallback: Optional[Tuple[Dict[str, Any], str, str, Dict[str, Any]]] = None
    for store in stores[:max_stores]:
        store_code = store_code_of(store)
        if not store_code:
            continue
        dates = as_list(_data_of(step("query-party-store-date",
                                      {"spuId": spu_id, "storeCode": store_code})))
        date_list = [str(first_value(d, ("date", "dateStr"), "") or "") for d in dates]
        date_list = [d for d in date_list if d]
        if preferred_date:
            # 用户显式指定日期：该店无此日期则换店，绝不改到别的日期
            date_list = [d for d in date_list if d == preferred_date]
        for date_str in date_list[:max_dates]:
            sessions = as_list(_data_of(step("query-party-store-session", {
                "spuId": spu_id, "storeCode": store_code, "dateStr": date_str,
            })))
            if not sessions:
                continue
            fit = next((s for s in sessions if (s.get("leftNum") or 0) >= count), None)
            if fit:
                return store, store_code, date_str, fit
            if fallback is None:
                fallback = (store, store_code, date_str, sessions[0])
    return fallback


def plan_party_booking(client: Any, city_name: str, spu_id: Optional[Any] = None,
                       date: Optional[str] = None, count: int = 5,
                       party_type: int = 1, dry_run: bool = True) -> Dict[str, Any]:
    """串联主题活动预约链路。dry_run=True（默认）只产出可下单载荷，不创建订单。"""
    trace: List[Dict[str, Any]] = []

    def step(tool: str, arguments: Dict[str, Any], **kwargs: Any) -> Any:
        payload = client.call_tool(tool, arguments, **kwargs)
        trace.append({"tool": tool, "arguments": arguments, "ok": _is_ok(payload),
                      "message": payload.get("message") if isinstance(payload, dict) else None})
        return payload

    spu = {"spuId": spu_id} if spu_id else discover_party_spu(client)
    if not spu or not spu.get("spuId"):
        return {"ok": False, "reason": "未定位到派对类主题商品（spuId）", "trace": trace}
    spu_id = spu["spuId"]

    cities = as_list(_data_of(step("query-party-city", {"spuId": spu_id})))
    # 明确匹配城市：不做「取第一个城市」的兜底，避免把派对预约到异地
    city = next((c for c in cities if city_name in str(c.get("name") or "")), None)
    if not city:
        candidates = "、".join(str(c.get("name") or "") for c in cities[:8])
        return {"ok": False, "trace": trace,
                "reason": f"未查询到城市「{city_name}」的主题活动；可选城市示例：{candidates}"}

    stores = as_list(_data_of(step("query-party-store", {
        "spuId": spu_id, "code": city.get("code"),
        "latitude": city.get("latitude"), "longitude": city.get("longitude"),
    })))
    if not stores:
        return {"ok": False, "reason": "该城市暂无可参与主题活动的门店", "trace": trace}

    # 逐店 × 逐日期探测：首店/首日无可用场次时继续尝试，避免单点失败中断整条链路
    selection = _select_session(step, spu_id, stores, date, count)
    if not selection:
        reason = (f"门店在 {date} 暂无可预约场次" if date
                  else "附近门店暂无可预约的日期或场次")
        return {"ok": False, "reason": reason, "trace": trace}
    store, store_code, picked_date, session = selection

    order_payload = {
        "partyType": party_type,
        "spuId": spu_id,
        "skuId": _sku_id_of(client, spu_id),
        "storeCode": store_code,
        "code": city.get("code"),
        "dateStr": picked_date,
        "id": session.get("id"),
        "leftNum": session.get("leftNum"),
        "timeStart": session.get("timeStart"),
        "timeEnd": session.get("timeEnd"),
        "count": count,
        "partyTimeInfo": session,
    }

    result: Dict[str, Any] = {
        "ok": True, "spu": spu, "city": city.get("name"), "store": name_of(store),
        "storeCode": store_code, "date": picked_date,
        "session": {"id": session.get("id"), "timeStart": session.get("timeStart"),
                    "timeEnd": session.get("timeEnd"),
                    "price": money_of(session, scale=CENTS_TO_YUAN),
                    "leftNum": session.get("leftNum"),
                    "partyMin": session.get("partyMin"), "partyMax": session.get("partyMax")},
        "orderPayload": order_payload, "trace": trace, "dryRun": dry_run,
    }
    left_num = session.get("leftNum") or 0
    if left_num < count:
        result["warning"] = f"所选场次余位 {left_num} 少于人数 {count}，建议换场次或拆单"
    if dry_run:
        result["order"] = None
        result["note"] = "dry_run=True：未真实下单，仅输出可下单载荷"
        return result

    # 显式声明非幂等：遇 5xx 不得自动重试，避免重复下单（详见 mcp_client.WRITE_TOOLS）
    result["order"] = step("party-order-create", order_payload, idempotent=False)
    result["note"] = "已提交真实下单请求"
    return result


def render_party_report(result: Dict[str, Any]) -> str:
    if not result.get("ok"):
        return f"【主题活动预约】无法继续：{result.get('reason')}"
    session = result.get("session") or {}
    lines = [
        "【主题活动预约】",
        f"主题：{result['spu'].get('spuName')}（spuId={result['spu'].get('spuId')}）",
        f"城市：{result.get('city')}　门店：{result.get('store')}（{result.get('storeCode')}）",
        f"日期：{result.get('date')}　场次：{session.get('timeStart')}-{session.get('timeEnd')}　"
        f"余位：{session.get('leftNum')}　价格：¥{session.get('price')}",
        f"调用链路：{' → '.join(t['tool'] for t in result['trace'])}",
        f"下单载荷：{result.get('orderPayload')}",
        f"！提示：{result['warning']}" if result.get("warning") else "",
        result.get("note", ""),
    ]
    return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# 场景 3：活动日历 × 附近门店 组织建议
# ---------------------------------------------------------------------------
def suggest_calendar_plan(client: Any, city: str, keyword: str, store_limit: int = 3,
                          specified_date: Optional[str] = None) -> Dict[str, Any]:
    calendar = client.call_tool("campaign-calendar",
                                {"specifiedDate": specified_date} if specified_date else {})
    days = as_list((_data_of(calendar) or {}).get("dailyList"))
    stores = _open_stores(client.call_tool("query-nearby-stores", {
        "beType": BE_TYPE_DINE_IN, "searchType": SEARCH_TYPE_BY_LOCATION,
        "city": city, "keyword": keyword,
    }), store_limit)

    suggestions: List[Dict[str, Any]] = []
    for day in days:
        titles = [_event_title(e) for e in as_list(day.get("events"))]
        titles = [t for t in titles if t][:5]
        date_text = str(day.get("dateText") or "")
        suggestions.append({
            "date": day.get("date"),
            "dateText": date_text,
            "events": titles,
            "stores": [name_of(s) for s in stores],
            "advice": _advice_for(date_text, len(stores)),
        })

    return {
        "city": city, "keyword": keyword, "storeCount": len(stores),
        "days": suggestions,
        "stores": [{"name": name_of(s), "code": store_code_of(s)} for s in stores],
        "mealAssistance": _group_meal_assistance(client),
    }


def _group_meal_assistance(client: Any) -> List[Dict[str, Any]]:
    """企业团餐助餐服务查询。任一步失败即降级标注，不中断流程。"""
    try:
        addresses = as_list((_data_of(client.call_tool("delivery-query-addresses", {})) or {})
                            .get("addresses"))
        if not addresses:
            return [{"note": "账号暂无配送地址，无法查询企业团餐助餐服务"}]
        address_id = first_value(addresses[0], ("addressId", "id"))
        stores = as_list(_data_of(client.call_tool("delivery-query-stores", {
            "addressId": address_id, "beType": BE_TYPE_GROUP_MEAL,
        })))
        result: List[Dict[str, Any]] = []
        for store in stores[:3]:
            try:
                payload = client.call_tool("query-meal-assistance", {
                    "storeCode": store_code_of(store),
                    "beCode": first_value(store, ("beCode",)),
                    "beType": BE_TYPE_GROUP_MEAL, "orderType": ORDER_TYPE_DELIVERY,
                })
                data = _data_of(payload)
                rows = as_list(data.get("mealAssistanceItems") if isinstance(data, dict)
                               else data)
                names = [str(first_value(r, ("gmServiceName", "name"), "助餐服务") or "助餐服务")
                         for r in rows]
                usable = [str(first_value(r, ("gmServiceName", "name"), "助餐服务") or "助餐服务")
                          for r in rows if r.get("enable") is not False]
                result.append({"store": name_of(store),
                               "available": bool(usable or names),
                               "services": (usable or names)[:5]})
            except McpToolError as exc:
                result.append({"store": name_of(store), "available": False,
                               "services": [], "note": f"助餐服务查询失败：{exc}"})
        return result
    except McpToolError as exc:
        return [{"note": f"企业团餐助餐服务不可用：{exc}"}]


def _event_title(event: Dict[str, Any]) -> str:
    """活动标题：真实数据中 activityTitle 常为空，标题在 articleDto 内。"""
    for key in ("activityTitle", "activitySubTitle", "activityTag"):
        value = str(event.get(key) or "").strip()
        if value:
            return value
    article = event.get("articleDto") or {}
    for key in ("title", "highlights"):
        value = str(article.get(key) or "").strip()
        if value:
            return value
    return str(event.get("activityCode") or "").strip()


def _advice_for(date_text: str, store_count: int) -> str:
    if "往期" in date_text:
        return "已过期活动，仅作参考；可关注同系列后续活动"
    if "今日" in date_text:
        return f"今日可参与，建议立即组织；优先锁定 {min(store_count, 3)} 家附近门店的团餐方案"
    return "未来活动，建议提前 3–5 天预约主题活动场次并预排团餐"


def render_calendar_report(result: Dict[str, Any]) -> str:
    lines = [
        "【活动日历组织建议】",
        f"城市：{result['city']}　关键词：{result['keyword']}　附近门店：{result['storeCount']}",
        "",
    ]
    if not result["days"]:
        lines.append("未查询到活动日历数据。")
    for day in result["days"]:
        events = "、".join(day["events"]) or "（无活动标题）"
        lines.append(f"· {day.get('date')}　{day.get('dateText')}")
        lines.append(f"  活动：{events}")
        lines.append(f"  建议：{day['advice']}")
    lines += ["", "企业团餐助餐服务："]
    for item in result["mealAssistance"]:
        if "store" not in item:
            lines.append(f"· {item.get('note')}")
            continue
        flag = "支持" if item["available"] else "不支持"
        detail = "、".join(item["services"]) if item["services"] else item.get("note", "")
        lines.append(f"· {item['store']}：{flag} {detail}")
    return "\n".join(lines)
