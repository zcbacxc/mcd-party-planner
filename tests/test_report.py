"""报告渲染测试：Markdown / HTML 的结构稳定性与转义安全。

重点：**服务端文本一律转义**（餐品名里出现 `<script>` 只能显示为文字），
以及 HTML **不引用任何外部资源**（否则「双击离线打开」就不成立）。
"""

import os
import re
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from src import report  # noqa: E402

PLAN_RESULT = {
    "city": "成都", "keyword": "春熙路", "headcount": 8, "budget": 400.0,
    "storesScanned": 2, "affordableCount": 1,
    "plans": [
        {"store": "麦当劳成都锦江区联升巷(京都大厦)餐厅", "storeCode": "1", "combo": "经济型",
         "total": 342.4, "perPerson": 42.8, "withinBudget": True,
         "items": [{"name": "厚薯泥培根肉酱双牛堡买一送一", "category": "主食", "quantity": 8}]},
        {"store": "另一家门店", "storeCode": "2", "combo": "均衡型",
         "total": 591.2, "perPerson": 73.9, "withinBudget": False,
         "items": [{"name": "含竖线|的餐品名", "category": "主食", "quantity": 8}]},
    ],
    "recommended": {"store": "麦当劳成都锦江区联升巷(京都大厦)餐厅", "combo": "经济型",
                    "total": 342.4, "perPerson": 42.8, "withinBudget": True},
    "warnings": ["门店 X 当前非营业时间，计价结果可能不可用"],
}

PARTY_RESULT = {
    "ok": True, "spu": {"spuId": 8084, "spuName": "迪士尼奇妙生日探索之旅"},
    "city": "成都市", "store": "麦当劳成都高新区大魔方餐厅", "storeCode": "3450261",
    "date": "2026-10-27",
    "session": {"id": 1, "timeStart": "18:30", "timeEnd": "20:00", "price": 50.0,
                "leftNum": 12, "partyMin": 5, "partyMax": 12},
    "orderPayload": {"spuId": 8084, "count": 5},
    "trace": [{"tool": "query-party-city"}, {"tool": "query-party-store"}],
    "dryRun": True, "note": "dry_run=True：未真实下单，仅输出可下单载荷",
}

CALENDAR_RESULT = {
    "city": "成都", "keyword": "春熙路", "storeCount": 3,
    "days": [{"date": "2026-10-27", "dateText": "今日",
              "events": ["<script>alert(1)</script>"], "stores": ["A"],
              "advice": "建议立即组织"}],
    "stores": [{"name": "A", "code": "1"}],
    "mealAssistance": [{"note": "账号暂无配送地址，无法查询企业团餐助餐服务"}],
}


class MarkdownTest(unittest.TestCase):
    def test_plan_table_columns_are_stable(self):
        text = report.to_markdown("plan", PLAN_RESULT)
        header = next(line for line in text.splitlines() if line.startswith("| # |"))
        self.assertEqual(header.count("|"), 8, "方案表应为 7 列（8 个竖线）")
        # 分隔行与表头列数一致
        divider = text.splitlines()[text.splitlines().index(header) + 1]
        self.assertEqual(divider.count("|"), header.count("|"))

    def test_plan_includes_recommendation_and_warnings(self):
        text = report.to_markdown("plan", PLAN_RESULT)
        self.assertIn("## 推荐", text)
        self.assertIn("¥342.4", text)
        self.assertIn("## 提示", text)
        self.assertIn("非营业时间", text)

    def test_pipe_in_cell_is_escaped(self):
        text = report.to_markdown("plan", PLAN_RESULT)
        self.assertIn("含竖线\\|的餐品名", text)

    def test_party_failure_renders_reason(self):
        text = report.to_markdown("party", {"ok": False, "reason": "该城市暂无可参与主题活动的门店",
                                           "trace": [{"tool": "query-party-city"}]})
        self.assertIn("无法继续", text)
        self.assertIn("该城市暂无可参与主题活动的门店", text)


class HtmlTest(unittest.TestCase):
    def test_no_external_resources(self):
        for scenario, result in (("plan", PLAN_RESULT), ("party", PARTY_RESULT),
                                 ("calendar", CALENDAR_RESULT)):
            page = report.to_html(scenario, result)
            self.assertNotIn("http://", page)
            self.assertNotIn("https://", page)
            self.assertNotIn("<script", page)
            self.assertNotIn("<link", page)
            self.assertNotIn("<img", page)
            self.assertIn("<style>", page, "样式必须内联")

    def test_escapes_server_text(self):
        page = report.to_html("calendar", CALENDAR_RESULT)
        self.assertNotIn("<script>alert(1)</script>", page)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", page)

    def test_escapes_quotes_in_attributes_and_text(self):
        page = report.to_html("plan", {"city": '"onmouseover=alert(1)', "keyword": "k",
                                       "headcount": 1, "budget": 1, "plans": []})
        self.assertIn("&quot;onmouseover=alert(1)", page)

    def test_renders_all_three_scenarios(self):
        for scenario, result in (("plan", PLAN_RESULT), ("party", PARTY_RESULT),
                                 ("calendar", CALENDAR_RESULT)):
            page = report.to_html(scenario, result)
            self.assertTrue(page.startswith("<!DOCTYPE html>"))
            self.assertIn(report.SCENARIO_TITLES[scenario], page)

    def test_calendar_assistance_note_row(self):
        page = report.to_html("calendar", CALENDAR_RESULT)
        self.assertIn("账号暂无配送地址", page)


class BuildDocumentTest(unittest.TestCase):
    def test_unknown_scenario_rejected(self):
        with self.assertRaises(ValueError):
            report.build_document("unknown", {})

    def test_empty_result_does_not_crash(self):
        for scenario in report.SCENARIO_TITLES:
            report.to_markdown(scenario, {})
            report.to_html(scenario, {})

    def test_markdown_table_placeholder_when_empty(self):
        text = report.to_markdown("plan", {"plans": [], "affordableCount": 0})
        self.assertIn("暂无可计价方案", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
