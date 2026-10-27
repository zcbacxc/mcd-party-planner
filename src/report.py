"""报告渲染：把场景结果渲染为可分享的 Markdown / HTML。

约束（与方案 §4.5 一致）：

- **单文件、内联样式、无任何外部资源引用**——双击即可离线打开，不依赖网络与 CDN；
- 所有来自服务端的文本一律 `html.escape`，餐品名 / 门店名里出现 `<script>` 也只会显示成文字；
- 只做「呈现」，不参与任何业务计算：数字全部来自 `party_planner` 的结果字典，
  与 `--format json` 的数值完全一致。
"""

from __future__ import annotations

import html
import json
from typing import Any, Dict, List, Optional, Sequence

SCENARIO_TITLES = {
    "plan": "团餐方案",
    "party": "主题活动预约",
    "calendar": "活动日历组织建议",
}


# ---------------------------------------------------------------------------
# 文档模型：先归一化成「标题 + 元信息 + 分节」，再分别渲染 md / html
# ---------------------------------------------------------------------------
def _meta_rows(pairs: Sequence[Sequence[Any]]) -> List[List[str]]:
    return [[_text(label), _text(value)] for label, value in pairs]


def _text(value: Any) -> str:
    if value is None or value == "":
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    return str(value)


def _money(value: Any) -> str:
    if value is None or value == "":
        return "—"
    return f"¥{value}"


def build_document(scenario: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """把场景结果归一化成渲染用的文档模型（不含任何 HTML）。"""
    if scenario not in SCENARIO_TITLES:
        raise ValueError(f"未知场景：{scenario}（可选 {', '.join(SCENARIO_TITLES)}）")
    builder = {"plan": _plan_doc, "party": _party_doc, "calendar": _calendar_doc}[scenario]
    doc = builder(result or {})
    doc["title"] = f"{SCENARIO_TITLES[scenario]}"
    doc.setdefault("sections", [])
    doc.setdefault("notes", [])
    return doc


def _plan_doc(result: Dict[str, Any]) -> Dict[str, Any]:
    plans = result.get("plans") or []
    meta = _meta_rows([
        ("城市", result.get("city")),
        ("门店关键词", result.get("keyword")),
        ("人数", result.get("headcount")),
        ("总预算", _money(result.get("budget"))),
        ("已扫描门店", result.get("storesScanned")),
        ("可选方案", len(plans)),
        ("预算内方案", result.get("affordableCount")),
    ])

    rows: List[List[str]] = []
    for index, plan in enumerate(plans, 1):
        items = "、".join(
            f"{_text(item.get('name'))}×{_text(item.get('quantity'))}"
            for item in plan.get("items") or []
        )
        rows.append([
            str(index),
            _text(plan.get("combo")),
            _text(plan.get("store")),
            _money(plan.get("total")),
            _money(plan.get("perPerson")),
            _text(plan.get("withinBudget")),
            items,
        ])

    sections = [{
        "heading": "方案对比",
        "table": {
            "columns": ["#", "组合", "门店", "合计", "人均", "预算内", "构成"],
            "rows": rows,
            "empty": "该门店群暂无可计价方案",
        },
    }]

    recommended = result.get("recommended")
    if recommended:
        sections.insert(0, {
            "heading": "推荐",
            "lines": [
                f"{_text(recommended.get('combo'))}　{_text(recommended.get('store'))}　"
                f"合计 {_money(recommended.get('total'))}　人均 {_money(recommended.get('perPerson'))}　"
                f"预算内：{_text(recommended.get('withinBudget'))}"
            ],
        })

    return {"meta": meta, "sections": sections,
            "notes": [str(w) for w in result.get("warnings") or []]}


def _party_doc(result: Dict[str, Any]) -> Dict[str, Any]:
    if not result.get("ok"):
        return {
            "meta": _meta_rows([("结果", "无法继续")]),
            "sections": [{
                "heading": "原因",
                "lines": [_text(result.get("reason"))],
            }, {
                "heading": "调用链路",
                "lines": [_text(t.get("tool")) for t in result.get("trace") or []],
            }],
            "notes": [],
        }

    session = result.get("session") or {}
    meta = _meta_rows([
        ("主题", _text((result.get("spu") or {}).get("spuName"))),
        ("spuId", (result.get("spu") or {}).get("spuId")),
        ("城市", result.get("city")),
        ("门店", f"{_text(result.get('store'))}（{_text(result.get('storeCode'))}）"),
        ("日期", result.get("date")),
        ("场次", f"{_text(session.get('timeStart'))}-{_text(session.get('timeEnd'))}"),
        ("余位", session.get("leftNum")),
        ("价格", _money(session.get("price"))),
        ("dry_run", result.get("dryRun")),
    ])

    sections = [{
        "heading": "调用链路",
        "lines": [_text(t.get("tool")) for t in result.get("trace") or []],
    }, {
        "heading": "下单载荷",
        "pre": json.dumps(result.get("orderPayload") or {}, ensure_ascii=False, indent=2, default=str),
    }]

    notes = [str(n) for n in (result.get("warning"), result.get("note")) if n]
    return {"meta": meta, "sections": sections, "notes": notes}


def _calendar_doc(result: Dict[str, Any]) -> Dict[str, Any]:
    days = result.get("days") or []
    meta = _meta_rows([
        ("城市", result.get("city")),
        ("门店关键词", result.get("keyword")),
        ("参考门店数", result.get("storeCount")),
        ("日历天数", len(days)),
    ])

    rows = [[
        _text(day.get("dateText") or day.get("date")),
        "、".join(_text(title) for title in day.get("events") or []) or "—",
        _text(day.get("advice")),
    ] for day in days]

    assistance_rows = []
    for item in result.get("mealAssistance") or []:
        if not item.get("store"):
            assistance_rows.append([_text(item.get("note")), "", ""])
            continue
        services = item.get("services") or []
        assistance_rows.append([
            _text(item.get("store")),
            "、".join(_text(s) for s in services) if services else ("不支持" if item.get("available") is False else "—"),
            _text(item.get("note")),
        ])

    sections = [{
        "heading": "活动日历",
        "table": {"columns": ["日期", "活动", "建议"], "rows": rows, "empty": "该时间段无活动"},
    }]
    if assistance_rows:
        sections.append({
            "heading": "企业团餐助餐服务",
            "table": {"columns": ["门店", "服务", "备注"], "rows": assistance_rows, "empty": "—"},
        })

    return {"meta": meta, "sections": sections, "notes": []}


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------
def _md_cell(value: Any) -> str:
    """表格单元格：竖线转义、换行压平，避免破坏表格结构。"""
    return _text(value).replace("|", "\\|").replace("\r", " ").replace("\n", " ")


def _md_table(table: Dict[str, Any]) -> List[str]:
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    lines = ["| " + " | ".join(_md_cell(c) for c in columns) + " |",
             "|" + "|".join(["---"] * len(columns)) + "|"]
    if not rows:
        lines.append("| " + " | ".join([_md_cell(table.get("empty") or "—")]
                                        + [""] * (len(columns) - 1)) + " |")
        return lines
    for row in rows:
        cells = [_md_cell(cell) for cell in row]
        cells += [""] * (len(columns) - len(cells))
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def to_markdown(scenario: str, result: Dict[str, Any]) -> str:
    doc = build_document(scenario, result)
    lines = [f"# {doc['title']}", ""]
    lines += _md_table({"columns": ["项", "值"], "rows": doc["meta"]})
    for section in doc["sections"]:
        lines += ["", f"## {section['heading']}", ""]
        if section.get("table"):
            lines += _md_table(section["table"])
        for line in section.get("lines") or []:
            lines.append(f"- {line}")
        if section.get("pre"):
            lines += ["```json", section["pre"], "```"]
    if doc["notes"]:
        lines += ["", "## 提示", ""]
        lines += [f"- {note}" for note in doc["notes"]]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML（单文件、内联 CSS、零外链）
# ---------------------------------------------------------------------------
_STYLE = """
:root { color-scheme: light; }
* { box-sizing: border-box; }
body { margin: 0; padding: 32px 20px; background: #f6f7f9; color: #1f2328;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Microsoft YaHei", sans-serif;
  line-height: 1.6; }
main { max-width: 960px; margin: 0 auto; background: #fff; border-radius: 12px;
  padding: 28px 32px 36px; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
h1 { font-size: 24px; margin: 0 0 4px; }
.sub { color: #6b7280; font-size: 13px; margin: 0 0 24px; }
h2 { font-size: 16px; margin: 28px 0 12px; padding-bottom: 6px; border-bottom: 1px solid #e5e7eb; }
table { width: 100%; border-collapse: collapse; font-size: 14px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f3; vertical-align: top; }
th { background: #fafbfc; font-weight: 600; color: #374151; }
tbody tr:hover { background: #fafbfc; }
.meta th { width: 128px; color: #6b7280; font-weight: 500; }
.rec { background: #f0f7ff; border: 1px solid #d6e6ff; border-radius: 8px; padding: 12px 14px; }
ul { margin: 8px 0; padding-left: 20px; }
li { margin: 4px 0; }
pre { background: #0d1117; color: #e6edf3; padding: 14px 16px; border-radius: 8px;
  overflow-x: auto; font-size: 13px; line-height: 1.5; }
.note { background: #fff8e6; border: 1px solid #ffe4a3; border-radius: 8px; padding: 10px 14px;
  font-size: 14px; }
footer { margin-top: 28px; color: #9ca3af; font-size: 12px; }
"""


def _esc(value: Any) -> str:
    return html.escape(_text(value), quote=True)


def _html_table(table: Dict[str, Any], css: str = "") -> str:
    columns = table.get("columns") or []
    rows = table.get("rows") or []
    parts = [f'<table class="{css}">' if css else "<table>", "<thead><tr>"]
    parts += [f"<th>{_esc(c)}</th>" for c in columns]
    parts.append("</tr></thead><tbody>")
    if not rows:
        parts.append(f'<tr><td colspan="{len(columns)}">{_esc(table.get("empty") or "—")}</td></tr>')
    for row in rows:
        cells = [_esc(cell) for cell in row]
        cells += [""] * (len(columns) - len(cells))
        parts.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    parts.append("</tbody></table>")
    return "".join(parts)


def to_html(scenario: str, result: Dict[str, Any],
            subtitle: str = "由 mcd-party-planner 生成 · 数据来自麦当劳 MCP") -> str:
    doc = build_document(scenario, result)
    body: List[str] = [f"<h1>{_esc(doc['title'])}</h1>",
                       f'<p class="sub">{_esc(subtitle)}</p>',
                       _html_table({"columns": ["项", "值"], "rows": doc["meta"]}, css="meta")]
    for section in doc["sections"]:
        body.append(f"<h2>{_esc(section['heading'])}</h2>")
        if section.get("table"):
            body.append(_html_table(section["table"]))
        if section.get("lines"):
            items = "".join(f"<li>{_esc(line)}</li>" for line in section["lines"])
            body.append(f"<ul>{items}</ul>")
        if section.get("pre"):
            body.append(f"<pre>{_esc(section['pre'])}</pre>")
    if doc["notes"]:
        body.append("<h2>提示</h2>")
        body.append("".join(f'<div class="note">{_esc(note)}</div>' for note in doc["notes"]))
    body.append("<footer>本页为静态快照，不包含任何外部资源引用，可离线打开。</footer>")

    return (
        "<!DOCTYPE html>\n"
        '<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{_esc(doc['title'])} · mcd-party-planner</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n<main>\n"
        + "\n".join(body)
        + "\n</main>\n</body>\n</html>\n"
    )
