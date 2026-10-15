"""业务逻辑层单元测试（按真实 MCP 响应结构构造替身）。

行为契约：团餐方案 ≥2 套含总价对比；主题活动链路各 Tool 均调用、
且 dry_run 下不真实下单；活动日历 × 门店交叉建议 + 团餐助餐降级。

替身数据均取自 `python -m src.cli probe` 实测到的真实响应形态：
- 门店：data[] {storeCode, storeName, businessStatus}
- 菜单：data.categories[] {name, meals:[{code}]}（无餐品名，需详情补齐）
- 计价：data.price（单位：分）
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import party_planner as pp  # noqa: E402
from src.mcp_client import McpToolError  # noqa: E402

# 单位价格（元）：主食 25、小食 12、饮料 9、甜品 8；服务端单位为分
UNIT = {"M1": 25.0, "M1B": 22.0, "S1": 12.0, "D1": 8.0, "B1": 9.0}
NAMES = {"M1": "巨无霸", "M1B": "双层吉士汉堡", "S1": "薯条", "D1": "香芋派", "B1": "可乐"}

STORES = {"success": True, "data": [
    {"storeCode": "3450339", "storeName": "总府路茂业百货餐厅", "businessStatus": True},
    {"storeCode": "3450043", "storeName": "壹购广场餐厅", "businessStatus": True},
]}
MENU = {"success": True, "data": {"categories": [
    {"name": "巨无霸\n牛鱼肉堡", "meals": [{"code": "M1"}, {"code": "M1B"}]},
    {"name": "小食甜品\n/其他", "meals": [{"code": "S1"}, {"code": "D1"}]},
    {"name": "饮品", "meals": [{"code": "B1"}]},
]}}


class StubClient:
    """按真实响应形态返回数据的替身客户端，并记录调用轨迹。"""

    def __init__(self, dispatch):
        self.dispatch = dispatch
        self.calls = []

    def call_tool(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        return self.dispatch(name, arguments or {})


def group_dispatch(name, arguments):
    if name == "query-nearby-stores":
        return STORES
    if name == "query-meals":
        return MENU
    if name == "query-meal-detail":
        code = arguments["code"]
        return {"success": True, "data": {"code": code, "name": NAMES.get(code, code)}}
    if name == "calculate-price":
        cents = sum(UNIT.get(i["productCode"], 0.0) * i["quantity"] for i in arguments["items"])
        return {"success": True, "data": {"price": round(cents * 100), "productList": []}}
    raise AssertionError(f"未预设的工具：{name}")


class GroupMealTest(unittest.TestCase):
    def setUp(self):
        self.client = StubClient(group_dispatch)

    def test_generates_multiple_comparable_plans(self):
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=4, budget=400)
        self.assertGreaterEqual(len(result["plans"]), 2)
        self.assertEqual(result["storesScanned"], 2)
        for plan in result["plans"]:
            self.assertIn(plan["combo"], ("经济型", "均衡型", "丰盛型"))
            self.assertAlmostEqual(plan["perPerson"], round(plan["total"] / 4, 2), places=2)
            self.assertTrue(plan["items"], "每个方案都应含餐品明细")

    def test_real_price_unit_conversion(self):
        """服务端金额单位为分，须正确换算为元（总价对比的前提）。"""
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=4, budget=400)
        economy = next(p for p in result["plans"] if p["combo"] == "经济型")
        self.assertAlmostEqual(economy["total"], (25 + 9) * 4, places=2)

    def test_meal_names_filled_from_detail(self):
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=2, budget=400)
        names = {i["name"] for p in result["plans"] for i in p["items"]}
        self.assertIn("巨无霸", names)
        self.assertIn("可乐", names)

    def test_budget_filter_and_recommendation(self):
        # 4 人：经济型 136、均衡型 184、丰盛型 216 → 预算 200 时丰盛型超预算
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=4, budget=200)
        self.assertTrue(result["recommended"]["withinBudget"])
        self.assertTrue([p for p in result["plans"] if not p["withinBudget"]],
                        "应识别出超预算方案以形成对比")

    def test_sorted_affordable_first(self):
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=4, budget=200)
        flags = [p["withinBudget"] for p in result["plans"]]
        self.assertEqual(flags, sorted(flags, key=lambda f: not f))

    def test_missing_role_produces_degradation_warning(self):
        menu_without_dessert = {"success": True, "data": {"categories": [
            {"name": "巨无霸\n牛鱼肉堡", "meals": [{"code": "M1"}]},
            {"name": "炸鸡", "meals": [{"code": "S1"}]},
            {"name": "饮品", "meals": [{"code": "B1"}]},
        ]}}

        def dispatch(name, arguments):
            if name == "query-meals":
                return menu_without_dessert
            return group_dispatch(name, arguments)

        result = pp.plan_group_meal(StubClient(dispatch), "成都", "春熙路",
                                    headcount=2, budget=400)
        self.assertTrue(any("降级" in w and "甜品" in w for w in result["warnings"]),
                        f"应记录甜品缺失降级，实际：{result['warnings']}")

    def test_closed_store_warns_but_continues(self):
        closed = {"success": True, "data": [
            {"storeCode": "S0", "storeName": "打烊餐厅", "businessStatus": False}]}

        def dispatch(name, arguments):
            if name == "query-nearby-stores":
                return closed
            return group_dispatch(name, arguments)

        result = pp.plan_group_meal(StubClient(dispatch), "成都", "春熙路",
                                    headcount=2, budget=300)
        self.assertEqual(result["storesScanned"], 1)
        self.assertTrue(any("非营业时间" in w for w in result["warnings"]))

    def test_invalid_headcount(self):
        with self.assertRaises(ValueError):
            pp.plan_group_meal(self.client, "成都", "春熙路", headcount=0, budget=100)

    def test_report_contains_totals(self):
        result = pp.plan_group_meal(self.client, "成都", "春熙路", headcount=4, budget=400)
        text = pp.render_group_meal_report(result)
        self.assertIn("【团餐方案】", text)
        self.assertIn("人均", text)
        self.assertIn("推荐：", text)


PARTY_SPU = {"success": True, "data": [
    {"spuId": 1779, "spuName": "麦当劳奇妙生日派对", "catName": "生日类派对"}]}
CITY = {"success": True, "data": [
    {"code": 510100, "name": "成都市", "latitude": 30.5, "longitude": 104.0}]}
PSTORE = {"success": True, "data": [
    {"code": "3450261", "name": "大魔方餐厅", "businessStatus": 1}]}
PDATE = {"success": True, "data": [{"date": "2026-10-16", "storeCode": "3450261"}]}
PSESSION = {"success": True, "data": [
    {"id": 36884158, "timeStart": "12:00", "timeEnd": "13:30", "price": 5000,
     "leftNum": 12, "partyMin": 5, "partyMax": 12}]}
PORDER = {"success": True, "data": {"orderNo": "ORDER-1"}}
SKU = {"success": True, "data": {"skuList": [{"skuId": 2027}]}}


def party_dispatch(name, arguments):
    return {
        "mall-points-products": PARTY_SPU,
        "mall-product-detail": SKU,
        "query-party-city": CITY,
        "query-party-store": PSTORE,
        "query-party-store-date": PDATE,
        "query-party-store-session": PSESSION,
        "party-order-create": PORDER,
    }[name]


class PartyBookingTest(unittest.TestCase):
    def setUp(self):
        self.client = StubClient(party_dispatch)

    def test_dry_run_does_not_create_order(self):
        result = pp.plan_party_booking(self.client, "成都市", count=6)
        self.assertTrue(result["ok"])
        self.assertTrue(result["dryRun"])
        self.assertIsNone(result["order"])
        tools = [n for n, _ in self.client.calls]
        self.assertNotIn("party-order-create", tools, "dry_run 下禁止真实下单")
        self.assertEqual(tools, ["mall-points-products", "query-party-city",
                                 "query-party-store", "query-party-store-date",
                                 "query-party-store-session", "mall-product-detail"])
        self.assertEqual(result["date"], "2026-10-16")
        self.assertEqual(result["session"]["price"], 50.0)  # 5000 分 → ¥50

    def test_spu_discovery_and_payload(self):
        result = pp.plan_party_booking(self.client, "成都市", count=5)
        payload = result["orderPayload"]
        self.assertEqual(payload["spuId"], 1779)
        self.assertEqual(payload["skuId"], 2027)
        self.assertEqual(payload["storeCode"], "3450261")
        self.assertEqual(payload["code"], 510100)
        self.assertEqual(payload["count"], 5)

    def test_confirm_places_order(self):
        result = pp.plan_party_booking(self.client, "成都市", count=6, dry_run=False)
        self.assertTrue(result["ok"])
        self.assertEqual(result["order"], {"success": True, "data": {"orderNo": "ORDER-1"}})
        self.assertIn("party-order-create", [n for n, _ in self.client.calls])

    def test_unknown_city_returns_failure(self):
        result = pp.plan_party_booking(self.client, "火星市")
        self.assertFalse(result["ok"])
        self.assertIn("未查询到城市", result["reason"])

    def test_no_party_spu_returns_failure(self):
        client = StubClient(lambda n, a: {"success": True, "data": []})
        result = pp.plan_party_booking(client, "成都市")
        self.assertFalse(result["ok"])
        self.assertIn("未定位到派对类主题商品", result["reason"])

    def test_report(self):
        text = pp.render_party_report(pp.plan_party_booking(self.client, "成都市"))
        self.assertIn("【主题活动预约】", text)
        self.assertIn("dry_run=True", text)


CALENDAR = {"success": True, "data": {"dailyList": [
    {"date": "2026-10-12", "dateText": "2026年10月12日 往期回顾",
     "events": [{"activityTitle": "会员日"}]},
    {"date": "2026-10-18", "dateText": "2026年10月18日 即将开始",
     "events": [{"activityTitle": "1+1随心配"}]},
]}}
ADDR = {"success": True, "data": {"addresses": [{"addressId": "A1"}]}}
DSTORES = {"success": True, "data": [
    {"storeCode": "3450314", "beCode": "345031481", "storeName": "府青路餐厅"}]}


def calendar_dispatch(name, arguments):
    if name == "campaign-calendar":
        return CALENDAR
    if name == "query-nearby-stores":
        return STORES
    if name == "delivery-query-addresses":
        return ADDR
    if name == "delivery-query-stores":
        return DSTORES
    if name == "query-meal-assistance":
        raise McpToolError("该门店不支持企业团餐助餐服务")
    raise AssertionError(name)


class CalendarTest(unittest.TestCase):
    def test_cross_suggestions_and_advice(self):
        result = pp.suggest_calendar_plan(StubClient(calendar_dispatch), "成都", "春熙路",
                                          store_limit=2)
        self.assertEqual(result["storeCount"], 2)
        self.assertEqual(len(result["days"]), 2)
        advices = [d["advice"] for d in result["days"]]
        self.assertTrue(any("已过期" in a for a in advices))
        self.assertTrue(any("提前" in a for a in advices))
        self.assertTrue(any("1+1随心配" in d["events"] for d in result["days"]))

    def test_group_meal_assistance_degrades(self):
        result = pp.suggest_calendar_plan(StubClient(calendar_dispatch), "成都", "春熙路")
        for item in result["mealAssistance"]:
            self.assertFalse(item["available"])
            self.assertIn("助餐服务查询失败", item["note"])

    def test_assistance_names_from_real_shape(self):
        """真实响应为 data.mealAssistanceItems[].gmServiceName。"""
        def dispatch(name, arguments):
            if name == "query-meal-assistance":
                return {"success": True, "data": {"mealAssistanceItems": [
                    {"gmServiceCode": "GMS001", "gmServiceName": "保鲜速达", "enable": True},
                    {"gmServiceCode": "GMS002", "gmServiceName": "专人分餐", "enable": False},
                ]}}
            return calendar_dispatch(name, arguments)

        result = pp.suggest_calendar_plan(StubClient(dispatch), "成都", "春熙路")
        item = result["mealAssistance"][0]
        self.assertTrue(item["available"])
        self.assertEqual(item["services"], ["保鲜速达"])

    def test_no_address_degrades(self):
        def dispatch(name, arguments):
            if name == "delivery-query-addresses":
                return {"success": True, "data": {"addresses": []}}
            return calendar_dispatch(name, arguments)

        result = pp.suggest_calendar_plan(StubClient(dispatch), "成都", "春熙路")
        self.assertTrue(any("暂无配送地址" in i.get("note", "") for i in result["mealAssistance"]))

    def test_report(self):
        result = pp.suggest_calendar_plan(StubClient(calendar_dispatch), "成都", "春熙路")
        text = pp.render_calendar_report(result)
        self.assertIn("【活动日历组织建议】", text)
        self.assertIn("企业团餐助餐服务", text)


class RoleMappingTest(unittest.TestCase):
    def test_category_maps_to_multiple_roles(self):
        mapping = pp._role_to_codes([
            {"name": "小食甜品\n/其他", "meals": [{"code": "S1"}, {"code": "D1"}]},
            {"name": "鸡肉汉堡\n/卷", "meals": [{"code": "M1"}]},
            {"name": "饮品", "meals": [{"code": "B1"}]},
        ])
        self.assertIn("S1", [c for c, _ in mapping["小食"]])
        self.assertIn("D1", [c for c, _ in mapping["甜品"]])
        self.assertIn("M1", [c for c, _ in mapping["主食"]])
        self.assertIn("B1", [c for c, _ in mapping["饮料"]])

    def test_pick_avoids_duplicate_code(self):
        mapping = pp._role_to_codes([
            {"name": "小食甜品\n/其他", "meals": [{"code": "X1"}]},
        ])
        picked, missing = pp._pick_codes(mapping, ("小食", "甜品"))
        self.assertEqual(len(picked), 1)
        self.assertEqual(missing, ["甜品"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
